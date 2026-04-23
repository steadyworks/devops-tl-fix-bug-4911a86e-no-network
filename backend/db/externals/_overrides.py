# pyright: reportPrivateUsage=false

from typing import Optional, Self

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.dal import DALAssets
from backend.db.data_models import DAOAssets, DAOPhotobooks
from backend.lib.asset_manager.base import AssetManager

from ._generated_DO_NOT_USE import (
    ReadableModelConvertibleFromDAOMixin,
    _AssetsOverviewResponse,
    _PhotobooksOverviewResponse,
)


class AssetsOverviewResponse(_AssetsOverviewResponse):
    asset_key_original: str = Field(exclude=True)
    asset_key_display: Optional[str] = Field(exclude=True)
    asset_key_llm: Optional[str] = Field(exclude=True)
    signed_asset_url: str

    @classmethod
    async def rendered_from_dao(
        cls,
        dao: DAOAssets,
        asset_manager: AssetManager,
    ) -> Self:
        signed_url = await asset_manager.generate_signed_url(dao.asset_key_original)
        return cls(
            **dao.model_dump(),
            signed_asset_url=signed_url,
        )


class PhotobooksOverviewResponse(
    _PhotobooksOverviewResponse, ReadableModelConvertibleFromDAOMixin[DAOPhotobooks]
):
    thumbnail_asset_signed_url: Optional[str]

    @classmethod
    async def rendered_from_dao(
        cls,
        dao: DAOPhotobooks,
        db_session: AsyncSession,
        asset_manager: AssetManager,
    ) -> Self:
        thumbnail_signed_url = None
        if dao.thumbnail_asset_id is not None:
            thumbnail_asset = await DALAssets.get_by_id(
                db_session, dao.thumbnail_asset_id
            )
            if thumbnail_asset is not None:
                thumbnail_signed_url = await asset_manager.generate_signed_url(
                    thumbnail_asset.asset_key_original  # FIXME
                )

        return cls(
            **cls.from_dao(dao).model_dump(),
            thumbnail_asset_signed_url=thumbnail_signed_url,
        )
