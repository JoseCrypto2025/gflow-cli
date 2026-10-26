#!/usr/bin/env python3
"""Validate CSV exports for the 23 V2 master tables and provider-neutral rules."""
import csv, json, math, re, sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "schemas" / "master"
EXPECTED_MASTER_TABLES = 23
SECRET_RE = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._-]{10,}|"
    r"(?:api[_-]?key|password|access[_-]?token|oauth[_-]?token)\s*[:=]\s*\S+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"\b(?:sk-[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,})\b)"
)
SENSITIVE_HEADER = re.compile(r"(?i)(?:password|api[_-]?key|access[_-]?token|oauth[_-]?token|cookie|private[_-]?key|browser[_-]?session)")
ID_PATTERNS = {
    "PROVIDER_ACCOUNT_MASTER": re.compile(r"^PVA_[0-9]{4}$"),
    "MODEL_PARAMETER_MAP": re.compile(r"^MPM_[0-9]{6}$"),
    "GENERATION_PROFILE_MASTER": re.compile(r"^GPR_[0-9]{4}$"),
    "PROVIDER_COST_MASTER": re.compile(r"^PCT_[0-9]{6}$"),
}

def load_schemas():
    schemas = {}
    for path in sorted(SCHEMAS.glob("*.schema.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        schemas[data["table_name"]] = data
    if len(schemas) != EXPECTED_MASTER_TABLES:
        raise ValueError(f"Expected {EXPECTED_MASTER_TABLES} master schemas, found {len(schemas)}")
    return schemas

def value(row, key):
    return (row.get(key) or "").strip()

def headers_match_schema(headers, expected, optional_fields):
    if headers == expected:
        return True
    if len(headers) > len(expected) or expected[:len(headers)] != headers:
        return False
    missing_trailing = expected[len(headers):]
    return set(missing_trailing).issubset(optional_fields)

def is_active(row):
    return value(row, "Status").upper() == "ACTIVE"

CHECKBOX_DEFAULT_FIELDS = {"Is_Default", "Is_Required"}
def blank_row(row):
    return all(not (v or "").strip() or (k in CHECKBOX_DEFAULT_FIELDS and (v or "").strip().upper() == "FALSE") for k, v in row.items())

def parse_json(text, table, field, rownum, errors, array=False):
    try:
        obj = json.loads(text)
        if array and not isinstance(obj, list):
            errors.append(f"{table}!{field}{rownum}: expected a JSON array")
        return obj
    except (json.JSONDecodeError, TypeError):
        errors.append(f"{table}!{field}{rownum}: invalid JSON")
        return None

def parse_time(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))

def validate(csv_dir):
    schemas = load_schemas()
    records = {}
    errors = []
    csv_dir = Path(csv_dir)

    for table, schema in schemas.items():
        path = csv_dir / (table + ".csv")
        if not path.is_file():
            errors.append(f"{table}: missing CSV export")
            continue
        with path.open(newline="", encoding="utf-8-sig") as stream:
            reader = csv.DictReader(stream)
            headers = reader.fieldnames or []
            rows = list(reader)
        expected = [f["field_name"] for f in schema["fields"]]
        if len(headers) != len(set(headers)):
            errors.append(f"{table}: duplicate headers")
        optional_fields = {
            field["field_name"] for field in schema["fields"] if field.get("required") is False
        }
        if not headers_match_schema(headers, expected, optional_fields):
            errors.append(f"{table}: headers do not match schema")
        records[table] = rows
        seen = set()
        pk = schema["primary_key"]

        for rownum, row in enumerate(rows, start=2):
            if blank_row(row):
                continue
            for header, cell in row.items():
                if cell and SENSITIVE_HEADER.search(header or ""):
                    errors.append(f"{table}!{header}{rownum}: secret-bearing column is prohibited")
                if cell and SECRET_RE.search(cell):
                    errors.append(f"{table}!{header}{rownum}: secret-like value is prohibited")
            for spec in schema["fields"]:
                name = spec["field_name"]
                text = value(row, name)
                dtype = spec["data_type"]
                if spec["required"] and not text:
                    errors.append(f"{table}!{name}{rownum}: required value is blank")
                if text and spec["allowed_values"] and text not in spec["allowed_values"]:
                    errors.append(f"{table}!{name}{rownum}: unsupported value {text!r}")
                if text and dtype == "BOOLEAN" and text.upper() not in ("TRUE", "FALSE"):
                    errors.append(f"{table}!{name}{rownum}: expected TRUE or FALSE")
                if text and dtype in ("INTEGER", "DECIMAL", "NUMBER"):
                    try:
                        number = float(text)
                        if not math.isfinite(number):
                            raise ValueError()
                        if dtype == "INTEGER" and not number.is_integer():
                            errors.append(f"{table}!{name}{rownum}: expected an integer")
                        if table == "PROVIDER_COST_MASTER" and name == "Cost_Rate" and number < 0:
                            errors.append(f"{table}!{name}{rownum}: cost rate must be non-negative")
                        if table == "GENERATION_PROFILE_MASTER" and name == "Default_Output_Count" and number <= 0:
                            errors.append(f"{table}!{name}{rownum}: output count must be positive")
                    except ValueError:
                        errors.append(f"{table}!{name}{rownum}: expected a finite number")
                if text and dtype == "TIMESTAMP":
                    try:
                        if parse_time(text).tzinfo is None:
                            errors.append(f"{table}!{name}{rownum}: timestamp must include a timezone")
                    except ValueError:
                        errors.append(f"{table}!{name}{rownum}: invalid ISO 8601 timestamp")
                if text and dtype == "JSON":
                    parsed = parse_json(text, table, name, rownum, errors, array=(name == "Allowed_Values_JSON"))
                    if name == "Default_Value_JSON" and parsed is not None:
                        allowed_text = value(row, "Allowed_Values_JSON")
                        if allowed_text:
                            allowed = parse_json(allowed_text, table, "Allowed_Values_JSON", rownum, errors, array=True)
                            if isinstance(allowed, list) and parsed not in allowed:
                                errors.append(f"{table}!Default_Value_JSON{rownum}: default is not in allowed values")
                if table == "PROVIDER_COST_MASTER" and name == "Currency" and text and not re.fullmatch(r"[A-Z]{3}", text):
                    errors.append(f"{table}!{name}{rownum}: expected an uppercase three-letter currency code")
                if table == "PROVIDER_ACCOUNT_MASTER" and name == "Auth_Profile_Key" and text:
                    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", text):
                        errors.append(f"{table}!{name}{rownum}: logical uppercase auth profile key required")
                if table == "MODEL_PARAMETER_MAP" and name == "Adapter_Parameter_Key" and text:
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]*", text):
                        errors.append(f"{table}!{name}{rownum}: adapter key must be semantic, not a selector or coordinate")
            if pk:
                key = value(row, pk)
                if key and key in seen:
                    errors.append(f"{table}!{pk}{rownum}: duplicate primary key {key}")
                if key:
                    seen.add(key)
                    pattern = ID_PATTERNS.get(table)
                    if pattern and not pattern.fullmatch(key):
                        errors.append(f"{table}!{pk}{rownum}: invalid ID format {key!r}")

    # Active foreign keys and optional foreign keys when populated.
    for table, rows in records.items():
        for spec in schemas[table]["fields"]:
            fk = spec.get("foreign_key")
            if spec.get("foreign_key_status") != "ACTIVE FK" or not fk:
                continue
            target, key = fk.split(".", 1)
            if target not in records:
                continue
            valid = {value(row, key) for row in records[target]}
            for rownum, row in enumerate(rows, start=2):
                ref = value(row, spec["field_name"])
                if ref and ref not in valid:
                    errors.append(f"{table}!{spec['field_name']}{rownum}: orphan reference to {target}.{key}")

    # Retain original polymorphic reference checks.
    targets = {"CHARACTER":"CHARACTER_MASTER","WARDROBE":"WARDROBE_MASTER","LOCATION":"LOCATION_MASTER","PROP":"PROP_MASTER","STYLE":"VISUAL_STYLE_MASTER"}
    if "REFERENCE_ENTITY_MAP" in records:
        primaries = set()
        for rownum, row in enumerate(records["REFERENCE_ENTITY_MAP"], start=2):
            kind, entity = value(row,"Entity_Type"), value(row,"Entity_ID")
            target = targets.get(kind)
            if target and entity and target in records:
                pk = schemas[target]["primary_key"]
                if entity not in {value(item,pk) for item in records[target]}:
                    errors.append(f"REFERENCE_ENTITY_MAP!Entity_ID{rownum}: polymorphic reference missing in {target}")
            if is_active(row) and value(row,"Is_Primary").upper() == "TRUE":
                key = (kind,entity,value(row,"Reference_Role"))
                if key in primaries:
                    errors.append(f"REFERENCE_ENTITY_MAP row {rownum}: duplicate active primary for {key!r}")
                primaries.add(key)

    # Parse active MODEL_CAPABILITY_MAP JSON and build supported mode/resolution indexes.
    mode_caps, resolution_caps = {}, {}
    for rownum, row in enumerate(records.get("MODEL_CAPABILITY_MAP",[]), start=2):
        raw = value(row,"Capability_Value")
        if not raw or value(row,"Unit").upper() != "JSON":
            continue
        parsed = parse_json(raw,"MODEL_CAPABILITY_MAP","Capability_Value",rownum,errors)
        if not isinstance(parsed,dict):
            errors.append(f"MODEL_CAPABILITY_MAP!Capability_Value{rownum}: expected a JSON object")
            continue
        if not is_active(row):
            continue
        model_id, kind = value(row,"Model_ID"), value(row,"Capability_Type")
        if kind == "GENERATION_MODE" and parsed.get("mode"):
            mode_caps[(model_id,parsed["mode"])] = parsed
        elif kind == "RESOLUTION":
            resolution_caps.setdefault(model_id,[]).append(parsed)

    account_rows = {value(r,"Provider_Account_ID"):r for r in records.get("PROVIDER_ACCOUNT_MASTER",[]) if value(r,"Provider_Account_ID")}
    model_rows = {value(r,"Model_ID"):r for r in records.get("MODEL_MASTER",[]) if value(r,"Model_ID")}
    prompt_rows = {value(r,"Prompt_Template_ID"):r for r in records.get("PROMPT_TEMPLATE_MASTER",[]) if value(r,"Prompt_Template_ID")}

    # One active default provider account per provider/environment.
    account_defaults=set()
    for rownum,row in enumerate(records.get("PROVIDER_ACCOUNT_MASTER",[]),start=2):
        if is_active(row) and value(row,"Is_Default").upper()=="TRUE":
            key=(value(row,"Provider_ID"),value(row,"Environment"))
            if key in account_defaults: errors.append(f"PROVIDER_ACCOUNT_MASTER row {rownum}: duplicate active default for {key!r}")
            account_defaults.add(key)

    # One active parameter mapping per model/mode/canonical parameter.
    mapping_keys=set()
    for rownum,row in enumerate(records.get("MODEL_PARAMETER_MAP",[]),start=2):
        if is_active(row):
            key=(value(row,"Model_ID"),value(row,"Generation_Mode"),value(row,"Canonical_Parameter"))
            if key in mapping_keys: errors.append(f"MODEL_PARAMETER_MAP row {rownum}: duplicate active logical mapping {key!r}")
            mapping_keys.add(key)
            cap=mode_caps.get((key[0],key[1]))
            if cap is None or cap.get("supported") is not True:
                errors.append(f"MODEL_PARAMETER_MAP row {rownum}: mode lacks active supported model capability")

    # Profile-account/model/provider consistency and capability enforcement.
    profile_defaults=set()
    for rownum,row in enumerate(records.get("GENERATION_PROFILE_MASTER",[]),start=2):
        account_id,model_id,mode=(value(row,k) for k in ("Provider_Account_ID","Model_ID","Generation_Mode"))
        account,model=account_rows.get(account_id),model_rows.get(model_id)
        provider_id=value(account or {},"Provider_ID")
        if account and model and provider_id != value(model,"Provider_ID"):
            errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: model provider does not match provider account")
        if account and value(row,"Environment") != value(account,"Environment"):
            errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: environment does not match provider account")
        prompt=prompt_rows.get(value(row,"Prompt_Template_ID"))
        if prompt and provider_id and value(prompt,"Provider_ID") and value(prompt,"Provider_ID") != provider_id:
            errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: prompt provider does not match provider account")
        if prompt and model_id and value(prompt,"Model_ID") and value(prompt,"Model_ID") != model_id:
            errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: prompt model does not match profile model")

        if model_id and mode:
            cap=mode_caps.get((model_id,mode))
            if cap is None or cap.get("supported") is not True:
                errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: mode lacks active supported model capability")
            else:
                duration=value(row,"Default_Duration_Sec")
                if duration:
                    try:
                        number=float(duration)
                        allowed=cap.get("duration_sec")
                        maximum=cap.get("maximum_duration_sec")
                        if isinstance(allowed,list):
                            if number not in [float(x) for x in allowed]: errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: unsupported duration")
                        elif maximum is not None:
                            if number > float(maximum): errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: duration exceeds model/mode maximum")
                        else:
                            errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: duration capability is not enumerated")
                    except (ValueError,TypeError):
                        errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: duration cannot be capability-validated")
                aspect=value(row,"Default_Aspect_Ratio")
                if aspect:
                    allowed=cap.get("aspect_ratios")
                    if isinstance(allowed,list):
                        if aspect not in [str(x) for x in allowed]: errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: unsupported aspect ratio")
                    elif aspect != allowed:
                        errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: aspect ratio capability is not explicitly enumerated")
                resolution=value(row,"Default_Resolution")
                if resolution:
                    def leaf_strings(obj):
                        if isinstance(obj,str): return {obj}
                        if isinstance(obj,dict): return set().union(*(leaf_strings(v) for v in obj.values())) if obj else set()
                        if isinstance(obj,list): return set().union(*(leaf_strings(v) for v in obj)) if obj else set()
                        return set()
                    supported=set()
                    for capability in resolution_caps.get(model_id,[]): supported.update(leaf_strings(capability))
                    if resolution not in supported: errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: resolution unsupported or not explicitly declared")
        if is_active(row) and value(row,"Is_Default").upper()=="TRUE":
            key=(value(row,"Purpose"),value(row,"Environment"))
            if key in profile_defaults: errors.append(f"GENERATION_PROFILE_MASTER row {rownum}: duplicate active default for purpose/environment {key!r}")
            profile_defaults.add(key)

    # Cost rows are source-backed, effective-dated, and provider-consistent.
    profile_rows={value(row,"Generation_Profile_ID"):row for row in records.get("GENERATION_PROFILE_MASTER",[]) if value(row,"Generation_Profile_ID")}
    for rownum,row in enumerate(records.get("PROVIDER_COST_MASTER",[]),start=2):
        provider_id,account_id,model_id=(value(row,k) for k in ("Provider_ID","Provider_Account_ID","Model_ID"))
        account,model=account_rows.get(account_id),model_rows.get(model_id)
        if account and provider_id != value(account,"Provider_ID"):
            errors.append(f"PROVIDER_COST_MASTER row {rownum}: provider does not match provider account")
        if model and provider_id != value(model,"Provider_ID"):
            errors.append(f"PROVIDER_COST_MASTER row {rownum}: provider does not match model")
        profile_id=value(row,"Generation_Profile_ID")
        if profile_id:
            profile=profile_rows.get(profile_id)
            if profile is None:
                errors.append(f"PROVIDER_COST_MASTER row {rownum}: generation profile is missing")
            else:
                scope_fields=(
                    ("Provider_Account_ID","Provider_Account_ID"),
                    ("Model_ID","Model_ID"),
                    ("Generation_Mode","Generation_Mode"),
                    ("Duration_Sec","Default_Duration_Sec"),
                    ("Aspect_Ratio","Default_Aspect_Ratio"),
                    ("Resolution","Default_Resolution"),
                    ("Audio_Mode","Audio_Mode"),
                    ("Output_Count","Default_Output_Count"),
                    ("Quality_Tier","Quality_Tier"),
                )
                for cost_field,profile_field in scope_fields:
                    cost_value,profile_value=value(row,cost_field),value(profile,profile_field)
                    if not cost_value or not profile_value:
                        errors.append(f"PROVIDER_COST_MASTER row {rownum}: profile quote scope is incomplete ({cost_field})")
                    elif cost_field in {"Duration_Sec","Output_Count"}:
                        try:
                            if float(cost_value) != float(profile_value):
                                errors.append(f"PROVIDER_COST_MASTER row {rownum}: {cost_field} differs from generation profile")
                        except ValueError:
                            errors.append(f"PROVIDER_COST_MASTER row {rownum}: {cost_field} is invalid")
                    elif cost_value != profile_value:
                        errors.append(f"PROVIDER_COST_MASTER row {rownum}: {cost_field} differs from generation profile")
        unit,currency=value(row,"Cost_Unit"),value(row,"Currency")
        if unit and unit!="CREDIT" and not currency:
            errors.append(f"PROVIDER_COST_MASTER row {rownum}: currency required unless Cost_Unit is CREDIT")
        start,end=value(row,"Effective_From"),value(row,"Effective_To")
        if start and end:
            try:
                if parse_time(end) <= parse_time(start):
                    errors.append(f"PROVIDER_COST_MASTER row {rownum}: Effective_To must follow Effective_From")
            except ValueError:
                pass
    return errors

if __name__=="__main__":
    if len(sys.argv)!=2:
        raise SystemExit("Usage: python validate_master_data.py <directory-containing-all-23-master-csv-exports>")
    try:
        problems=validate(sys.argv[1])
    except Exception as exc:
        print(json.dumps({"errors":[str(exc)],"error_count":1},indent=2))
        raise SystemExit(1)
    print(json.dumps({"errors":problems,"error_count":len(problems)},indent=2))
    raise SystemExit(1 if problems else 0)
