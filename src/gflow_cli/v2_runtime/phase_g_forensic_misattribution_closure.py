"""Fail-closed evidence contract for closing a historical D2 misattribution."""

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
from .phase_g_forensic_common import (
    add_operator_arguments,
    preview_result,
    sanitized_reason,
    timestamp_error,
    verified_ledger_attempt,
)

_SCHEMA_VERSION = "D2_FORENSIC_MISATTRIBUTION_CLOSURE_V2"
_REASON_RE = re.compile(r"^[A-Za-z0-9 .,:;()/_-]{1,240}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REQUIRED_TRUE = (
    "RETRY_AUTHORIZATION_PRESENT",
    "RECORDED_PROVIDER_IDS_HISTORICAL",
    "HISTORICAL_EXECUTION_MATCH",
    "HISTORICAL_MEDIA_MATCH",
    "LIVE_STRUCTURED_ENUMERATION_COMPLETE",
    "RECOVERY_JOURNAL_CLEAN",
)
_REQUIRED_FALSE = (
    "POST_BASELINE_NEW_MEDIA_ASSOCIATION",
    "REMOTE_UPLOAD_OCCURRED",
    "REMOTE_VERIFIED",
)
_EVIDENCE_KEYS = frozenset(
    {
        "SCHEMA_VERSION",
        "D2_RUN_ID",
        "CANONICAL_LEDGER_SHA256",
        "IMPLEMENTATION_COMMIT",
        "PROVIDER_ACCOUNT_ID",
        "PROVIDER_PROJECT_ID",
        "CURRENT_SUBMISSION_STATE",
        "CURRENT_TERMINAL_STATE",
        "GENERATE_ACTION_COUNT",
        "APPROVAL_ACTION_COUNT",
        "ACCOUNTED_CREDIT_COST_EXPECTED",
        "ACCOUNTING_DATE_EXPECTED",
        *_REQUIRED_TRUE,
        "RETRY_PREDECESSOR",
        "RECORDED_PROVIDER_EXECUTION_ID",
        "RECORDED_PROVIDER_MEDIA_ID",
        "RECORDED_PROVIDER_MEDIA_IDS",
        "CANDIDATE_NEW_EXECUTION_COUNT",
        "CANDIDATE_NEW_MEDIA_COUNT",
        "ATTRIBUTION_CLASSIFICATION",
        "PROVIDER_OUTCOME",
        "CREDIT_OUTCOME",
        "PROVIDER_BILLING_OUTCOME",
        "SAFE_TO_CLOSE",
        "FORENSIC_COMPLETED_AT",
        "FORENSIC_REASON",
        *_REQUIRED_FALSE,
    }
)


def validate_forensic_misattribution_evidence(
    evidence: Mapping[str, object],
) -> dict[str, object]:
    """Validate and sort the complete sanitized proof package."""
    if frozenset(evidence) != _EVIDENCE_KEYS:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "misattribution evidence fields are incomplete or unsupported",
        )
    if evidence.get("SCHEMA_VERSION") != _SCHEMA_VERSION:
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "misattribution schema is invalid")
    for key, pattern in (
        ("D2_RUN_ID", r"D2_[A-Za-z0-9_-]{8,64}"),
        ("CANONICAL_LEDGER_SHA256", _SHA256_RE.pattern),
        ("IMPLEMENTATION_COMMIT", r"[0-9a-f]{40,64}"),
    ):
        value = evidence.get(key)
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", f"{key} is invalid")
    if (
        evidence.get("PROVIDER_ACCOUNT_ID") != "PVA_0001"
        or evidence.get("PROVIDER_PROJECT_ID") != "34e60953-e534-46e9-ac65-993893e61065"
        or evidence.get("CURRENT_SUBMISSION_STATE") != "GENERATION_SUCCEEDED"
        or evidence.get("CURRENT_TERMINAL_STATE") not in ("NONE", "GENERATION_SUCCEEDED")
        or evidence.get("GENERATE_ACTION_COUNT") != 1
        or isinstance(evidence.get("GENERATE_ACTION_COUNT"), bool)
        or evidence.get("APPROVAL_ACTION_COUNT") != 0
        or isinstance(evidence.get("APPROVAL_ACTION_COUNT"), bool)
        or evidence.get("ACCOUNTED_CREDIT_COST_EXPECTED") != "40"
        or evidence.get("ACCOUNTING_DATE_EXPECTED") != "2026-10-22"
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence does not match the exact retry shape"
        )
    for key in _REQUIRED_TRUE:
        if evidence.get(key) is not True:
            raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", f"{key} must be verified")
    for key in _REQUIRED_FALSE:
        if evidence.get(key) is not False:
            raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", f"{key} must be false")
    if (
        evidence.get("RETRY_PREDECESSOR") != "D2_2aebc5af59bd423b"
        or evidence.get("RECORDED_PROVIDER_EXECUTION_ID") != "9dc0380b-0a38-44f7-9c6c-8fe5262fdc2a"
        or evidence.get("RECORDED_PROVIDER_MEDIA_ID") != "d6e85926-120f-40ab-bfc0-6a788c521339"
        or evidence.get("ATTRIBUTION_CLASSIFICATION") != "NO_NEW_RETRY_OUTPUT_PROVEN"
        or evidence.get("CANDIDATE_NEW_EXECUTION_COUNT") != 0
        or evidence.get("CANDIDATE_NEW_MEDIA_COUNT") != 0
        or evidence.get("PROVIDER_OUTCOME") != "UNKNOWN"
        or evidence.get("CREDIT_OUTCOME") != "UNKNOWN"
        or evidence.get("PROVIDER_BILLING_OUTCOME") != "UNKNOWN"
        or evidence.get("SAFE_TO_CLOSE") is not True
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "evidence does not prove historical misattribution with unknown outcomes",
        )
    media_ids_value = evidence.get("RECORDED_PROVIDER_MEDIA_IDS")
    if not isinstance(media_ids_value, list) or not media_ids_value:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "recorded media identity list is invalid"
        )
    media_ids = cast(list[object], media_ids_value)
    if any(not isinstance(item, str) or not item for item in media_ids):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "recorded media identity list is invalid"
        )
    media_id_strings = cast(list[str], media_ids)
    if (
        len(media_id_strings) != len(set(media_id_strings))
        or "d6e85926-120f-40ab-bfc0-6a788c521339" not in media_id_strings
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "recorded media identity list is invalid"
        )
    completed_at = evidence.get("FORENSIC_COMPLETED_AT")
    if timestamp_error(completed_at) is not None:
        raise PhaseGError(
            "PHASE_G_FORENSIC_TIME_INVALID", "forensic time must include a UTC offset"
        )
    try:
        datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PhaseGError("PHASE_G_FORENSIC_TIME_INVALID", "forensic time is invalid") from exc
    if not sanitized_reason(evidence.get("FORENSIC_REASON"), _REASON_RE):
        raise PhaseGError(
            "PHASE_G_FORENSIC_REASON_INVALID", "forensic reason must be short and sanitized"
        )
    return {key: evidence[key] for key in sorted(_EVIDENCE_KEYS)}


def forensic_misattribution_evidence_digest(evidence: Mapping[str, object]) -> str:
    """Hash the canonical JSON representation of validated sanitized evidence."""
    encoded = json.dumps(
        validate_forensic_misattribution_evidence(evidence),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_misattribution_evidence(path: Path) -> tuple[dict[str, object], str]:
    """Load only canonical JSON evidence; raw data is never echoed."""
    if path.is_symlink() or not path.is_file():
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_MISSING", "misattribution evidence is missing or unsafe"
        )
    try:
        raw = path.read_bytes()
        value: object = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "misattribution evidence JSON is unreadable"
        ) from exc
    if not isinstance(value, dict):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence must be an object")
    summary = validate_forensic_misattribution_evidence(cast(dict[str, object], value))
    canonical = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if raw != canonical:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_NONCANONICAL", "evidence JSON must be canonical"
        )
    return summary, hashlib.sha256(raw).hexdigest()


def forensic_misattribution_closure_preview(
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
    """Validate all preconditions without mutating the canonical ledger."""
    summary = validate_forensic_misattribution_evidence(evidence)
    if (
        summary["D2_RUN_ID"] != d2_run_id
        or summary["IMPLEMENTATION_COMMIT"] != implementation_commit
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence run ID or implementation differs"
        )
    if summary["CANONICAL_LEDGER_SHA256"] != expected_ledger_sha256:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence names a different ledger SHA"
        )
    if (
        evidence_sha256 != expected_evidence_sha256
        or forensic_misattribution_evidence_digest(summary) != evidence_sha256
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_HASH_MISMATCH", "evidence SHA differs from expectation"
        )
    if not _SHA256_RE.fullmatch(evidence_sha256):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_HASH_INVALID", "evidence SHA is invalid")
    actual_sha, attempt = verified_ledger_attempt(
        runtime_state_root, d2_run_id=d2_run_id, expected_sha256=expected_ledger_sha256
    )
    ledger = D2DailyCreditLedger(runtime_state_root)
    rows = ledger.attempts()
    if sum(row.get("d2_run_id") == d2_run_id for row in rows) != 1:
        raise PhaseGError("PHASE_G_ATTEMPT_NOT_UNIQUE", "D2 run ID must exist exactly once")
    if (
        expected_state != "GENERATION_SUCCEEDED"
        or attempt.get("submission_state") != expected_state
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_STATE_MISMATCH", "expected current state is GENERATION_SUCCEEDED"
        )
    terminal_state = attempt.get("terminal_state")
    if terminal_state not in (None, "GENERATION_SUCCEEDED") or evidence.get(
        "CURRENT_TERMINAL_STATE"
    ) != ("NONE" if terminal_state is None else terminal_state):
        raise PhaseGError(
            "PHASE_G_ATTEMPT_TERMINAL",
            "attempt must have an evidence-bound nonterminal generation marker",
        )
    if sum(row.get("d2_run_id") == "D2_2aebc5af59bd423b" for row in rows) != 1:
        raise PhaseGError(
            "PHASE_G_RETRY_PREDECESSOR_MISMATCH", "retry predecessor must exist exactly once"
        )
    D2DailyCreditLedger.validate_misattributed_attempt(
        runtime_state_root,
        {"attempts": [dict(row) for row in rows]},
        attempt,
        summary,
    )
    return preview_result(
        ledger_sha256=actual_sha,
        evidence_sha256=evidence_sha256,
        implementation_commit=implementation_commit,
        attempt=attempt,
        evidence=summary,
    )


def main(argv: list[str] | None = None) -> int:
    """Preview by default; mutate only after exact evidence and SHA checks with --apply."""
    parser = argparse.ArgumentParser(description=__doc__)
    add_operator_arguments(parser, apply_help="apply the reviewed misattribution closure")
    args = parser.parse_args(argv)
    try:
        evidence, evidence_sha = load_misattribution_evidence(args.evidence_json)
        preview = forensic_misattribution_closure_preview(
            args.runtime_state_root,
            d2_run_id=args.d2_run_id,
            expected_ledger_sha256=args.expected_ledger_sha256,
            expected_state=args.expected_current_state,
            expected_evidence_sha256=args.expected_evidence_sha256,
            implementation_commit=args.implementation_commit,
            evidence=evidence,
            evidence_sha256=evidence_sha,
        )
        preview_attempt = cast(dict[str, object], preview["attempt"])
        attempt = preview_attempt
        if args.apply:
            attempt = dict(
                D2DailyCreditLedger(
                    args.runtime_state_root
                ).close_misattributed_generation_forensic(
                    args.d2_run_id,
                    expected_ledger_sha256=args.expected_ledger_sha256,
                    evidence_sha256=evidence_sha,
                    implementation_commit=args.implementation_commit,
                    evidence=evidence,
                )
            )
        click.echo(f"CURRENT_STATE = {preview_attempt['submission_state']}")
        click.echo("PROPOSED_STATE = FORENSICALLY_CLOSED_UNKNOWN")
        click.echo(f"FORENSIC_EVIDENCE_SHA256 = {evidence_sha}")
        click.echo(f"ACCOUNTED_CREDIT_COST = {attempt.get('accounted_credit_cost')} / unchanged")
        click.echo("PROVIDER_OUTCOME = UNKNOWN")
        click.echo("CREDIT_OUTCOME = UNKNOWN")
        click.echo("PROVIDER_BILLING_OUTCOME = UNKNOWN")
        click.echo(f"DRY_RUN = {not args.apply}")
        click.echo(f"LEDGER_MUTATED = {bool(args.apply)}")
        if args.apply:
            click.echo(f"RESULTING_STATE = {attempt['submission_state']}")
        return 0
    except (PhaseGError, OSError) as exc:
        click.echo(f"ERROR = {exc}", err=True)
        return 2
