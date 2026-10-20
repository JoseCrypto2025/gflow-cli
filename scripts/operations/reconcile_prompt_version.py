"""Operator-only immutable prompt-version lifecycle for the V2 master Sheet."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from gflow_cli.v2_runtime.config import load_runtime_settings
from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    GoogleSheetsApiTransport,
    GoogleSheetsReadError,
    GoogleSheetsSnapshotSource,
    GoogleWorkspaceCredentialProvider,
)
from gflow_cli.v2_runtime.google_sheets_transactions import (
    GoogleSheetsPromptVersionApiTransport,
    OptimisticGoogleSheetsPromptVersionStore,
)
from gflow_cli.v2_runtime.phase_e import InMemorySceneCatalog, preflight_scene
from gflow_cli.v2_runtime.prompt_approval import require_single_current_approval
from gflow_cli.v2_runtime.prompt_version_lifecycle import (
    PromptVersionLifecycle,
    PromptVersionLifecycleError,
    current_approved_rows,
)


def _read_snapshot(credential_path: Path, schema_dir: Path):
    repository_root = Path(__file__).resolve().parents[2]
    provider = GoogleWorkspaceCredentialProvider(credential_path, repository_root=repository_root)
    transport = GoogleSheetsApiTransport(provider)
    try:
        snapshot = GoogleSheetsSnapshotSource(transport, schema_dir).read()
    finally:
        transport.close()
    if (
        snapshot.spreadsheet_id != APPROVED_SPREADSHEET_ID
        or snapshot.source != "LIVE_GOOGLE_SHEETS"
    ):
        raise PromptVersionLifecycleError(
            "SNAPSHOT_UNTRUSTED", "Phase E source is not the approved live Sheet"
        )
    return snapshot


def _profile_environment(
    tables: Mapping[str, Sequence[Mapping[str, object]]], profile_id: str
) -> str:
    """Resolve Phase E environment from one active Master profile, never caller defaults."""
    profiles = [
        row
        for row in tables.get("GENERATION_PROFILE_MASTER", ())
        if row.get("Generation_Profile_ID") == profile_id
    ]
    if not profiles:
        raise PromptVersionLifecycleError(
            "PROFILE_NOT_FOUND", "generation profile was not found in the live Master snapshot"
        )
    if len(profiles) != 1:
        raise PromptVersionLifecycleError(
            "PROFILE_DUPLICATE", "generation profile is not unique in the live Master snapshot"
        )
    profile = profiles[0]
    if profile.get("Status") != "ACTIVE":
        raise PromptVersionLifecycleError(
            "PROFILE_INACTIVE", "generation profile is not ACTIVE"
        )
    raw_environment = profile.get("Environment")
    if not isinstance(raw_environment, str) or not raw_environment.strip():
        raise PromptVersionLifecycleError(
            "PROFILE_ENVIRONMENT_MISSING", "generation profile environment is blank"
        )
    environment = raw_environment.strip()
    if environment not in {"TEST", "PROD"}:
        raise PromptVersionLifecycleError(
            "PROFILE_ENVIRONMENT_UNSUPPORTED",
            "generation profile environment must be TEST or PROD",
        )
    return environment


def _fresh_phase_e(credential_path: Path, schema_dir: Path, scene_id: str, profile_id: str):
    snapshot = _read_snapshot(credential_path, schema_dir)
    environment = _profile_environment(snapshot.tables, profile_id)
    return preflight_scene(
        InMemorySceneCatalog(snapshot.tables), scene_id, profile_id, environment
    )


def _safe_preview(preview) -> dict[str, object]:
    return {
        "spreadsheet_id": APPROVED_SPREADSHEET_ID,
        "scene_id": preview.candidate["Scene_ID"],
        "generation_profile_id": preview.candidate["Generation_Profile_ID"],
        "new_prompt_id": preview.scene_prompt_id,
        "new_prompt_version": preview.prompt_version,
        "prompt_sha256": preview.prompt_sha256,
        "source_context_sha256": preview.source_context_sha256,
        "initial_status": preview.initial_status,
        "current_prompt_id": preview.previous_approved_id,
        "expected_current_transition": {
            "from": "APPROVED",
            "to": "SUPERSEDED",
            "current_semantics": "derived from Prompt_Status; schema has no Is_Current column",
        },
        "expected_new_transition": ["DRAFT", "VALIDATED", "APPROVED"],
        "write_performed": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create, validate, or approve an immutable V2 prompt version"
    )
    parser.add_argument(
        "--action", choices=("preview", "create", "validate", "approve", "verify"), required=True
    )
    parser.add_argument("--scene-id")
    parser.add_argument("--profile-id")
    parser.add_argument("--prompt-id")
    parser.add_argument("--expected-current-id")
    parser.add_argument(
        "--execute", action="store_true", help="allow the selected Sheet write operation"
    )
    parser.add_argument(
        "--credential-path", default=os.environ.get("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", "")
    )
    args = parser.parse_args()

    if args.action in {"preview", "create", "verify"} and not (args.scene_id and args.profile_id):
        parser.error("--scene-id and --profile-id are required for preview/create/verify")
    if args.action in {"validate", "approve"} and not args.prompt_id:
        parser.error("--prompt-id is required for validate/approve")
    if args.action in {"preview", "create", "approve"} and not args.expected_current_id:
        parser.error("--expected-current-id is required to guard the current approval")
    if args.action in {"create", "validate", "approve"} and not args.execute:
        parser.error("write actions require --execute; preview and verify never write")
    if args.action == "verify" and args.execute:
        parser.error("verify is read-only and does not accept --execute")
    if not args.credential_path:
        parser.error("canonical Google Workspace credential path is required")

    schema_dir = Path(__file__).resolve().parents[2] / "schemas"
    credential_path = Path(args.credential_path)
    settings = load_runtime_settings()
    provider = GoogleWorkspaceCredentialProvider(
        credential_path, repository_root=Path(__file__).resolve().parents[2]
    )
    write_transport = GoogleSheetsPromptVersionApiTransport(provider)
    try:
        store = OptimisticGoogleSheetsPromptVersionStore(
            write_transport,
            schema_dir,
            writer_lock_root=settings.runtime_state_root,
        )
        service = PromptVersionLifecycle(
            store,
            phase_e=lambda scene, profile: _fresh_phase_e(
                credential_path, schema_dir, scene, profile
            ),
        )
        if args.action == "verify":
            rows = store.read_prompt_rows()
            matching = [
                row
                for row in rows
                if row.get("Scene_ID") == args.scene_id
                and row.get("Generation_Profile_ID") == args.profile_id
            ]
            current = current_approved_rows(
                matching,
                scene_id=args.scene_id,
                generation_profile_id=args.profile_id,
            )
            require_single_current_approval(
                matching,
                scene_id=args.scene_id,
                generation_profile_id=args.profile_id,
            )
            current_ids = {str(row["Scene_Prompt_ID"]) for row in current}
            result = {
                "scene_id": args.scene_id,
                "generation_profile_id": args.profile_id,
                "current_approved_count": len(current),
                "versions": [
                    {
                        "prompt_id": row["Scene_Prompt_ID"],
                        "version": row["Prompt_Version"],
                        "status": row["Prompt_Status"],
                        "is_current_derived": str(row["Scene_Prompt_ID"]) in current_ids,
                        "prompt_sha256": row["Prompt_SHA256"],
                        "source_context_sha256": row["Source_Context_SHA256"],
                    }
                    for row in matching
                ],
                "write_performed": False,
            }
        elif args.action == "preview":
            result = _safe_preview(service.preview(args.scene_id, args.profile_id))
            if result["current_prompt_id"] != args.expected_current_id:
                raise PromptVersionLifecycleError(
                    "CONCURRENT_MODIFICATION", "current approved prompt changed"
                )
        elif args.action == "create":
            row = service.create_prompt_version(
                args.scene_id, args.profile_id, expected_current_id=args.expected_current_id
            )
            result = {
                "created": True,
                "prompt_id": row["Scene_Prompt_ID"],
                "version": row["Prompt_Version"],
                "status": row["Prompt_Status"],
                "prompt_sha256": row["Prompt_SHA256"],
                "source_context_sha256": row["Source_Context_SHA256"],
            }
        elif args.action == "validate":
            row = service.validate_prompt_version(args.prompt_id)
            result = {
                "validated": True,
                "prompt_id": row["Scene_Prompt_ID"],
                "version": row["Prompt_Version"],
                "status": row["Prompt_Status"],
                "prompt_sha256": row["Prompt_SHA256"],
                "source_context_sha256": row["Source_Context_SHA256"],
            }
        else:
            row = service.approve_prompt_version(
                args.prompt_id, expected_previous_id=args.expected_current_id
            )
            result = {
                "approved": True,
                "prompt_id": row["Scene_Prompt_ID"],
                "version": row["Prompt_Version"],
                "status": row["Prompt_Status"],
                "current_prompt_id": row["Scene_Prompt_ID"],
                "expected_previous_id": args.expected_current_id,
            }
    except PromptVersionLifecycleError as exc:
        print(
            json.dumps(
                {"state": "HOLD", "reason_code": exc.code, "detail": str(exc)}, sort_keys=True
            )
        )
        return 2
    except Exception as exc:
        detail = (
            str(exc)
            if isinstance(exc, GoogleSheetsReadError)
            else "prompt lifecycle operation failed safely"
        )
        print(
            json.dumps(
                {
                    "state": "HOLD",
                    "reason_code": "PROMPT_LIFECYCLE_FAILED",
                    "detail": detail,
                    "error_type": type(exc).__name__,
                },
                sort_keys=True,
            )
        )
        return 2
    finally:
        write_transport.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
