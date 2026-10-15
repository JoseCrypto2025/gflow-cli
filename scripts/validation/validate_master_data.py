#!/usr/bin/env python3
"""Read-only validator for CSV exports of the V2 Google Sheets tables."""
import csv, json, sys
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas"
def load_schemas():
    result = {}
    for directory in (SCHEMAS / "master", SCHEMAS / "relation"):
        for path in sorted(directory.glob("*.schema.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            result[data["table_name"]] = data
    return result
def validate(csv_dir):
    schemas = load_schemas(); records = {}; errors = []
    for table, schema in schemas.items():
        path = Path(csv_dir) / (table + ".csv")
        if not path.exists(): continue
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream); headers = reader.fieldnames or []; rows = list(reader)
        expected = [field["field_name"] for field in schema["fields"]]
        if len(headers) != len(set(headers)): errors.append(table + ": duplicate headers")
        if headers != expected: errors.append(table + ": headers do not match schema")
        records[table] = rows; pk = schema["primary_key"]; seen = set()
        for row_number, row in enumerate(rows, start=2):
            if all(not (value or "").strip() for value in row.values()): continue
            for field in schema["fields"]:
                name = field["field_name"]; value = (row.get(name) or "").strip()
                if field["required"] and not value: errors.append(table + "!" + name + str(row_number) + ": required value is blank")
                allowed = field["allowed_values"]
                if value and allowed and value not in allowed: errors.append(table + "!" + name + str(row_number) + ": unsupported value " + repr(value))
                if value and field["data_type"] == "BOOLEAN" and value.upper() not in ("TRUE", "FALSE"): errors.append(table + "!" + name + str(row_number) + ": expected TRUE or FALSE")
                if value and field["data_type"] == "TIMESTAMP":
                    try: datetime.fromisoformat(value.replace("Z", "+00:00"))
                    except ValueError: errors.append(table + "!" + name + str(row_number) + ": invalid ISO 8601 timestamp")
            if pk:
                key = (row.get(pk) or "").strip()
                if key and key in seen: errors.append(table + "!" + pk + str(row_number) + ": duplicate primary key " + key)
                if key: seen.add(key)
    for table, rows in records.items():
        for field in schemas[table]["fields"]:
            if field["foreign_key_status"] != "ACTIVE FK" or not field["foreign_key"]: continue
            target, key = field["foreign_key"].split(".", 1)
            if target not in records: continue
            valid = {(row.get(key) or "").strip() for row in records[target]}
            for row_number, row in enumerate(rows, start=2):
                value = (row.get(field["field_name"]) or "").strip()
                if value and value not in valid: errors.append(table + "!" + field["field_name"] + str(row_number) + ": orphan reference to " + target + "." + key)
    entity_targets = {"CHARACTER":"CHARACTER_MASTER", "WARDROBE":"WARDROBE_MASTER", "LOCATION":"LOCATION_MASTER", "PROP":"PROP_MASTER", "STYLE":"VISUAL_STYLE_MASTER"}
    if "REFERENCE_ENTITY_MAP" in records:
        primary = set()
        for row_number, row in enumerate(records["REFERENCE_ENTITY_MAP"], start=2):
            kind = (row.get("Entity_Type") or "").strip(); entity = (row.get("Entity_ID") or "").strip(); target = entity_targets.get(kind)
            if target and entity and target in records:
                target_key = schemas[target]["primary_key"]
                valid = {(item.get(target_key) or "").strip() for item in records[target]}
                if entity not in valid: errors.append("REFERENCE_ENTITY_MAP!Entity_ID" + str(row_number) + ": polymorphic reference missing in " + target)
            if (row.get("Status") or "").strip() == "ACTIVE" and (row.get("Is_Primary") or "").strip().upper() == "TRUE":
                key = (kind, entity, (row.get("Reference_Role") or "").strip())
                if key in primary: errors.append("REFERENCE_ENTITY_MAP row " + str(row_number) + ": duplicate active primary for " + repr(key))
                primary.add(key)
    return errors
if __name__ == "__main__":
    if len(sys.argv) != 2: raise SystemExit("Usage: python validate_master_data.py <directory-containing-csv-exports>")
    issues = validate(sys.argv[1])
    print(json.dumps({"errors": issues, "error_count": len(issues)}, indent=2))
    raise SystemExit(1 if issues else 0)
