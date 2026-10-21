"""Shared mechanical helpers for versioned D2 forensic closure operators."""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from re import Pattern
from typing import Literal

from .phase_g import D2DailyCreditLedger, PhaseGError
from .phase_g_artifacts import sha256_file


def timestamp_error(value: object) -> Literal["invalid", "offset"] | None:
    """Classify malformed or offset-free timestamps without changing versioned messages."""
    if not isinstance(value, str):
        return "invalid"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "invalid"
    return None if parsed.tzinfo is not None and parsed.utcoffset() is not None else "offset"


def sanitized_reason(value: object, pattern: Pattern[str]) -> bool:
    """Return whether an operator reason matches the contract's safe alphabet."""
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def verified_ledger_attempt(
    runtime_state_root: Path, *, d2_run_id: str, expected_sha256: str
) -> tuple[str, Mapping[str, object]]:
    """Hash the canonical ledger and read one exact attempt under the ledger API."""
    ledger = D2DailyCreditLedger(runtime_state_root)
    if ledger.path.is_symlink() or not ledger.path.is_file():
        raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "D2 ledger is missing or unsafe")
    actual_sha = sha256_file(ledger.path)
    if actual_sha != expected_sha256:
        raise PhaseGError(
            "PHASE_G_LEDGER_CHANGED", "D2 ledger SHA differs from operator expectation"
        )
    attempt = next((row for row in ledger.attempts() if row.get("d2_run_id") == d2_run_id), None)
    if attempt is None:
        raise PhaseGError(
            "PHASE_G_ATTEMPT_NOT_FOUND", "D2 attempt does not exist in the local ledger"
        )
    return actual_sha, attempt


def preview_result(
    *,
    ledger_sha256: str,
    evidence_sha256: str,
    implementation_commit: str,
    attempt: Mapping[str, object],
    evidence: Mapping[str, object],
) -> dict[str, object]:
    """Build the stable read-only preview envelope used by both contract versions."""
    return {
        "ledger_sha256": ledger_sha256,
        "evidence_sha256": evidence_sha256,
        "implementation_commit": implementation_commit,
        "attempt": dict(attempt),
        "evidence": dict(evidence),
    }


def add_operator_arguments(parser: argparse.ArgumentParser, *, apply_help: str) -> None:
    """Add the shared, exact operator CLI arguments to a versioned closure parser."""
    parser.add_argument("--runtime-state-root", type=Path, required=True)
    parser.add_argument("--d2-run-id", required=True)
    parser.add_argument("--expected-current-state", required=True)
    parser.add_argument("--expected-ledger-sha256", required=True)
    parser.add_argument("--expected-evidence-sha256", required=True)
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--evidence-json", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help=apply_help)
