#!/usr/bin/env python3
"""One-shot, read-only pricing/config inspection for the pinned PROD Flow profile."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from gflow_cli.v2_runtime.config import load_runtime_settings  # noqa: E402
from gflow_cli.v2_runtime.flow_pricing_inspector import (  # noqa: E402
    EXPECTED_PROJECT_ID,
    EXPECTED_PROVIDER_ACCOUNT_ID,
    inspect_flow_pricing_read_only,
)


def _source_commit() -> str | None:
    try:
        status = subprocess.run(
            ["git", "-C", str(_ROOT), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if not status.stdout.strip():
            result = subprocess.run(
                ["git", "-C", str(_ROOT), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            commit = result.stdout.strip().lower()
            if re.fullmatch(r"[0-9a-f]{40}", commit):
                return commit
    except (OSError, subprocess.SubprocessError):
        pass

    try:
        manifest = json.loads((_ROOT / "deployment-manifest.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    commit = manifest.get("commit_sha") if isinstance(manifest, dict) else None
    return commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read the current Flow UI state for the fixed gflow-prod profile. "
            "This operation does not submit, approve, edit, reserve, or generate."
        )
    )
    parser.add_argument(
        "--provider-account-id", choices=[EXPECTED_PROVIDER_ACCOUNT_ID], required=True
    )
    parser.add_argument("--project-id", choices=[EXPECTED_PROJECT_ID], required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        runtime = load_runtime_settings()
        report = asyncio.run(
            inspect_flow_pricing_read_only(
                provider_account_id=args.provider_account_id,
                expected_project_id=args.project_id,
                expected_email="singleava508@gmail.com",
                runtime_state_root=runtime.runtime_state_root,
                software_commit=_source_commit(),
            )
        )
    except Exception:
        report = {
            "INSPECTION_STATUS": "HOLD",
            "REASON_CODE": "INSPECTION_RUNTIME_FAILED",
            "PROVIDER_GENERATE_ACTIONS": 0,
            "NEW_ATTEMPTS_CREATED": 0,
            "NEW_RESERVATIONS_CREATED": 0,
            "APPROVAL_ACTIONS": 0,
            "PROVIDER_CREDITS_CONSUMED": 0,
        }
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0 if report.get("INSPECTION_STATUS") == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
