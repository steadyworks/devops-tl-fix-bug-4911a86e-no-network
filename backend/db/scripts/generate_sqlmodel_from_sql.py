"""
Usage:
cd $(git rev-parse --show-toplevel)/backend && PYTHONPATH=.. python db/scripts/generate_sqlmodel_from_sql.py

This script parses the schema.sql file and generates SQLModel classes into backend/db/data_models/__init__.py
"""

import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUTPUT_FILE = Path("db/data_models/__init__.py")
INPUT_FILE = Path("db/schema.sql")

SQL_TO_PYTHON_TYPE: dict[str, str] = {
    "uuid": "UUID",
    "text": "str",
    "character varying": "str",
    "jsonb": "dict[str, Any]",
    "integer": "int",
    "timestamp without time zone": "datetime",
    "timestamp with time zone": "datetime",
    "timestamp": "datetime",  # fallback
}

RESERVED_NAMES = {"metadata"}
ENUMS: dict[str, list[str]] = {}  # SQL enum name → list of values


def snake_to_pascal_case(s: str) -> str:
    return "".join(word.capitalize() for word in s.split("_"))


def parse_enums(sql: str) -> None:
    matches = re.findall(
        r"CREATE TYPE (?:public\.)?(\w+) AS ENUM\s*\((.*?)\);",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    for enum_name, values_raw in matches:
        values = [v.strip().strip("'") for v in values_raw.split(",")]
        ENUMS[enum_name] = values


def parse_tables(sql: str) -> dict[str, list[dict[str, Any]]]:
    tables: dict[str, list[dict[str, Any]]] = {}
    table_blocks = re.findall(
        r"CREATE TABLE public\.(\w+)\s*\((.*?)\);", sql, re.DOTALL
    )

    for table_name, body in table_blocks:
        columns: list[dict[str, Any]] = []
        lines = [
            line.strip().rstrip(",")
            for line in body.strip().splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]

        for line in lines:
            parts = line.split()
            if not parts:
                continue

            col_name = parts[0]
            raw_type = " ".join(parts[1:]).strip()

            nullable = "NOT NULL" not in raw_type.upper()
            default = None
            if "DEFAULT" in raw_type.upper():
                type_and_default = re.split(
                    r"\bDEFAULT\b", raw_type, maxsplit=1, flags=re.IGNORECASE
                )
                raw_type = type_and_default[0].strip()
                default = (
                    type_and_default[1].strip() if len(type_and_default) > 1 else None
                )

            col: dict[str, Any] = {
                "name": col_name,
                "type": raw_type.strip(),
                "nullable": nullable,
                "default": default,
            }
            columns.append(col)

        tables[table_name] = columns

    return tables


def extract_base_type(raw_type: str) -> str:
    raw_type = raw_type.lower()
    raw_type = re.split(r"\bdefault\b", raw_type)[0].strip()
    raw_type = re.split(r"\bnot null\b", raw_type)[0].strip()
    raw_type = re.split(r"\bnull\b", raw_type)[0].strip()
    raw_type = raw_type.split("::")[0].strip()

    # Handle public.schema prefix like 'public.user_provided_occasion'
    if raw_type.startswith("public."):
        raw_type = raw_type.split(".", 1)[1]
    return raw_type


def map_column_to_field(col: dict[str, Any]) -> str:
    orig_name = col["name"]
    nullable = col["nullable"]
    is_reserved = orig_name in RESERVED_NAMES
    name = orig_name + "_" if is_reserved else orig_name

    sql_type = extract_base_type(col["type"])
    if sql_type in ENUMS:
        py_type = snake_to_pascal_case(sql_type)
        enum_class_expr = f"""sa_column=Column(Enum({py_type}, nullable={"True" if nullable else "False"}, values_callable=enum_values))"""
    else:
        py_type = SQL_TO_PYTHON_TYPE.get(sql_type, "Any")
        enum_class_expr = None

    default = col.get("default")
    is_primary = orig_name == "id"

    type_prefix = f"Optional[{py_type}]" if nullable and not is_primary else py_type

    field_args: list[str] = []

    if is_primary:
        field_args.append("primary_key=True")
        if default and "gen_random_uuid()" in default:
            field_args.append("default_factory=uuid4")
    elif default:
        if "now()" in default:
            field_args.append("default_factory=lambda: datetime.now(timezone.utc)")
    elif nullable:
        field_args.append("default=None")

    if is_reserved:
        raise Exception(
            "Naming a field metadata is known to cause problems with SQLAlchemy. Please rename the column."
        )
    elif sql_type in {"json", "jsonb"}:
        field_args.append("sa_type=JSON")

    if enum_class_expr:
        field_args.append(enum_class_expr)
    field_expr = f" = Field({', '.join(field_args)})" if field_args else ""
    return f"    {name}: {type_prefix}{field_expr}"


def render_enum(name: str, values: list[str]) -> str:
    enum_name = snake_to_pascal_case(name)
    lines = [f"class {enum_name}(str, enum.Enum):"]
    for value in values:
        const_name = value.upper().replace(" ", "_")
        lines.append(f"    {const_name} = {repr(value)}")
    return "\n".join(lines)


def render_model(table_name: str, columns: list[dict[str, Any]]) -> str:
    class_name = "".join(word.capitalize() for word in table_name.split("_"))
    lines = [f"class DAO{class_name}(SQLModel, table=True):"]
    lines.append(f'    __tablename__ = cast("Any", "{table_name}")')
    if not columns:
        lines.append("    pass")
    else:
        lines += [map_column_to_field(col) for col in columns]
    return "\n".join(lines)


def main() -> None:
    sql = INPUT_FILE.read_text()

    # Step 1: Parse enums and tables
    parse_enums(sql)
    tables = parse_tables(sql)

    # Step 2: Emit header
    generated_header = f"""# ---------------------------------------------
# ⚠️ AUTO-GENERATED FILE — DO NOT EDIT MANUALLY
# Source: backend/db/schemas/__init__.py, backend/db/data_models/__init__.py
# Generated by: backend/db/scripts/generate_sqlmodel_from_sql.py
# Time: {datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")}
# ---------------------------------------------

"""

    header_imports = """import enum
from datetime import datetime, timezone
from typing import Any, Optional, cast
from uuid import UUID, uuid4

from sqlalchemy.dialects.postgresql import JSON
from sqlmodel import Field, SQLModel, Column, Enum

def enum_values(enum_class: type[enum.Enum]) -> list[str]:
    \"\"\"Get values for enum.\"\"\"
    return [status.value for status in enum_class]


"""

    # Step 3: Emit body
    enum_block = "\n\n".join(
        render_enum(name, values) for name, values in ENUMS.items()
    )
    model_block = "\n\n".join(
        render_model(name, cols)
        for name, cols in tables.items()
        if name != "schema_migrations"
    )

    # Step 4: Write file
    OUTPUT_FILE.write_text(
        generated_header + header_imports + enum_block + "\n\n" + model_block + "\n"
    )
    print(f"✅ Generated {OUTPUT_FILE}")

    # Step 5: Run Ruff
    try:
        subprocess.run(["ruff", "format", OUTPUT_FILE], check=True)
        subprocess.run(
            ["ruff", "check", "--select", "I", "--fix", OUTPUT_FILE], check=True
        )
        print("✅ Applied ruff formatting")
    except subprocess.CalledProcessError as e:
        print(f"❌ Ruff formatting failed: {e}")
    except FileNotFoundError:
        print("⚠️ Ruff not installed. Run `pip install ruff`.")


if __name__ == "__main__":
    main()
