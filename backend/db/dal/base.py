import logging
from contextlib import asynccontextmanager
from enum import Enum
from typing import Any, AsyncGenerator, Callable, Generic, Optional, TypeVar
from uuid import UUID

from sqlalchemy import ColumnElement, and_, asc, case, desc, func, select, update
from sqlalchemy import exists as sa_exists
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import SQLModel

from backend.db.dal.schemas import WritableModel
from backend.lib.types.exception import UUIDNotFoundError
from backend.lib.utils.common import utcnow


@asynccontextmanager
async def safe_commit(session: AsyncSession) -> AsyncGenerator[None, None]:
    try:
        yield  # caller uses the session directly
        await session.commit()
    except Exception:
        await session.rollback()
        logging.exception("DB commit failed; session rolled back.")
        raise


class FilterOp(str, Enum):
    EQ = "eq"
    NE = "ne"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IN = "in"


class OrderDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


# === TypeVars ===

ModelType = TypeVar("ModelType", bound=SQLModel)

CreateSchemaType = TypeVar("CreateSchemaType", bound=WritableModel)
UpdateSchemaType = TypeVar("UpdateSchemaType", bound=WritableModel)


# === Exceptions ===


class InvalidFilterFieldError(ValueError):
    def __init__(self, field: str, model: type[SQLModel]) -> None:
        super().__init__(f"Invalid field '{field}' for model '{model.__name__}'")


# === DAL ===
class AsyncPostgreSQLDAL(Generic[ModelType, CreateSchemaType, UpdateSchemaType]):
    model: type[ModelType]  # Must be set in subclass
    IMMUTABLE_FIELDS: set[str] = {"id", "created_at"}
    AUTO_UPDATE_FIELDS: dict[str, Callable[[], Any]] = {"updated_at": lambda: utcnow()}

    @classmethod
    async def _add_and_flush(
        cls,
        session: AsyncSession,
        objs: ModelType | list[ModelType],
    ) -> None:
        if isinstance(objs, list):
            session.add_all(objs)
        else:
            session.add(objs)
        await session.flush()

    @classmethod
    def _get_column(cls, field: str) -> Any:
        if not hasattr(cls.model, field):
            raise InvalidFilterFieldError(field, cls.model)
        return getattr(cls.model, field)

    @classmethod
    async def get_by_id(cls, session: AsyncSession, id: UUID) -> Optional[ModelType]:
        return await session.get(cls.model, id)

    @classmethod
    async def get_by_ids(
        cls, session: AsyncSession, ids: list[UUID]
    ) -> list[ModelType]:
        if not ids:
            return []
        id_col = getattr(cls.model, "id")
        stmt = select(cls.model).where(id_col.in_(ids))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    async def create(cls, session: AsyncSession, obj_in: CreateSchemaType) -> ModelType:
        db_obj: ModelType = cls.model.model_validate(obj_in)
        await cls._add_and_flush(session, db_obj)
        return db_obj

    @classmethod
    async def update_many_by_id(
        cls,
        session: AsyncSession,
        updates: dict[UUID, UpdateSchemaType],
    ) -> None:
        """
        Batch update rows using per-ID UpdateSchemaType objects.

        Args:
            session: Active DB session.
            updates: Mapping of UUID -> partial update object.
        """
        if not updates:
            logging.info("update_many_by_id called with empty dict. Skipping.")
            return

        try:
            id_col = getattr(cls.model, "id")

            parsed_updates: list[tuple[UUID, dict[str, Any]]] = []
            for id_, update_obj in updates.items():
                data = update_obj.model_dump(exclude_unset=True)
                if not data:
                    continue  # skip no-op updates
                parsed_updates.append((id_, data))

            if not parsed_updates:
                logging.info("No fields to update after parsing. Skipping.")
                return

            # Determine all fields to update
            all_fields: set[str] = set()
            for _, data in parsed_updates:
                all_fields.update(data.keys())

            if not all_fields:
                logging.warning("No fields detected for update.")
                return

            # Build CASE expressions
            values_to_set = {}
            for field in all_fields:
                field_cases = {
                    id_: data[field] for id_, data in parsed_updates if field in data
                }
                values_to_set[field] = case(field_cases, value=id_col)

            stmt = (
                update(cls.model)
                .where(id_col.in_([id_ for id_, _ in parsed_updates]))
                .values(**values_to_set)
            )

            await session.execute(stmt)

        except Exception:
            logging.exception("update_many_by_id failed.")
            raise

    @classmethod
    async def update_by_id(
        cls, session: AsyncSession, id: UUID, obj_in: UpdateSchemaType
    ) -> ModelType:
        db_obj = await session.get(cls.model, id)
        if db_obj is None:
            raise UUIDNotFoundError(id)
        return await cls._update(session, db_obj, obj_in)

    @classmethod
    async def _update(
        cls, session: AsyncSession, db_obj: ModelType, obj_in: UpdateSchemaType
    ) -> ModelType:
        update_data: dict[str, Any] = obj_in.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if field not in cls.IMMUTABLE_FIELDS and hasattr(db_obj, field):
                setattr(db_obj, field, value)

        for field, factory in cls.AUTO_UPDATE_FIELDS.items():
            if not hasattr(
                db_obj, field
            ):  # Data model does not contain auto update field
                continue

            if (
                hasattr(obj_in, field) and getattr(obj_in, field) is not None
            ):  # Explicit value set by update request
                continue

            setattr(db_obj, field, factory())
        await cls._add_and_flush(session, db_obj)
        return db_obj

    @classmethod
    def _resolve_filter_condition(
        cls,
        field: str,
        op: FilterOp,
        value: Any,
    ) -> ColumnElement[bool]:
        column = cls._get_column(field)
        if op == FilterOp.EQ:
            return column == value
        if op == FilterOp.NE:
            return column != value
        if op == FilterOp.LT:
            return column < value
        if op == FilterOp.LTE:
            return column <= value
        if op == FilterOp.GT:
            return column > value
        if op == FilterOp.GTE:
            return column >= value
        if op == FilterOp.IN and isinstance(value, list):
            return column.in_(value)
        raise ValueError(f"Unsupported filter op: {op}")

    @classmethod
    def _build_filter_conditions(
        cls,
        filters: Optional[dict[str, tuple[FilterOp, Any]]],
    ) -> list[ColumnElement[bool]]:
        if not filters:
            return []
        return [
            cls._resolve_filter_condition(f, op, v) for f, (op, v) in filters.items()
        ]

    @classmethod
    async def list_all(
        cls,
        session: AsyncSession,
        filters: Optional[dict[str, tuple[FilterOp, Any]]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        order_by: Optional[list[tuple[str, OrderDirection]]] = None,
    ) -> list[ModelType]:
        stmt = select(cls.model)

        conditions = cls._build_filter_conditions(filters)
        if conditions:
            stmt = stmt.where(and_(*conditions))

        if order_by:
            stmt = stmt.order_by(
                *[
                    desc(cls._get_column(field))
                    if direction == OrderDirection.DESC
                    else asc(cls._get_column(field))
                    for field, direction in order_by
                ]
            )

        if limit is not None:
            stmt = stmt.limit(limit)
        if offset is not None:
            stmt = stmt.offset(offset)

        result = await session.execute(stmt)
        return list(result.scalars().all())

    @classmethod
    async def count(
        cls,
        session: AsyncSession,
        filters: Optional[dict[str, tuple[FilterOp, Any]]] = None,
    ) -> int:
        stmt = select(func.count()).select_from(cls.model)
        conditions = cls._build_filter_conditions(filters)
        if conditions:
            stmt = stmt.where(and_(*conditions))
        result = await session.execute(stmt)
        return result.scalar_one()

    @classmethod
    async def exists(
        cls,
        session: AsyncSession,
        filters: Optional[dict[str, tuple[FilterOp, Any]]] = None,
    ) -> bool:
        conditions = cls._build_filter_conditions(filters)
        stmt = (
            select(sa_exists().where(and_(*conditions)))
            if conditions
            else select(sa_exists().select_from(cls.model))
        )
        result = await session.execute(stmt)
        return result.scalar_one_or_none() is True

    @classmethod
    async def create_many(
        cls,
        session: AsyncSession,
        objs_in: list[CreateSchemaType],
    ) -> list[ModelType]:
        db_objs = [cls.model.model_validate(obj) for obj in objs_in]
        await cls._add_and_flush(session, db_objs)
        return db_objs
