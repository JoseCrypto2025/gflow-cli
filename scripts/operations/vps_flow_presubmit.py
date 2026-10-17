"""Production-only, non-submitting Flow presubmit for a verified data snapshot.

This operator tool has no Phase G or Generate dependency. It is not a live
Sheets connector: a caller must provide a recent snapshot from the approved
master spreadsheet before D5 can run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from gflow_cli.v2_runtime.config import RuntimeSettings, load_runtime_settings
from gflow_cli.v2_runtime.google_flow import FlowPresubmitRequest, PersistedPromptVersion
from gflow_cli.v2_runtime.phase_e import InMemorySceneCatalog, Row, preflight_scene

SPREADSHEET_ID = "1wtoIBfeHNdGYK6cyhDfKPG6W3TnnfL4QDvCAtJBX27Q"
PROD_PROJECT_NAME = "AI Video Automation V2 - PROD"
MAX_SNAPSHOT_AGE = timedelta(minutes=5)
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SECRET_FIELD_RE = re.compile(
    r"password|token|cookie|secret|private[_-]?key|api[_-]?key|oauth", re.I
)


class PresubmitInputError(ValueError):
    """Stable, safe-to-report rejection for an invalid D5 input."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise PresubmitInputError("SNAPSHOT_TIMESTAMP_INVALID", "snapshot timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PresubmitInputError(
            "SNAPSHOT_TIMESTAMP_INVALID", "snapshot timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PresubmitInputError("SNAPSHOT_TIMESTAMP_INVALID", "snapshot timestamp must be UTC")
    return parsed.astimezone(UTC)


def load_snapshot(path: Path, *, now: datetime | None = None) -> dict[str, list[Row]]:
    """Load a small, mode-restricted snapshot; never echo paths or file contents."""
    if path.is_symlink() or not path.is_file():
        raise PresubmitInputError("SNAPSHOT_FILE_INVALID", "snapshot file is unavailable")
    try:
        file_stat = path.stat()
        if os.name == "posix" and stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise PresubmitInputError(
                "SNAPSHOT_PERMISSIONS_INVALID", "snapshot permissions are too broad"
            )
        if file_stat.st_size > 16 * 1024 * 1024:
            raise PresubmitInputError("SNAPSHOT_TOO_LARGE", "snapshot exceeds the size limit")
        document = json.loads(path.read_text(encoding="utf-8"))
    except PresubmitInputError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PresubmitInputError("SNAPSHOT_UNREADABLE", "snapshot could not be read") from exc
    if not isinstance(document, dict):
        raise PresubmitInputError("SNAPSHOT_SHAPE_INVALID", "snapshot structure is invalid")
    snapshot = cast(dict[str, Any], document)
    if _contains_secret_field(snapshot):
        raise PresubmitInputError(
            "SNAPSHOT_SECRET_FIELD", "snapshot contains a prohibited secret field"
        )
    if snapshot.get("spreadsheet_id") != SPREADSHEET_ID:
        raise PresubmitInputError(
            "SNAPSHOT_SOURCE_MISMATCH", "snapshot source is not the approved spreadsheet"
        )
    read_at = _utc_datetime(snapshot.get("read_at"))
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    age = current.astimezone(UTC) - read_at
    if age < timedelta(seconds=-30) or age > MAX_SNAPSHOT_AGE:
        raise PresubmitInputError(
            "SNAPSHOT_STALE", "snapshot is outside the permitted freshness window"
        )
    raw_tables = snapshot.get("tables")
    if not isinstance(raw_tables, dict):
        raise PresubmitInputError("SNAPSHOT_SHAPE_INVALID", "snapshot tables are missing")
    tables: dict[str, list[Row]] = {}
    for name, rows in cast(dict[str, object], raw_tables).items():
        if (
            not isinstance(name, str)
            or not isinstance(rows, list)
            or any(not isinstance(row, dict) for row in cast(list[object], rows))
        ):
            raise PresubmitInputError("SNAPSHOT_SHAPE_INVALID", "snapshot table rows are invalid")
        tables[name] = cast(list[Row], rows)
    return tables


def _contains_secret_field(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            (isinstance(key, str) and _SECRET_FIELD_RE.search(key)) or _contains_secret_field(item)
            for key, item in cast(dict[object, object], value).items()
        )
    if isinstance(value, list):
        return any(_contains_secret_field(item) for item in cast(list[object], value))
    return False


def build_prod_presubmit_request(
    tables: Mapping[str, list[Row]],
    *,
    scene_id: str,
    profile_id: str,
    settings: RuntimeSettings,
    expected_email: str,
) -> tuple[FlowPresubmitRequest, dict[str, str]]:
    """Resolve current PROD identity and require exact fresh Phase E approval."""
    if settings.environment != "production":
        raise PresubmitInputError("PRODUCTION_RUNTIME_REQUIRED", "runtime must be production")
    if not _EMAIL_RE.fullmatch(expected_email.strip()):
        raise PresubmitInputError(
            "EXPECTED_IDENTITY_REQUIRED", "explicit Google identity is required"
        )
    catalog = InMemorySceneCatalog(tables)
    profiles = [
        row
        for row in tables.get("GENERATION_PROFILE_MASTER", [])
        if row.get("Generation_Profile_ID") == profile_id
    ]
    if len(profiles) != 1:
        raise PresubmitInputError("PROD_PROFILE_INVALID", "one production profile must resolve")
    profile = profiles[0]
    if (
        str(profile.get("Environment", "")).upper() != "PROD"
        or str(profile.get("Status", "")).upper() != "ACTIVE"
    ):
        raise PresubmitInputError("PROD_PROFILE_INVALID", "profile must be active and production")
    account_id = str(profile.get("Provider_Account_ID", ""))
    accounts = [
        row
        for row in tables.get("PROVIDER_ACCOUNT_MASTER", [])
        if row.get("Provider_Account_ID") == account_id
    ]
    if len(accounts) != 1:
        raise PresubmitInputError("PROD_ACCOUNT_INVALID", "one production account must resolve")
    account = accounts[0]
    if (
        str(account.get("Environment", "")).upper() != "PROD"
        or str(account.get("Status", "")).upper() != "ACTIVE"
        or account.get("Auth_Profile_Key") != "GFLOW_PROD_AUTH"
        or account.get("Execution_Target") != "WEB_UI"
        or account.get("Base_URL") != "https://flow.google.com/"
        or not str(account.get("Provider_Project_ID", "")).strip()
    ):
        raise PresubmitInputError(
            "PROD_ACCOUNT_INVALID", "account is not a verified production Flow account"
        )
    project_id = str(account["Provider_Project_ID"]).strip()
    preflight = preflight_scene(catalog, scene_id, profile_id, "PROD")
    if (
        preflight.status != "PREFLIGHT_PASS"
        or not preflight.ready_for_provider_adapter
        or preflight.errors
    ):
        raise PresubmitInputError("PHASE_E_FAILED", "fresh production Phase E did not pass")
    if preflight.references:
        raise PresubmitInputError(
            "F5_REFERENCES_UNSUPPORTED", "production F5 supports no reference assets"
        )
    technical = preflight.technical_configuration
    if (
        technical.get("generation_mode") != "TEXT_TO_VIDEO"
        or technical.get("aspect_ratio") != "9:16"
        or technical.get("output_count") != 1
    ):
        raise PresubmitInputError(
            "F5_SETTINGS_UNSUPPORTED", "production profile is outside the verified F5 contract"
        )
    if str(preflight.resolved.get("provider_account_id", "")) != account_id:
        raise PresubmitInputError(
            "PROD_ACCOUNT_MISMATCH", "Phase E resolved a different provider account"
        )
    prompt_rows = [
        row
        for row in tables.get("SCENE_PROMPT_VERSION", [])
        if row.get("Scene_ID") == scene_id
        and row.get("Generation_Profile_ID") == profile_id
        and row.get("Prompt_Status") == "APPROVED"
    ]
    if len(prompt_rows) != 1:
        raise PresubmitInputError(
            "APPROVED_PROMPT_AMBIGUOUS", "exactly one current approved prompt is required"
        )
    approved = prompt_rows[0]
    expected = {
        "Scene_ID": preflight.scene_id,
        "Generation_Profile_ID": preflight.generation_profile_id,
        "Compiled_Prompt_Text": preflight.compiled_prompt,
        "Compiled_Negative_Prompt": preflight.compiled_negative_prompt or "",
        "Source_Context_SHA256": preflight.source_context_sha256,
        "Prompt_SHA256": preflight.prompt_sha256,
    }
    if any(approved.get(field) != value for field, value in expected.items()):
        raise PresubmitInputError(
            "APPROVED_PROMPT_STALE", "approved prompt differs from fresh Phase E"
        )
    prompt = PersistedPromptVersion(
        str(approved.get("Scene_Prompt_ID", "")),
        scene_id,
        profile_id,
        "APPROVED",
        str(approved.get("Compiled_Prompt_Text", "")),
        str(approved.get("Compiled_Negative_Prompt", "") or ""),
        str(approved.get("Source_Context_SHA256", "")),
        str(approved.get("Prompt_SHA256", "")),
    )
    provider_id = str(preflight.resolved.get("provider_id", ""))
    model_id = str(preflight.resolved.get("model_id", ""))
    models = [row for row in tables.get("MODEL_MASTER", []) if row.get("Model_ID") == model_id]
    if len(models) != 1 or not str(models[0].get("Model_Name", "")).strip():
        raise PresubmitInputError("MODEL_DISPLAY_NAME_MISSING", "model display name is unresolved")
    request = FlowPresubmitRequest(
        preflight,
        prompt,
        provider_id,
        account_id,
        model_id,
        str(profile.get("Generation_Mode", "")),
        project_id,
        settings.runtime_state_root,
        settings.temp_root,
        (),
    )
    return request, {model_id: str(models[0]["Model_Name"]).strip()}


async def run_prod_presubmit(
    request: FlowPresubmitRequest,
    *,
    model_display_names: Mapping[str, str],
    expected_email: str,
) -> dict[str, object]:
    """Run only F5; do not start authentication, submit, or generate media."""
    from gflow_cli.v2_runtime.live_flow import prepare_live_flow

    result = await prepare_live_flow(
        request,
        auth_profile_key="GFLOW_PROD_AUTH",
        project_name=PROD_PROJECT_NAME,
        model_display_names=model_display_names,
        expected_email=expected_email,
        headless=False,
    )
    if result.state != "FLOW_PRESUBMIT_READY":
        raise PresubmitInputError(
            "F5_NOT_READY", "non-submitting Flow presubmit did not reach READY"
        )
    return {
        "state": result.state,
        "provider_account_id": result.provider_account_id,
        "provider_project_id": result.provider_project_id,
        "prompt_sha256": result.prompt_sha256,
        "generate_present": result.generate_present,
        "generate_enabled": result.generate_enabled,
        "generate_clicked": False,
        "credits_used": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a production non-submitting Flow presubmit")
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--scene-id", required=True)
    parser.add_argument("--profile-id", required=True)
    args = parser.parse_args()
    expected_email = os.environ.get("GFLOW_EXPECTED_GOOGLE_EMAIL", "")
    try:
        settings = load_runtime_settings()
        tables = load_snapshot(args.snapshot)
        request, model_names = build_prod_presubmit_request(
            tables,
            scene_id=args.scene_id,
            profile_id=args.profile_id,
            settings=settings,
            expected_email=expected_email,
        )
        report = asyncio.run(
            run_prod_presubmit(
                request, model_display_names=model_names, expected_email=expected_email
            )
        )
    except PresubmitInputError as exc:
        print(json.dumps({"state": "HOLD", "reason_code": exc.code, "detail": str(exc)}))
        return 2
    except Exception:
        print(
            json.dumps(
                {
                    "state": "HOLD",
                    "reason_code": "PRESUBMIT_FAILED",
                    "detail": "presubmit failed safely",
                }
            )
        )
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
