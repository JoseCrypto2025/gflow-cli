"""Version 2 evidence contract for closing unknown D2 attempts with chat-only activity."""

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

_SCHEMA_VERSION = "D2_FORENSIC_CLOSURE_V2"
_TEST_ACCOUNT_ID = "PVA_0001"
_TEST_PROJECT_ID = "34e60953-e534-46e9-ac65-993893e61065"
_TEST_PROJECT_NAME = "AI Video Automation V2 - TEST"
_REQUIRED_TRUE = (
    "CANONICAL_LEDGER_FOUND",
    "LEDGER_SHA_VERIFIED",
    "RECOVERY_JOURNAL_CLEAN",
    "PROVIDER_EXECUTION_ID_ABSENT",
    "PROVIDER_MEDIA_IDS_ABSENT",
    "PHASE_G_RESULT_ABSENT",
    "HISTORICAL_TEMP_OUTPUT_ABSENT",
    "EXACT_TEST_LANE_RECOVERED",
    "EXACT_TEST_IDENTITY_VERIFIED",
    "EXACT_TEST_PROJECT_VERIFIED",
    "CHAT_SNAPSHOT_HYDRATED",
    "CHAT_ENUMERATION_COMPLETE",
)
_ZERO_COUNTS = (
    "CURRENT_PROJECT_MEDIA_COUNT",
    "CURRENT_PROJECT_EXECUTION_COUNT",
    "CURRENT_PROJECT_PENDING_COUNT",
    "CURRENT_PROJECT_CONFIRMATION_COUNT",
)
_CHAT_FIELDS = frozenset(
    {
        "TEXT_SHA256",
        "EXACT_PROMPT_MATCH",
        "EXACT_SUBSTRING_MATCH",
        "MEDIA_UUIDS",
        "D2_RUN_ID_PRESENT",
        "PROVIDER_EXECUTION_IDS",
        "STABLE_ID_AVAILABLE",
        "STABLE_ID_SHA256",
        "TIMESTAMP_AVAILABLE",
        "TIMESTAMP_UTC",
    }
)
_EVIDENCE_KEYS = frozenset(
    {
        "SCHEMA_VERSION",
        "D2_RUN_ID",
        "CANONICAL_LEDGER_SHA256",
        *_REQUIRED_TRUE,
        "PROVIDER_ACCOUNT_ID",
        "PROVIDER_PROJECT_ID",
        "PROVIDER_PROJECT_NAME",
        "GENERATE_ACTION_COUNT",
        "APPROVAL_ACTION_COUNT",
        *_ZERO_COUNTS,
        "CURRENT_PROJECT_CHAT_COUNT",
        "CURRENT_PROJECT_MEDIA_IDS",
        "CURRENT_PROJECT_EXECUTION_IDS",
        "CURRENT_PROJECT_CONFIRMATION_IDS",
        "BASELINE_AGENT_TURN_ID",
        "BASELINE_CONFIRMATION_IDS",
        "BASELINE_CHAT_COUNT",
        "BASELINE_CHAT_TAIL_HASH",
        "BASELINE_CHAT_BUBBLE_SHA256S",
        "CURRENT_CHAT_TAIL_HASH",
        "CURRENT_CHAT_BUBBLE_SHA256S",
        "CHAT_BUBBLES",
        "PROMPT_SHA256",
        "ACCOUNTED_CREDIT_COST_EXPECTED",
        "PROVIDER_OUTCOME",
        "CREDIT_OUTCOME",
        "PROVIDER_BILLING_OUTCOME",
        "OPERATING_CLASSIFICATION",
        "SAFE_TO_RETRY",
        "FORENSIC_COMPLETED_AT",
        "FORENSIC_REASON",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^D2_[A-Za-z0-9_-]{8,64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
_REASON_RE = re.compile(r"^[A-Za-z0-9 .,:;()/_-]{1,240}$")


def _hash_list(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", f"{field} is invalid")
    items = cast(list[object], value)
    if any(not isinstance(item, str) or not _SHA256_RE.fullmatch(item) for item in items):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", f"{field} is invalid")
    return cast(list[str], items)


def _parse_baseline_agent_turn_id(value: object) -> tuple[int, str]:
    if not isinstance(value, str):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "baseline Agent turn ID is invalid")
    match = re.fullmatch(r"(0|[1-9][0-9]*):([0-9a-f]{0,64})", value)
    if match is None:
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "baseline Agent turn ID is invalid")
    count, tail_hash = int(match.group(1)), match.group(2)
    if (count == 0 and tail_hash) or (count > 0 and not _SHA256_RE.fullmatch(tail_hash)):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "baseline Agent turn ID is inconsistent"
        )
    return count, tail_hash


def validate_forensic_evidence_v2(evidence: Mapping[str, object]) -> dict[str, object]:
    """Validate a sanitized V2 chat-only evidence package without inferring attribution."""
    if frozenset(evidence) != _EVIDENCE_KEYS:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "V2 evidence fields are incomplete or contain unsupported data",
        )
    if evidence.get("SCHEMA_VERSION") != _SCHEMA_VERSION:
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "V2 schema version is invalid")
    run_id = evidence.get("D2_RUN_ID")
    ledger_sha = evidence.get("CANONICAL_LEDGER_SHA256")
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence D2 run ID is invalid")
    if not isinstance(ledger_sha, str) or not _SHA256_RE.fullmatch(ledger_sha):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "ledger SHA-256 is invalid")
    for key in _REQUIRED_TRUE:
        if evidence.get(key) is not True:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "required V2 evidence is not verified"
            )
    if (
        evidence.get("PROVIDER_ACCOUNT_ID") != _TEST_ACCOUNT_ID
        or evidence.get("PROVIDER_PROJECT_ID") != _TEST_PROJECT_ID
        or evidence.get("PROVIDER_PROJECT_NAME") != _TEST_PROJECT_NAME
    ):
        raise PhaseGError(
            "PHASE_G_EXECUTION_IDENTITY_MISMATCH", "V2 evidence is not pinned to the TEST project"
        )
    generate_count = evidence.get("GENERATE_ACTION_COUNT")
    if isinstance(generate_count, bool) or generate_count != 1:
        raise PhaseGError(
            "PHASE_G_GENERATE_COUNT_MISMATCH", "V2 closure requires exactly one Generate action"
        )
    approval_count = evidence.get("APPROVAL_ACTION_COUNT")
    if (
        isinstance(approval_count, bool)
        or not isinstance(approval_count, int)
        or approval_count > 1
    ):
        raise PhaseGError(
            "PHASE_G_GENERATE_COUNT_MISMATCH", "V2 closure permits at most one approval action"
        )
    baseline_confirmation_ids = evidence.get("BASELINE_CONFIRMATION_IDS")
    if not isinstance(baseline_confirmation_ids, list):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "baseline confirmation IDs must be a unique sanitized string list",
        )
    confirmation_items = cast(list[object], baseline_confirmation_ids)
    if any(not isinstance(item, str) or not item for item in confirmation_items):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "baseline confirmation IDs must be a unique sanitized string list",
        )
    confirmation_strings = cast(list[str], confirmation_items)
    if len(set(confirmation_strings)) != len(confirmation_strings):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "baseline confirmation IDs must be a unique sanitized string list",
        )
    for key in _ZERO_COUNTS:
        value = evidence.get(key)
        if isinstance(value, bool) or value != 0:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                "V2 requires zero non-chat provider activity",
            )
    for key in (
        "CURRENT_PROJECT_MEDIA_IDS",
        "CURRENT_PROJECT_EXECUTION_IDS",
        "CURRENT_PROJECT_CONFIRMATION_IDS",
    ):
        value = evidence.get(key)
        if not isinstance(value, list) or value:
            raise PhaseGError("PHASE_G_PROVIDER_OUTCOME_PRESENT", f"{key} must be an empty list")

    baseline_id = evidence.get("BASELINE_AGENT_TURN_ID")
    baseline_count, baseline_tail = _parse_baseline_agent_turn_id(baseline_id)
    baseline_count_evidence = evidence.get("BASELINE_CHAT_COUNT")
    if isinstance(baseline_count_evidence, bool) or baseline_count_evidence != baseline_count:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "baseline chat count differs from persisted marker"
        )
    if evidence.get("BASELINE_CHAT_TAIL_HASH") != baseline_tail:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "baseline chat tail hash differs from marker"
        )
    baseline_hashes = _hash_list(
        evidence.get("BASELINE_CHAT_BUBBLE_SHA256S"), field="baseline chat hashes"
    )
    if len(baseline_hashes) != baseline_count:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "ordered baseline bubble hashes are required for every baseline chat",
        )
    current_count = evidence.get("CURRENT_PROJECT_CHAT_COUNT")
    if (
        isinstance(current_count, bool)
        or not isinstance(current_count, int)
        or current_count <= baseline_count
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "V2 chat-only closure requires post-baseline chat activity",
        )
    current_tail = evidence.get("CURRENT_CHAT_TAIL_HASH")
    if not isinstance(current_tail, str) or not _SHA256_RE.fullmatch(current_tail):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "current chat tail hash is invalid")
    current_hashes = _hash_list(
        evidence.get("CURRENT_CHAT_BUBBLE_SHA256S"), field="current chat hashes"
    )
    if len(current_hashes) != current_count or current_hashes[:baseline_count] != baseline_hashes:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "current ordered chat hashes do not prove the baseline-relative chat delta",
        )
    bubbles = evidence.get("CHAT_BUBBLES")
    if not isinstance(bubbles, list):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "all current chat bubbles must be enumerated"
        )
    bubble_items = cast(list[object], bubbles)
    if len(bubble_items) != current_count:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "all current chat bubbles must be enumerated"
        )
    for index, bubble_value in enumerate(bubble_items):
        if not isinstance(bubble_value, dict):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "per-bubble evidence fields are invalid"
            )
        bubble = cast(dict[str, object], bubble_value)
        if frozenset(bubble) != _CHAT_FIELDS:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "per-bubble evidence fields are invalid"
            )
        if bubble.get("TEXT_SHA256") != current_hashes[index]:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID", "per-bubble hash order is inconsistent"
            )
        exact = bubble.get("EXACT_PROMPT_MATCH")
        substring = bubble.get("EXACT_SUBSTRING_MATCH")
        if (
            not isinstance(exact, bool)
            or not isinstance(substring, bool)
            or (exact and not substring)
        ):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID", "per-bubble prompt findings are invalid"
            )
        media_uuids = bubble.get("MEDIA_UUIDS")
        execution_ids = bubble.get("PROVIDER_EXECUTION_IDS")
        media_uuid_items = cast(list[object], media_uuids) if isinstance(media_uuids, list) else []
        if (
            not isinstance(media_uuids, list)
            or any(
                not isinstance(value, str) or not _UUID_RE.fullmatch(value)
                for value in media_uuid_items
            )
            or media_uuids
            or not isinstance(execution_ids, list)
            or execution_ids
            or bubble.get("D2_RUN_ID_PRESENT") is not False
        ):
            raise PhaseGError(
                "PHASE_G_PROVIDER_OUTCOME_PRESENT",
                "chat bubble contains a provider or run identifier",
            )
        stable_available = bubble.get("STABLE_ID_AVAILABLE")
        stable_hash = bubble.get("STABLE_ID_SHA256")
        timestamp_available = bubble.get("TIMESTAMP_AVAILABLE")
        timestamp_value = bubble.get("TIMESTAMP_UTC")
        if (
            not isinstance(stable_available, bool)
            or (
                stable_available
                and (not isinstance(stable_hash, str) or not _SHA256_RE.fullmatch(stable_hash))
            )
            or (not stable_available and stable_hash is not None)
            or not isinstance(timestamp_available, bool)
        ):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID", "stable ID or timestamp findings are invalid"
            )
        if timestamp_available:
            if not isinstance(timestamp_value, str):
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INVALID", "available chat timestamp is missing"
                )
            try:
                parsed_timestamp = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INVALID", "chat timestamp is invalid"
                ) from exc
            if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INVALID", "chat timestamp must include a UTC offset"
                )
        elif timestamp_value is not None:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID", "unavailable chat timestamp must be null"
            )

    prompt_sha = evidence.get("PROMPT_SHA256")
    if not isinstance(prompt_sha, str) or not _SHA256_RE.fullmatch(prompt_sha):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "prompt SHA-256 is invalid")
    accounted = evidence.get("ACCOUNTED_CREDIT_COST_EXPECTED")
    if not isinstance(accounted, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", accounted
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "expected accounted credit cost is invalid"
        )
    if (
        evidence.get("OPERATING_CLASSIFICATION") != "CHAT_ONLY_UNRESOLVED_ACTIVITY"
        or evidence.get("PROVIDER_OUTCOME") != "UNKNOWN"
        or evidence.get("CREDIT_OUTCOME") != "UNKNOWN"
        or evidence.get("PROVIDER_BILLING_OUTCOME") != "UNKNOWN"
        or evidence.get("SAFE_TO_RETRY") != "NO"
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "V2 must preserve unresolved outcomes and prohibit retry",
        )
    completed_at = evidence.get("FORENSIC_COMPLETED_AT")
    time_error = timestamp_error(completed_at)
    if time_error == "invalid":
        raise PhaseGError("PHASE_G_FORENSIC_TIME_INVALID", "forensic completion time is invalid")
    if time_error == "offset":
        raise PhaseGError(
            "PHASE_G_FORENSIC_TIME_INVALID", "forensic completion time must include a UTC offset"
        )
    reason = evidence.get("FORENSIC_REASON")
    if not sanitized_reason(reason, _REASON_RE):
        raise PhaseGError(
            "PHASE_G_FORENSIC_REASON_INVALID", "forensic reason must be short and sanitized"
        )
    return {key: evidence[key] for key in sorted(_EVIDENCE_KEYS)}


def forensic_evidence_digest_v2(evidence: Mapping[str, object]) -> str:
    summary = validate_forensic_evidence_v2(evidence)
    encoded = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_evidence_v2(path: Path) -> tuple[dict[str, object], str]:
    if path.is_symlink() or not path.is_file():
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_MISSING", "V2 evidence file is missing or unsafe"
        )
    try:
        raw = path.read_bytes()
        value: object = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "V2 evidence JSON is unreadable"
        ) from exc
    if not isinstance(value, dict):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "V2 evidence must be an object")
    summary = validate_forensic_evidence_v2(cast(dict[str, object], value))
    canonical = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if raw != canonical:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_NONCANONICAL", "V2 evidence must use canonical compact JSON"
        )
    return summary, hashlib.sha256(raw).hexdigest()


def forensic_closure_preview_v2(
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
    summary = validate_forensic_evidence_v2(evidence)
    if (
        summary["D2_RUN_ID"] != d2_run_id
        or summary["CANONICAL_LEDGER_SHA256"] != expected_ledger_sha256
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "V2 evidence run ID or ledger SHA differs"
        )
    if (
        evidence_sha256 != expected_evidence_sha256
        or forensic_evidence_digest_v2(summary) != evidence_sha256
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_HASH_MISMATCH", "V2 evidence SHA differs from expectation"
        )
    if not _SHA256_RE.fullmatch(evidence_sha256):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_HASH_INVALID", "V2 evidence SHA is invalid")
    if not re.fullmatch(r"[0-9a-f]{40,64}", implementation_commit):
        raise PhaseGError("PHASE_G_FORENSIC_COMMIT_INVALID", "implementation commit SHA is invalid")
    actual_sha, attempt = verified_ledger_attempt(
        runtime_state_root, d2_run_id=d2_run_id, expected_sha256=expected_ledger_sha256
    )
    if (
        expected_state != "UNKNOWN_SUBMISSION_STATE"
        or attempt.get("submission_state") != expected_state
    ):
        raise PhaseGError("PHASE_G_FORENSIC_STATE_MISMATCH", "V2 requires UNKNOWN_SUBMISSION_STATE")
    if attempt.get("terminal_state") is not None:
        raise PhaseGError("PHASE_G_ATTEMPT_TERMINAL", "D2 attempt is already terminal")
    approval_count = attempt.get("approval_action_count", 0)
    if isinstance(approval_count, bool) or not isinstance(approval_count, int):
        raise PhaseGError("PHASE_G_LEDGER_INVALID", "stored approval action count is invalid")
    if (
        attempt.get("provider_execution_id")
        or attempt.get("provider_media_id")
        or attempt.get("provider_media_ids")
        or attempt.get("generate_action_count") != 1
        or approval_count > 1
        or evidence["APPROVAL_ACTION_COUNT"] != approval_count
        or attempt.get("provider_outcome", "UNKNOWN") != "UNKNOWN"
        or attempt.get("credit_outcome", "UNKNOWN") != "UNKNOWN"
        or attempt.get("provider_billing_outcome") != "UNKNOWN"
        or attempt.get("accounted_credit_cost") != summary["ACCOUNTED_CREDIT_COST_EXPECTED"]
        or attempt.get("prompt_sha256") != summary["PROMPT_SHA256"]
        or attempt.get("baseline_agent_turn_id") != summary["BASELINE_AGENT_TURN_ID"]
        or attempt.get("baseline_confirmation_ids") != summary["BASELINE_CONFIRMATION_IDS"]
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "V2 evidence differs from persisted attempt"
        )
    baseline_count, _ = _parse_baseline_agent_turn_id(attempt.get("baseline_agent_turn_id"))
    stored_hashes: object = attempt.get("baseline_agent_turn_bubble_hashes")
    if baseline_count == 0:
        stored_hashes = [] if stored_hashes is None else stored_hashes
    if (
        not isinstance(stored_hashes, list)
        or cast(list[object], stored_hashes) != summary["BASELINE_CHAT_BUBBLE_SHA256S"]
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "persisted ordered baseline chat hashes are missing or differ",
        )
    return preview_result(
        ledger_sha256=actual_sha,
        evidence_sha256=evidence_sha256,
        implementation_commit=implementation_commit,
        attempt=attempt,
        evidence=summary,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_operator_arguments(parser, apply_help="apply the reviewed V2 terminal closure")
    args = parser.parse_args(argv)
    try:
        evidence, evidence_sha = load_evidence_v2(args.evidence_json)
        preview = forensic_closure_preview_v2(
            args.runtime_state_root,
            d2_run_id=args.d2_run_id,
            expected_ledger_sha256=args.expected_ledger_sha256,
            expected_state=args.expected_current_state,
            expected_evidence_sha256=args.expected_evidence_sha256,
            implementation_commit=args.implementation_commit,
            evidence=evidence,
            evidence_sha256=evidence_sha,
        )
        attempt = cast(dict[str, object], preview["attempt"])
        current_cost = str(attempt.get("accounted_credit_cost", "0"))
        if args.apply:
            closed = D2DailyCreditLedger(args.runtime_state_root).close_unknown_forensic_v2(
                args.d2_run_id,
                expected_ledger_sha256=args.expected_ledger_sha256,
                evidence_sha256=evidence_sha,
                implementation_commit=args.implementation_commit,
                evidence=evidence,
            )
            current_state = str(closed["submission_state"])
        else:
            current_state = str(attempt["submission_state"])
        click.echo("V2_EVIDENCE_COMPLETE = YES")
        click.echo(f"CURRENT_STATE = {attempt['submission_state']}")
        click.echo("PROPOSED_STATE = FORENSICALLY_CLOSED_UNKNOWN")
        click.echo("V2_CLASSIFICATION = CHAT_ONLY_UNRESOLVED_ACTIVITY")
        click.echo(f"CURRENT_ACCOUNTED_CREDIT_COST = {current_cost}")
        click.echo(f"PROPOSED_ACCOUNTED_CREDIT_COST = {current_cost} / unchanged")
        click.echo("PROVIDER_OUTCOME = UNKNOWN")
        click.echo("CREDIT_OUTCOME = UNKNOWN")
        click.echo("PROVIDER_BILLING_OUTCOME = UNKNOWN")
        click.echo("SAME_RUN_RETRY_ALLOWED = NO")
        click.echo("FRESH_D2_ALLOWED_AFTER_CLOSURE = YES (separate gates required)")
        click.echo("D6_ALLOWED = NO")
        click.echo(f"LEDGER_SHA_MATCH = {preview['ledger_sha256'] == args.expected_ledger_sha256}")
        click.echo(f"V2_EVIDENCE_SHA256 = {preview['evidence_sha256']}")
        click.echo(f"DRY_RUN = {not args.apply}")
        click.echo(f"LEDGER_MUTATED = {bool(args.apply)}")
        if args.apply:
            click.echo(f"RESULTING_STATE = {current_state}")
        return 0
    except (PhaseGError, OSError) as exc:
        click.echo(f"ERROR = {exc}", err=True)
        return 2
