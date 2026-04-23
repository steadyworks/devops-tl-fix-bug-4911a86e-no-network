"""
Usage:
cd $(git rev-parse --show-toplevel)/backend && PYTHONPATH=.. python db/scripts/generate_crud_schemas.py
"""

import ast
import subprocess
from datetime import datetime, timezone
from typing import Any, Optional, Set, Union, get_args, get_origin

from sqlmodel import SQLModel

import backend.db.data_models as data_models

# Path to the output file
OUTPUT_PATH = "db/dal/schemas.py"
OUTPUT_PATH_EXTERNALS = "db/externals/_generated_DO_NOT_USE.py"
INIT_PATH_EXTERNALS = "db/externals/__init__.py"
OVERRIDES_PATH = "db/externals/_overrides.py"

# Track types used for imports
used_typenames: Set[str] = set()
EXCLUDED_MODELS = {"SchemaMigrations"}


def generate_crud_schemas(
    model_cls: type[SQLModel], name: str
) -> tuple[str, str, bool]:
    fields: dict[str, Any] = model_cls.model_fields
    create_fields: dict[str, tuple[type[Any], Any, dict[str, Any]]] = {}
    read_fields: dict[str, tuple[type[Any], Any, dict[str, Any]]] = {}
    update_fields: dict[str, tuple[Any, Any, dict[str, Any]]] = {}

    used_field = False

    for fname, f in fields.items():
        annotation: Any = f.annotation
        field_info: dict[str, Any] = {}
        if f.alias and f.alias != fname:
            field_info["alias"] = f.alias

        if fname in {"id", "created_at"}:
            read_fields[fname] = (annotation, ..., field_info)
        elif fname in {"updated_at"}:
            update_fields[fname] = (Optional[annotation], None, field_info)
            read_fields[fname] = (annotation, ..., field_info)
        else:
            create_fields[fname] = (annotation, ..., field_info)
            update_fields[fname] = (Optional[annotation], None, field_info)
            read_fields[fname] = (annotation, ..., field_info)

    def render_field(name: str, typ: Any, default: Any, info: dict[str, Any]) -> str:
        nonlocal used_field
        typename = get_typename(typ)

        if info:
            used_field = True
            args = ", ".join(f"{k}={repr(v)}" for k, v in info.items())
            return (
                f"    {name}: {typename} = Field({args})"
                if default is ...
                else f"    {name}: {typename} = Field(default={default}, {args})"
            )
        return (
            f"    {name}: {typename}"
            if default is ...
            else f"    {name}: {typename} = {repr(default)}"
        )

    lines: list[str] = []

    lines.append(f"class {name}Create(WritableModel):")
    if create_fields:
        for k, (typ, default, info) in create_fields.items():
            lines.append(render_field(k, typ, default, info))
    else:
        lines.append("    pass")
    lines.append("")

    lines.append(f"class {name}Update(WritableModel):")
    if update_fields:
        for k, (typ, default, info) in update_fields.items():
            lines.append(render_field(k, typ, default, info))
    else:
        lines.append("    pass")
    lines.append("")

    lines_public: list[str] = []
    lines_public.append(
        f"class _{name.removeprefix('DAO')}OverviewResponse(ReadableModel[{name}]):"
    )
    if read_fields:
        for k, (typ, default, info) in read_fields.items():
            lines_public.append(render_field(k, typ, default, info))
    else:
        lines_public.append("    pass")
    lines_public.append("")

    return "\n".join(lines), "\n".join(lines_public), used_field


def get_typename(t: Any) -> str:
    origin = get_origin(t)
    args = get_args(t)

    if origin is Union and args:
        non_none_args = [a for a in args if a is not type(None)]
        if len(non_none_args) == 1:
            used_typenames.add("Optional")
            return f"Optional[{get_typename(non_none_args[0])}]"
        return " | ".join(get_typename(a) for a in args)

    if origin is list and args:
        used_typenames.add("list")
        return f"list[{get_typename(args[0])}]"

    if origin is dict and len(args) == 2:
        used_typenames.add("dict")
        return f"dict[{get_typename(args[0])}, {get_typename(args[1])}]"

    # ENUM FIX: track all used explicit type names (like UserProvidedOccasion)
    type_name = getattr(t, "__name__", str(t))
    used_typenames.add(type_name)
    return type_name


def emit_imports(
    field_used: bool, model_cls_set: set[type[SQLModel]]
) -> tuple[str, str]:
    lines: list[str] = [
        "from pydantic import BaseModel, ConfigDict",
    ]
    if field_used:
        lines.append("from pydantic import Field  # noqa: F401")

    if "Optional" in used_typenames:
        lines.append("from typing import Optional")
    if "Any" in used_typenames:
        lines.append("from typing import Any")
    if "UUID" in used_typenames:
        lines.append("from uuid import UUID")
    if "datetime" in used_typenames:
        lines.append("from datetime import datetime")

    # Import enums used in type hints
    enum_types = [
        tname
        for tname in sorted(used_typenames)
        if tname
        not in {"Optional", "Any", "UUID", "datetime", "list", "dict", "str", "int"}
    ]
    if enum_types:
        lines.append(f"from backend.db.data_models import {', '.join(enum_types)}")

    readable_extra = f"""\n
from sqlmodel import SQLModel
from typing import TypeVar, Generic, Self, Protocol, Sequence, runtime_checkable
from backend.db.data_models import {", ".join(model_cls.__name__ for model_cls in model_cls_set)}

TDAO = TypeVar("TDAO", bound=SQLModel, contravariant=True)

class ReadableModelConvertibleFromDAOMixin(BaseModel, Generic[TDAO]):
    @classmethod
    def from_dao(cls, dao: TDAO) -> Self:
        dao_dict = dao.model_dump()
        allowed_keys = cls.model_fields.keys()
        filtered = {{k: v for k, v in dao_dict.items() if k in allowed_keys}}
        return cls.model_validate(filtered)
        
    @classmethod
    def from_daos(cls, daos: Sequence[TDAO]) -> list[Self]:
        return [cls.from_dao(dao) for dao in daos]


class ReadableModel(BaseModel, Generic[TDAO]):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

@runtime_checkable
class CanBeRenderedFromDAO(Protocol, Generic[TDAO]):
    @classmethod
    async def rendered_from_dao(
        cls: type[Self], dao: TDAO, *args: Any, **kwargs: Any
    ) -> Self: ...
    
@runtime_checkable
class CanBeBatchRenderedFromDAOs(Protocol, Generic[TDAO]):
    @classmethod
    async def rendered_from_daos(
        cls: type[Self], dao: Sequence[TDAO], *args: Any, **kwargs: Any
    ) -> Sequence[Self]: ...

"""

    writeable_extra = """\n
class WritableModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True)  # used for Create/Update"""

    return (
        "\n".join(lines) + readable_extra + "\n\n",
        "\n".join(lines) + writeable_extra + "\n\n",
    )


if __name__ == "__main__":
    all_cls: list[tuple[type[SQLModel], str]] = []
    for name, cls in vars(data_models).items():
        if (
            isinstance(cls, type)
            and issubclass(cls, SQLModel)
            and name not in EXCLUDED_MODELS
            and cls.__name__ != "SQLModel"
        ):
            all_cls.append((cls, name))

    used_typenames.clear()
    class_defs: list[str] = []
    class_defs_read: list[str] = []
    field_used = False
    model_cls_set: set[type[SQLModel]] = set()

    for model_cls, name in all_cls:
        class_def, class_def_read, model_uses_field = generate_crud_schemas(
            model_cls, name
        )
        class_defs.append(class_def)
        class_defs_read.append(class_def_read)
        field_used |= model_uses_field
        model_cls_set.add(model_cls)

    imports_read, imports_write = emit_imports(field_used, model_cls_set)

    header = f"""# ---------------------------------------------
# ⚠️ AUTO-GENERATED FILE — DO NOT EDIT MANUALLY
# Source: backend/db/data_models/__init__.py
# Generated by: backend/db/scripts/generate_crud_schemas.py
# Time: {datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")}
# ---------------------------------------------

"""

    content = header + imports_write + "\n".join(class_defs)
    with open(OUTPUT_PATH, "w") as f:
        f.write(content)
    print(f"✅ Wrote: {OUTPUT_PATH}")

    header_read = f"""# ---------------------------------------------
# ⚠️ AUTO-GENERATED FILE — DO NOT EDIT MANUALLY
# Source: backend/db/data_models/__init__.py
# Generated by: backend/db/scripts/generate_crud_schemas.py
# Time: {datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")}
# ---------------------------------------------
# pyright: reportUnusedClass=false

"""
    content_read = header_read + imports_read + "\n".join(class_defs_read)
    with open(OUTPUT_PATH_EXTERNALS, "w") as f:
        f.write(content_read)
    print(f"✅ Wrote: {OUTPUT_PATH_EXTERNALS}")

    class_names = [name.removeprefix("DAO") + "OverviewResponse" for _, name in all_cls]
    class_names_sorted = sorted(class_names)

    overridden_classes: set[str] = set()

    try:
        with open(OVERRIDES_PATH, "r") as f:
            tree = ast.parse(f.read(), filename=OVERRIDES_PATH)
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    overridden_classes.add(node.name)
    except FileNotFoundError:
        # No _overrides.py — ignore
        pass

    init_header = f'''# ---------------------------------------------
# ⚠️ AUTO-GENERATED FILE — DO NOT EDIT MANUALLY
# Source: backend/db/data_models/__init__.py
# Generated by: backend/db/scripts/generate_crud_schemas.py
# Time: {datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")}
# ---------------------------------------------
# pyright: reportPrivateUsage=false
# pyright: reportUnusedClass=false
# pyright: reportUnusedImport=false
# ruff: noqa: F401

"""
This __init__.py exposes public OverviewResponse classes.

- If a class is overridden in _overrides.py, we use that.
- Otherwise, fall back to _generated_DO_NOT_USE.py.
"""

'''

    # Emit __all__ declaration
    import_lines = [
        "from ._generated_DO_NOT_USE import (",
        "    ReadableModelConvertibleFromDAOMixin,",
        *[f"    _{name}," for name in class_names_sorted],
        ")",
        "",
    ]

    import_lines += [
        "from backend.db.data_models import (",
        *[f"    {name}," for _, name in all_cls],
        ")",
        "",
    ]

    # Add static imports for overridden ones
    for name in class_names_sorted:
        if name in overridden_classes:
            import_lines.append(f"from ._overrides import {name}  # noqa: E402")
        else:
            import_lines.append(
                f"""class {name}(_{name}, ReadableModelConvertibleFromDAOMixin[{"DAO" + name.removesuffix("OverviewResponse")}]):
    pass"""
            )

    all_export = f"__all__ = {class_names_sorted!r}\n"
    init_content = init_header + "\n".join(import_lines) + "\n\n" + all_export
    with open(INIT_PATH_EXTERNALS, "w") as f:
        f.write(init_content)

    print(f"✅ Wrote: {INIT_PATH_EXTERNALS}")

    # Run Ruff format
    try:
        subprocess.run(["ruff", "format", OUTPUT_PATH], check=True)
        subprocess.run(
            ["ruff", "check", "--select", "I", "--fix", OUTPUT_PATH], check=True
        )
        subprocess.run(["ruff", "format", OUTPUT_PATH_EXTERNALS], check=True)
        subprocess.run(
            ["ruff", "check", "--select", "I", "--fix", OUTPUT_PATH_EXTERNALS],
            check=True,
        )
        subprocess.run(["ruff", "format", INIT_PATH_EXTERNALS], check=True)
        subprocess.run(
            ["ruff", "check", "--select", "I", "--fix", INIT_PATH_EXTERNALS],
            check=True,
        )
        print("✅ Applied ruff formatting")
    except subprocess.CalledProcessError as e:
        print(f"❌ Ruff formatting failed: {e}")
    except FileNotFoundError:
        print("⚠️ Ruff not installed. Run `pip install ruff`.")
