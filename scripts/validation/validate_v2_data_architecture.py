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
EXPECTED_COUNTS = {"master": 23, "transaction": 8, "relation": 3}
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

    provider_neutral_jobs = {"PRODUCTION_JOBS", "SCENE_JOBS", "TTS_JOBS", "FINAL_OUTPUT"}
    provider_specific = {
        "Google_Flow_Model",
        "Google_Project_ID",
        "Veo_Model",
        "Veo_Duration",
        "Fal_Model",
        "Phaya_Model",
        "Meta_Model_Name",
    }
    for table in provider_neutral_jobs.intersection(schemas):
        schema_fields = {field["field_name"] for field in schemas[table]["fields"]}
        leaked = {
            name
            for name in schema_fields
            if name in provider_specific
            or name in {"Provider_Name", "Model_Name"}
            or name.startswith(("Google_Flow_", "Veo_", "Fal_", "Phaya_", "Meta_"))
        }
        if leaked:
            raise ValueError(f"{table}: provider-specific fields are prohibited: {sorted(leaked)}")

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
            if not isinstance(constraint, list) or len(constraint) < 1:
                raise ValueError(
                    f"{schema['table_name']}: unique constraints need at least one field"
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
        offset = parsed.utcoffset()
        if parsed.tzinfo is None or offset is None or offset.total_seconds() != 0:
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

    errors.extend(_validate_production_execution(clean_rows))
    return errors


def _validate_production_execution(tables: dict[str, list[dict[str, str]]]) -> list[str]:
    errors: list[str] = []

    def rows(name: str) -> list[dict[str, str]]:
        return tables.get(name, [])

    def indexed(name: str, key: str) -> dict[str, dict[str, str]]:
        return {r.get(key, ""): r for r in rows(name) if r.get(key, "")}

    prompts = indexed("SCENE_PROMPT_VERSION", "Scene_Prompt_ID")
    scenes = indexed("SCENES", "Scene_ID")
    scripts = indexed("SCRIPT_MASTER", "Script_ID")
    profiles = indexed("GENERATION_PROFILE_MASTER", "Generation_Profile_ID")
    jobs = indexed("PRODUCTION_JOBS", "Production_Job_ID")
    accounts = indexed("PROVIDER_ACCOUNT_MASTER", "Provider_Account_ID")
    models = indexed("MODEL_MASTER", "Model_ID")
    voices = indexed("VOICE_MASTER", "Voice_ID")
    lines = indexed("SCENE_DIALOGUE_LINES", "Dialogue_Line_ID")
    hashes = {
        "Source_Context_SHA256",
        "Prompt_SHA256",
        "Scene_Set_SHA256",
        "Output_SHA256",
        "Input_Text_SHA256",
        "Audio_SHA256",
        "Assembly_Manifest_SHA256",
    }
    for table in (
        "SCENE_PROMPT_VERSION",
        "PRODUCTION_JOBS",
        "SCENE_JOBS",
        "TTS_JOBS",
        "FINAL_OUTPUT",
    ):
        for n, row in enumerate(rows(table), 2):
            for key in hashes.intersection(row):
                if row.get(key) and not re.fullmatch(r"[0-9a-fA-F]{64}", row[key]):
                    errors.append(f"{table} row {n}: {key} must be 64 hex chars")
    for table in ("SCENE_JOBS", "TTS_JOBS", "FINAL_OUTPUT"):
        for n, row in enumerate(rows(table), 2):
            for key in ("Output_Size_Bytes", "Audio_Size_Bytes"):
                if row.get(key):
                    try:
                        if int(row[key]) < 0:
                            errors.append(f"{table} row {n}: {key} must be >= 0")
                    except ValueError:
                        pass
            for key in (
                "Output_Duration_Sec",
                "Audio_Duration_Ms",
                "Duration_Sec",
                "Width",
                "Height",
            ):
                if row.get(key):
                    try:
                        if float(row[key]) <= 0:
                            errors.append(f"{table} row {n}: {key} must be positive")
                    except ValueError:
                        pass
    approved = set()
    for n, r in enumerate(rows("SCENE_PROMPT_VERSION"), 2):
        st = r.get("Prompt_Status", "")
        sid = r.get("Scene_ID", "")
        if st in {"COMPILED", "VALIDATED", "APPROVED", "SUPERSEDED"} and not r.get(
            "Compiled_Prompt_Text"
        ):
            errors.append(f"SCENE_PROMPT_VERSION row {n}: Compiled_Prompt_Text required for {st}")
        if st == "APPROVED" and not r.get("Approved_At"):
            errors.append(f"SCENE_PROMPT_VERSION row {n}: Approved_At required when APPROVED")
        if (
            st in {"VALIDATED", "APPROVED"}
            and r.get("Generation_Profile_ID") in profiles
            and profiles[r["Generation_Profile_ID"]].get("Status") != "ACTIVE"
        ):
            errors.append(f"SCENE_PROMPT_VERSION row {n}: profile must be ACTIVE")
        if st == "APPROVED":
            if sid in approved:
                errors.append(
                    f"SCENE_PROMPT_VERSION row {n}: maximum one APPROVED prompt per Scene"
                )
            approved.add(sid)
    for n, r in enumerate(rows("PRODUCTION_JOBS"), 2):
        st = r.get("Job_Status", "")
        if st in {"RUNNING", "COMPLETED", "FAILED"} and not r.get("Started_At"):
            errors.append(f"PRODUCTION_JOBS row {n}: Started_At required for {st}")
        if st in {"COMPLETED", "FAILED", "CANCELLED"} and not r.get("Completed_At"):
            errors.append(f"PRODUCTION_JOBS row {n}: Completed_At required for {st}")
        if st == "HOLD" and not r.get("Hold_Reason_Code"):
            errors.append(f"PRODUCTION_JOBS row {n}: Hold_Reason_Code required for HOLD")
        if st == "FAILED" and not r.get("Error_Code"):
            errors.append(f"PRODUCTION_JOBS row {n}: Error_Code required for FAILED")
        if st == "FAILED" and not r.get("Error_Message"):
            errors.append(f"PRODUCTION_JOBS row {n}: Error_Message required for FAILED")
    for table in ("SCENE_JOBS", "TTS_JOBS"):
        for n, r in enumerate(rows(table), 2):
            st = r.get("Job_Status", "")
            verified = r.get("Remote_Verified", "").lower() in BOOL_TRUE
            if r.get("Local_Cleanup_Status") == "CLEANED":
                if not verified:
                    errors.append(f"{table} row {n}: cleanup prohibited before remote verification")
                if not r.get("Local_Deleted_At"):
                    errors.append(f"{table} row {n}: Local_Deleted_At required when CLEANED")
            if st == "FAILED" and not r.get("Error_Code"):
                errors.append(f"{table} row {n}: Error_Code required for FAILED")
            if st == "FAILED" and table == "PRODUCTION_JOBS" and not r.get("Error_Message"):
                errors.append(f"{table} row {n}: Error_Message required for FAILED")
            if table == "SCENE_JOBS":
                parent = jobs.get(r.get("Production_Job_ID", ""), {})
                workspace = r.get("Temporary_Workspace", "").lower()
                if parent.get("Execution_Environment") == "PROD" and re.search(
                    r"[a-z]:\\|google drive|drivefs|drive for desktop", workspace
                ):
                    errors.append(f"{table} row {n}: PROD workspace must not use local paths")
            if st == "COMPLETED":
                needed = ["Remote_Verified_At", "Completed_At"] + (
                    [
                        "Remote_Output_URI",
                        "Output_SHA256",
                        "Output_Size_Bytes",
                        "Output_Duration_Sec",
                        "Generation_Completed_At",
                    ]
                    if table == "SCENE_JOBS"
                    else [
                        "Resolved_Voice_ID",
                        "Provider_Account_ID",
                        "Model_ID",
                        "Input_Text_SHA256",
                        "Remote_Audio_URI",
                        "Audio_SHA256",
                        "Audio_Size_Bytes",
                        "Audio_Duration_Ms",
                    ]
                )
                for key in needed:
                    if not r.get(key):
                        errors.append(f"{table} row {n}: {key} required when COMPLETED")
                if not verified:
                    errors.append(f"{table} row {n}: COMPLETED requires Remote_Verified=TRUE")
    for n, r in enumerate(rows("SCENE_JOBS"), 2):
        prompt = prompts.get(r.get("Scene_Prompt_ID", ""), {})
        job = jobs.get(r.get("Production_Job_ID", ""), {})
        scene = scenes.get(prompt.get("Scene_ID", ""), {})
        script = scripts.get(job.get("Script_ID", ""), {})
        if scene and script and scene.get("Script_ID") != script.get("Script_ID"):
            errors.append(
                f"SCENE_JOBS row {n}: Scene prompt does not belong to Production Job Script"
            )
    for n, r in enumerate(rows("TTS_JOBS"), 2):
        st = r.get("Job_Status", "")
        v = voices.get(r.get("Resolved_Voice_ID", ""), {})
        a = accounts.get(r.get("Provider_Account_ID", ""), {})
        m = models.get(r.get("Model_ID", ""), {})
        if st in {"QUEUED", "RUNNING", "UPLOADING", "REMOTE_VERIFYING", "COMPLETED"}:
            for key in ("Resolved_Voice_ID", "Provider_Account_ID", "Model_ID"):
                if not r.get(key):
                    errors.append(f"TTS_JOBS row {n}: {key} required before execution")
        if v and a and m:
            if (
                len({v.get("Provider_ID", ""), a.get("Provider_ID", ""), m.get("Provider_ID", "")})
                > 1
            ):
                errors.append(f"TTS_JOBS row {n}: voice, account, and model providers must match")
            gt = m.get("Generation_Type", "").upper()
            if st in {
                "QUEUED",
                "RUNNING",
                "UPLOADING",
                "REMOTE_VERIFYING",
                "COMPLETED",
            } and not any(x in gt for x in ("TTS", "TEXT_TO_SPEECH", "AUDIO")):
                errors.append(f"TTS_JOBS row {n}: model must support TTS/audio generation")
        line = lines.get(r.get("Dialogue_Line_ID", ""), {})
        job = jobs.get(r.get("Production_Job_ID", ""), {})
        scene = scenes.get(line.get("Scene_ID", ""), {})
        script = scripts.get(job.get("Script_ID", ""), {})
        if scene and script and scene.get("Script_ID") != script.get("Script_ID"):
            errors.append(
                f"TTS_JOBS row {n}: Dialogue Line does not belong to Production Job Script"
            )
    ready = set()
    primary = set()
    for n, r in enumerate(rows("FINAL_OUTPUT"), 2):
        if r.get("Output_Status") == "READY":
            if (
                r.get("Validation_Status") != "PASS"
                or r.get("Remote_Verified", "").lower() not in BOOL_TRUE
            ):
                errors.append(
                    f"FINAL_OUTPUT row {n}: READY requires PASS validation and remote verification"
                )
            for key in ("Remote_Output_URI", "Output_SHA256", "Output_Size_Bytes"):
                if not r.get(key):
                    errors.append(f"FINAL_OUTPUT row {n}: {key} required for READY")
        if (
            r.get("Output_Type") == "FINAL_VIDEO"
            and r.get("Is_Primary", "").lower() in BOOL_TRUE
            and r.get("Output_Status") != "ARCHIVED"
        ):
            jid = r.get("Production_Job_ID", "")
            if jid in primary:
                errors.append(f"FINAL_OUTPUT row {n}: duplicate active primary FINAL_VIDEO")
            primary.add(jid)
            if (
                r.get("Validation_Status") == "PASS"
                and r.get("Output_Status") == "READY"
                and r.get("Remote_Verified", "").lower() in BOOL_TRUE
            ):
                ready.add(jid)
    for n, r in enumerate(rows("PRODUCTION_JOBS"), 2):
        if r.get("Job_Status") == "COMPLETED" and r.get("Production_Job_ID") not in ready:
            errors.append(
                f"PRODUCTION_JOBS row {n}: COMPLETED requires a verified primary FINAL_VIDEO"
            )
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
        "--require-all-tables", action="store_true", help="Require CSVs for all 34 schema tables"
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
