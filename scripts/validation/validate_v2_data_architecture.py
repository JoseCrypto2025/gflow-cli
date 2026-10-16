#!/usr/bin/env python3
"""Validate V2 master, transaction, and relation schema bundles/CSV exports."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCHEMA_DIR = ROOT / "schemas"
EXPECTED_COUNTS = {"master": 23, "transaction": 3, "relation": 3}
BOOL_TRUE = {"true", "1", "yes"}
BOOL_FALSE = {"false", "0", "no"}
ID_PATTERN_RE = re.compile(r"\^([^\s]+)\$")


def load_schemas(schema_dir: Path) -> dict[str, dict[str, Any]]:
    """Load and structurally validate every V2 schema JSON file."""
    schemas: dict[str, dict[str, Any]] = {}
    counts = {key: 0 for key in EXPECTED_COUNTS}
    for subdir in ("master", "transaction", "relation"):
        for schema_path in sorted((schema_dir / subdir).glob("*.schema.json")):
            try:
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"{schema_path}: invalid JSON: {exc}") from exc
            table = schema.get("table_name")
            if not isinstance(table, str) or not table:
                raise ValueError(f"{schema_path}: missing table_name")
            if schema.get("table_type") != subdir:
                raise ValueError(f"{schema_path}: table_type must be {subdir!r}")
            if table in schemas:
                raise ValueError(f"duplicate schema table_name: {table}")
            fields = schema.get("fields")
            if not isinstance(fields, list) or not fields:
                raise ValueError(f"{schema_path}: fields must be a non-empty array")
            names = [field.get("field_name") for field in fields]
            if any(not isinstance(name, str) or not name for name in names):
                raise ValueError(f"{schema_path}: every field requires a field_name")
            if len(names) != len(set(names)):
                raise ValueError(f"{schema_path}: duplicate field names")
            primary_keys = [field["field_name"] for field in fields if field.get("primary_key")]
            if primary_keys != [schema.get("primary_key")]:
                raise ValueError(
                    f"{schema_path}: primary_key must match the single primary_key field"
                )
            schemas[table] = schema
            counts[subdir] += 1

    for table_type, expected in EXPECTED_COUNTS.items():
        if counts[table_type] != expected:
            raise ValueError(
                f"expected {expected} {table_type} schemas, found {counts[table_type]}"
            )

    for schema in schemas.values():
        fields_by_name = {field["field_name"]: field for field in schema["fields"]}
        for field in schema["fields"]:
            foreign_key = field.get("foreign_key")
            if not foreign_key:
                continue
            target_table, _, target_field = foreign_key.partition(".")
            target_schema = schemas.get(target_table)
            if target_schema is None:
                raise ValueError(
                    f"{schema['table_name']}.{field['field_name']}: unknown FK table {target_table}"
                )
            target_names = {target["field_name"] for target in target_schema["fields"]}
            if target_field not in target_names:
                raise ValueError(
                    f"{schema['table_name']}.{field['field_name']}: unknown FK field {foreign_key}"
                )
        for constraint in schema.get("unique_constraints", []):
            if not isinstance(constraint, list) or len(constraint) < 2:
                raise ValueError(
                    f"{schema['table_name']}: unique constraints need at least two fields"
                )
            if any(name not in fields_by_name for name in constraint):
                raise ValueError(
                    f"{schema['table_name']}: unknown field in unique constraint {constraint}"
                )
    return schemas


def _normalized(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _blank_row(row: dict[str, Any], fields: list[dict[str, Any]]) -> bool:
    by_name = {field["field_name"]: field for field in fields}
    for name, raw in row.items():
        value = _normalized(raw)
        if not value:
            continue
        if by_name.get(name, {}).get("data_type") == "BOOLEAN" and value.lower() in BOOL_FALSE:
            continue
        return False
    return True


def _type_error(value: str, data_type: str) -> str | None:
    kind = data_type.upper()
    if kind == "STRING":
        return None
    if kind == "INTEGER":
        try:
            int(value)
        except ValueError:
            return "must be an integer"
    elif kind in {"DECIMAL", "NUMBER"}:
        try:
            number = float(value)
        except ValueError:
            return "must be numeric"
        if number != number or number in {float("inf"), float("-inf")}:
            return "must be finite"
    elif kind == "BOOLEAN" and value.lower() not in BOOL_TRUE | BOOL_FALSE:
        return "must be boolean"
    elif kind == "TIMESTAMP":
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return "must be ISO 8601 timestamp"
        if (
            parsed.tzinfo is None
            or parsed.utcoffset() is None
            or parsed.utcoffset().total_seconds() != 0
        ):
            return "must use UTC ISO 8601"
    elif kind == "URL":
        parsed_url = urlparse(value)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            return "must be an absolute HTTP(S) URL"
    elif kind == "JSON":
        try:
            json.loads(value)
        except json.JSONDecodeError:
            return "must contain valid JSON"
    return None


def _id_pattern(field: dict[str, Any]) -> re.Pattern[str] | None:
    rule = field.get("validation_rule") or ""
    match = ID_PATTERN_RE.search(rule)
    return re.compile(match.group(1)) if match else None


def validate_rows(schema_dir: Path, tables: dict[str, list[dict[str, Any]]]) -> list[str]:
    """Validate populated rows, keys, controlled values, and available FKs."""
    schemas = load_schemas(schema_dir)
    errors: list[str] = []
    clean_rows: dict[str, list[dict[str, str]]] = {}

    for table_name, rows in tables.items():
        schema = schemas.get(table_name)
        if schema is None:
            errors.append(f"{table_name}: no local schema")
            continue
        fields = schema["fields"]
        names = [field["field_name"] for field in fields]
        table_rows: list[dict[str, str]] = []
        seen_pk: set[str] = set()
        for row_number, raw_row in enumerate(rows, start=2):
            row = {str(key): _normalized(value) for key, value in raw_row.items()}
            if _blank_row(row, fields):
                continue
            table_rows.append(row)
            extra = set(row) - set(names)
            if extra:
                errors.append(f"{table_name} row {row_number}: unexpected columns {sorted(extra)}")
            for field in fields:
                name = field["field_name"]
                value = row.get(name, "")
                if field.get("required") and not value:
                    errors.append(f"{table_name} row {row_number}: {name} is required")
                    continue
                if not value:
                    continue
                type_problem = _type_error(value, field.get("data_type", "STRING"))
                if type_problem:
                    errors.append(f"{table_name} row {row_number}: {name} {type_problem}")
                allowed = field.get("allowed_values")
                if allowed and value not in allowed:
                    errors.append(f"{table_name} row {row_number}: {name} must be one of {allowed}")
                id_pattern = _id_pattern(field)
                if id_pattern and not id_pattern.fullmatch(value):
                    errors.append(
                        f"{table_name} row {row_number}: {name} does not match its ID format"
                    )
                rule = (field.get("validation_rule") or "").lower()
                if "positive integer" in rule:
                    try:
                        if int(value) <= 0:
                            errors.append(f"{table_name} row {row_number}: {name} must be positive")
                    except ValueError:
                        pass  # The general data type check reports invalid integers.
                if table_name == "SCRIPT_MASTER" and name == "Script_Checksum_SHA256":
                    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
                        errors.append(
                            f"{table_name} row {row_number}: Script_Checksum_SHA256 "
                            "must be 64 hex chars"
                        )

            pk = row.get(schema["primary_key"], "")
            if pk:
                if pk in seen_pk:
                    errors.append(f"{table_name} row {row_number}: duplicate primary key {pk}")
                seen_pk.add(pk)
        clean_rows[table_name] = table_rows

    for table_name, rows in clean_rows.items():
        schema = schemas[table_name]
        fields = schema["fields"]
        constraints = list(schema.get("unique_constraints", []))

        for constraint in constraints:
            seen: set[tuple[str, ...]] = set()
            for index, row in enumerate(rows, start=2):
                values = tuple(row.get(name, "") for name in constraint)
                if not all(values):
                    continue
                if values in seen:
                    errors.append(
                        f"{table_name} row {index}: duplicate unique key {constraint}={values}"
                    )
                seen.add(values)

        field_defs = {field["field_name"]: field for field in fields}
        for index, row in enumerate(rows, start=2):
            if table_name == "SCENE_DIALOGUE_LINES":
                speaker = row.get("Speaker_Type", "")
                character_id = row.get("Character_ID", "")
                if speaker == "CHARACTER" and not character_id:
                    errors.append(
                        f"{table_name} row {index}: Character_ID is required for CHARACTER speaker"
                    )
                elif speaker and speaker != "CHARACTER" and character_id:
                    errors.append(
                        f"{table_name} row {index}: Character_ID must be blank "
                        "for non-CHARACTER speaker"
                    )
            for field_name, field in field_defs.items():
                reference = field.get("foreign_key")
                value = row.get(field_name, "")
                if not reference or not value or field.get("foreign_key_status") == "DEFERRED FK":
                    continue
                target_table, _, target_field = reference.partition(".")
                if target_table not in tables:
                    errors.append(
                        f"{table_name} row {index}: missing table data for FK {reference}"
                    )
                    continue
                target_values = {
                    target.get(target_field, "") for target in clean_rows.get(target_table, [])
                }
                if value not in target_values:
                    errors.append(
                        f"{table_name} row {index}: orphan FK {field_name}={value} -> {reference}"
                    )

    approved_by_episode: set[str] = set()
    for index, row in enumerate(clean_rows.get("SCRIPT_MASTER", []), start=2):
        if row.get("Script_Status") != "APPROVED":
            continue
        episode_id = row.get("Episode_ID", "")
        if episode_id in approved_by_episode:
            errors.append(f"SCRIPT_MASTER row {index}: maximum one APPROVED Script per Episode")
        approved_by_episode.add(episode_id)

    return errors


def read_csv_bundle(
    csv_dir: Path, schemas: dict[str, dict[str, Any]], require_all: bool
) -> tuple[dict[str, list[dict[str, str]]], list[str]]:
    tables: dict[str, list[dict[str, str]]] = {}
    errors: list[str] = []
    for table_name, schema in schemas.items():
        csv_path = csv_dir / f"{table_name}.csv"
        if not csv_path.exists():
            if require_all:
                errors.append(f"{table_name}: missing CSV export {csv_path.name}")
            continue
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = reader.fieldnames or []
            expected = [field["field_name"] for field in schema["fields"]]
            if header != expected:
                errors.append(f"{table_name}: header mismatch; expected {expected}, got {header}")
                continue
            tables[table_name] = list(reader)
    return tables, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema-dir", type=Path, default=DEFAULT_SCHEMA_DIR)
    parser.add_argument("--csv-dir", type=Path, help="Directory with TABLE_NAME.csv exports")
    parser.add_argument(
        "--require-all-tables", action="store_true", help="Require CSVs for all 29 schema tables"
    )
    args = parser.parse_args(argv)
    try:
        schemas = load_schemas(args.schema_dir)
    except (OSError, ValueError) as exc:
        print(json.dumps({"errors": [str(exc)], "error_count": 1}, indent=2))
        return 1
    errors: list[str] = []
    tables: dict[str, list[dict[str, str]]] = {}
    if args.csv_dir:
        tables, errors = read_csv_bundle(args.csv_dir, schemas, args.require_all_tables)
        if tables:
            errors.extend(validate_rows(args.schema_dir, tables))
    elif args.require_all_tables:
        errors.append("--require-all-tables requires --csv-dir")
    print(
        json.dumps(
            {"schema_count": len(schemas), "errors": errors, "error_count": len(errors)}, indent=2
        )
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
