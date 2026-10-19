"""Fail-closed evidence contract for closing irrecoverable unknown D2 attempts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import cast

import click

from .phase_g import D2DailyCreditLedger, PhaseGError
from .phase_g_artifacts import sha256_file

_REQUIRED_TRUE = (
    "CANONICAL_LEDGER_FOUND",
    "LEDGER_SHA_VERIFIED",
    "RECOVERY_JOURNAL_CLEAN",
    "PROVIDER_EXECUTION_ID_ABSENT",
    "PROVIDER_MEDIA_ID_ABSENT",
    "PHASE_G_RESULT_ABSENT",
    "HISTORICAL_TEMP_OUTPUT_ABSENT",
    "EXACT_TEST_LANE_RECOVERED",
    "EXACT_TEST_IDENTITY_VERIFIED",
)
_PROJECT_COUNTS = (
    "CURRENT_PROJECT_MEDIA_COUNT",
    "CURRENT_PROJECT_EXECUTION_COUNT",
    "CURRENT_PROJECT_PENDING_COUNT",
    "CURRENT_PROJECT_CONFIRMATION_COUNT",
    "CURRENT_PROJECT_CHAT_COUNT",
)
_EVIDENCE_KEYS = frozenset(
    {
        "D2_RUN_ID",
        "CANONICAL_LEDGER_SHA256",
        *_REQUIRED_TRUE,
        "GENERATE_ACTION_COUNT",
        *_PROJECT_COUNTS,
        "PROVIDER_BILLING_OUTCOME",
        "OPERATING_CLASSIFICATION",
        "SAFE_TO_RETRY",
        "FORENSIC_COMPLETED_AT",
        "FORENSIC_REASON",
    }
)
_REASON_RE = re.compile(r"^[A-Za-z0-9 .,:;()/_-]{1,240}$")


def validate_forensic_evidence(evidence: Mapping[str, object]) -> dict[str, object]:
    """Return a strict sanitized evidence summary, rejecting incomplete/raw data."""
    if frozenset(evidence) != _EVIDENCE_KEYS:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "forensic evidence fields are incomplete or contain unsupported data",
        )
    run_id = evidence.get("D2_RUN_ID")
    ledger_sha = evidence.get("CANONICAL_LEDGER_SHA256")
    if not isinstance(run_id, str) or not re.fullmatch(r"D2_[A-Za-z0-9_-]{8,64}", run_id):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence D2 run ID is invalid")
    if not isinstance(ledger_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", ledger_sha):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence ledger SHA-256 is invalid")
    for key in _REQUIRED_TRUE:
        if evidence.get(key) is not True:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "required forensic evidence is not verified"
            )
    if evidence.get("GENERATE_ACTION_COUNT") != 1:
        raise PhaseGError(
            "PHASE_G_GENERATE_COUNT_MISMATCH",
            "forensic evidence must record exactly one Generate action",
        )
    for key in _PROJECT_COUNTS:
        value = evidence.get(key)
        if isinstance(value, bool) or value != 0:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                "current provider project must have zero observed activity",
            )
    if evidence.get("PROVIDER_BILLING_OUTCOME") != "UNKNOWN":
        raise PhaseGError(
            "PHASE_G_BILLING_OUTCOME_INVALID",
            "forensic closure must preserve unknown provider billing",
        )
    if (
        evidence.get("OPERATING_CLASSIFICATION") != "UNKNOWN_SUBMISSION_STATE"
        or evidence.get("SAFE_TO_RETRY") != "NO"
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "evidence must preserve the unknown classification and prohibit retry",
        )
    completed_at = evidence.get("FORENSIC_COMPLETED_AT")
    if not isinstance(completed_at, str):
        raise PhaseGError("PHASE_G_FORENSIC_TIME_INVALID", "forensic completion time is invalid")
    try:
        parsed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PhaseGError(
            "PHASE_G_FORENSIC_TIME_INVALID", "forensic completion time is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PhaseGError(
            "PHASE_G_FORENSIC_TIME_INVALID", "forensic completion time must include a UTC offset"
        )
    reason = evidence.get("FORENSIC_REASON")
    if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
        raise PhaseGError(
            "PHASE_G_FORENSIC_REASON_INVALID", "forensic reason must be a short sanitized statement"
        )
    return {key: evidence[key] for key in sorted(_EVIDENCE_KEYS)}


def forensic_evidence_digest(evidence: Mapping[str, object]) -> str:
    """Hash the canonical JSON representation of the validated summary."""
    summary = validate_forensic_evidence(evidence)
    encoded = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_evidence(path: Path) -> tuple[dict[str, object], str]:
    """Load a local sanitized evidence JSON object; never print its raw contents."""
    if path.is_symlink() or not path.is_file():
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_MISSING", "forensic evidence file is missing or unsafe"
        )
    try:
        raw_bytes = path.read_bytes()
        value: object = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "forensic evidence JSON is unreadable"
        ) from exc
    if not isinstance(value, dict):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "forensic evidence must be a JSON object"
        )
    summary = validate_forensic_evidence(cast(dict[str, object], value))
    canonical_bytes = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if raw_bytes != canonical_bytes:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_NONCANONICAL",
            "evidence file must use canonical compact JSON before it is hashed",
        )
    return summary, hashlib.sha256(raw_bytes).hexdigest()


def forensic_closure_preview(
    runtime_state_root: Path,
    *,
    d2_run_id: str,
    expected_ledger_sha256: str,
    expected_state: str,
    expected_evidence_sha256: str,
    implementation_commit: str,
    evidence: Mapping[str, object],
    evidence_sha256: str,
) -> dict[str, object]:
    """Validate a closure proposal without writing the ledger."""
    summary = validate_forensic_evidence(evidence)
    if summary["D2_RUN_ID"] != d2_run_id:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence belongs to a different D2 run"
        )
    if summary["CANONICAL_LEDGER_SHA256"] != expected_ledger_sha256:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence names a different canonical ledger SHA"
        )
    if (
        evidence_sha256 != expected_evidence_sha256
        or forensic_evidence_digest(summary) != evidence_sha256
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_HASH_MISMATCH",
            "forensic evidence SHA differs from expectation",
        )
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_HASH_INVALID", "forensic evidence SHA-256 is invalid"
        )
    if not re.fullmatch(r"[0-9a-f]{40,64}", implementation_commit):
        raise PhaseGError("PHASE_G_FORENSIC_COMMIT_INVALID", "implementation commit SHA is invalid")
    ledger = D2DailyCreditLedger(runtime_state_root)
    if ledger.path.is_symlink() or not ledger.path.is_file():
        raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "D2 ledger is missing or unsafe")
    actual_sha = sha256_file(ledger.path)
    if actual_sha != expected_ledger_sha256:
        raise PhaseGError(
            "PHASE_G_LEDGER_CHANGED", "D2 ledger SHA differs from operator expectation"
        )
    attempt = next((row for row in ledger.attempts() if row.get("d2_run_id") == d2_run_id), None)
    if attempt is None:
        raise PhaseGError(
            "PHASE_G_ATTEMPT_NOT_FOUND", "D2 attempt does not exist in the local ledger"
        )
    if (
        expected_state != "UNKNOWN_SUBMISSION_STATE"
        or attempt.get("submission_state") != expected_state
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_STATE_MISMATCH",
            "expected and current D2 state must be UNKNOWN_SUBMISSION_STATE",
        )
    if attempt.get("terminal_state") is not None:
        raise PhaseGError("PHASE_G_ATTEMPT_TERMINAL", "D2 attempt is already terminal")
    if attempt.get("provider_media_id") or attempt.get("provider_execution_id"):
        raise PhaseGError(
            "PHASE_G_PROVIDER_OUTCOME_PRESENT",
            "provider identities require provider-outcome reconciliation",
        )
    if attempt.get("provider_media_ids"):
        raise PhaseGError(
            "PHASE_G_PROVIDER_OUTCOME_PRESENT",
            "provider media identities require provider-outcome reconciliation",
        )
    if attempt.get("generate_action_count") != 1 or summary["GENERATE_ACTION_COUNT"] != 1:
        raise PhaseGError(
            "PHASE_G_GENERATE_COUNT_MISMATCH",
            "forensic closure requires exactly one Generate action",
        )
    return {
        "ledger_sha256": actual_sha,
        "evidence_sha256": evidence_sha256,
        "implementation_commit": implementation_commit,
        "attempt": dict(attempt),
        "evidence": summary,
    }


def main(argv: list[str] | None = None) -> int:
    """Preview by default; mutate only with --apply and an exact concurrency guard."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-state-root", type=Path, required=True)
    parser.add_argument("--d2-run-id", required=True)
    parser.add_argument("--expected-current-state", required=True)
    parser.add_argument("--expected-ledger-sha256", required=True)
    parser.add_argument("--expected-evidence-sha256", required=True)
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--evidence-json", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="apply the reviewed terminal closure")
    args = parser.parse_args(argv)
    try:
        evidence, evidence_sha256 = load_evidence(args.evidence_json)
        preview = forensic_closure_preview(
            args.runtime_state_root,
            d2_run_id=args.d2_run_id,
            expected_ledger_sha256=args.expected_ledger_sha256,
            expected_state=args.expected_current_state,
            expected_evidence_sha256=args.expected_evidence_sha256,
            implementation_commit=args.implementation_commit,
            evidence=evidence,
            evidence_sha256=evidence_sha256,
        )
        attempt = cast(dict[str, object], preview["attempt"])
        current_cost = str(attempt.get("accounted_credit_cost", "0"))
        if args.apply:
            closed = D2DailyCreditLedger(args.runtime_state_root).close_unknown_forensic(
                args.d2_run_id,
                expected_ledger_sha256=args.expected_ledger_sha256,
                evidence_sha256=evidence_sha256,
                implementation_commit=args.implementation_commit,
                evidence=evidence,
            )
            current_state = str(closed["submission_state"])
            applied = True
        else:
            current_state = str(attempt["submission_state"])
            applied = False
        click.echo(f"CURRENT_STATE = {attempt['submission_state']}")
        click.echo("PROPOSED_STATE = FORENSICALLY_CLOSED_UNKNOWN")
        click.echo(f"GENERATE_ACTION_COUNT = {attempt.get('generate_action_count')}")
        click.echo(f"CURRENT_ACCOUNTED_CREDIT_COST = {current_cost}")
        click.echo(f"PROPOSED_ACCOUNTED_CREDIT_COST = {current_cost} / unchanged")
        click.echo("PROVIDER_OUTCOME = UNKNOWN")
        click.echo("CREDIT_OUTCOME = UNKNOWN")
        click.echo("NEW_GENERATE_ALLOWED_FOR_SAME_RUN = NO")
        click.echo("FRESH_D2_ALLOWED_AFTER_CLOSURE = YES")
        click.echo("D6_ALLOWED_AFTER_CLOSURE = NO")
        click.echo(f"LEDGER_SHA_MATCH = {preview['ledger_sha256'] == args.expected_ledger_sha256}")
        click.echo(f"FORENSIC_EVIDENCE_SHA256 = {preview['evidence_sha256']}")
        click.echo(f"FORENSIC_IMPLEMENTATION_COMMIT = {preview['implementation_commit']}")
        click.echo(f"DRY_RUN = {not args.apply}")
        click.echo(f"LEDGER_MUTATED = {applied}")
        if args.apply:
            click.echo(f"RESULTING_STATE = {current_state}")
        return 0
    except (PhaseGError, OSError) as exc:
        click.echo(f"ERROR = {exc}", err=True)
        return 2
