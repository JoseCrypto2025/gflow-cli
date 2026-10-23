"""Sanitized evidence contract for a pre-approval TEST-session no-charge reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from .phase_g import PhaseGError

_SCHEMA_VERSION = "D2_TEST_SESSION_PREAPPROVAL_NO_CHARGE_V1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_RUN_ID_RE = re.compile(r"^D2_[A-Za-z0-9_-]{8,64}$")
_IDENTITY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
_EVIDENCE_KEYS = frozenset(
    {
        "SCHEMA_VERSION",
        "D2_RUN_ID",
        "CANONICAL_LEDGER_SHA256",
        "IMPLEMENTATION_COMMIT",
        "RUNTIME_STATE_ROOT",
        "TEST_PROFILE_KEY",
        "PROVIDER_ACCOUNT_ID",
        "PROVIDER_PROJECT_ID",
        "SESSION_ID",
        "CURRENT_SUBMISSION_STATE",
        "CURRENT_TERMINAL_STATE",
        "GENERATE_ACTION_COUNT",
        "APPROVAL_ACTION_COUNT",
        "REQUESTED_OUTPUT_COUNT",
        "AGENT_TURN_DISPATCHED",
        "OBSERVED_CONFIRMATION_ID",
        "OBSERVED_VIDEO_COUNT",
        "OBSERVED_CREDIT_COST",
        "OBSERVED_VISIBLE_BALANCE",
        "PROVIDER_EXECUTION_ID",
        "PROVIDER_MEDIA_ID",
        "PROVIDER_MEDIA_IDS",
        "BASELINE_MEDIA_IDS",
        "CURRENT_PROJECT_MEDIA_IDS",
        "BASELINE_EXECUTION_IDS",
        "CURRENT_PROJECT_EXECUTION_IDS",
        "BASELINE_CONFIRMATION_IDS",
        "CURRENT_PROJECT_CONFIRMATION_IDS",
        "BASELINE_CHAT_COUNT",
        "CURRENT_PROJECT_CHAT_COUNT",
        "PROVIDER_DIRECTIVE_SHA256",
        "NEW_AGENT_PROMPT_BUBBLE_INDEX",
        "PROVIDER_DIRECTIVE_MATCH_COUNT",
        "CHAT_ENUMERATION_COMPLETE",
        "BASELINE_PENDING_COUNT",
        "CURRENT_PROJECT_PENDING_COUNT",
        "NEW_FINISHED_MEDIA_IDS",
        "NEW_AGENT_ASSOCIATED_MEDIA_IDS",
        "PROJECT_SNAPSHOT_HYDRATED",
        "PROJECT_ENUMERATION_COMPLETE",
        "PROGRESS_BEFORE_CONFIRMATION",
        "PROVIDER_GENERATION_STARTED",
        "PROVIDER_REFUSAL",
        "WAF_REFUSAL",
        "NO_OTHER_ACCOUNT_ACTIVITY",
        "CREDITS_BEFORE",
        "CREDITS_AFTER",
        "CAPTURED_AT",
    }
)
_REQUIRED_FALSE = (
    "PROGRESS_BEFORE_CONFIRMATION",
    "PROVIDER_GENERATION_STARTED",
    "PROVIDER_REFUSAL",
    "WAF_REFUSAL",
)
_REQUIRED_TRUE = (
    "PROJECT_SNAPSHOT_HYDRATED",
    "PROJECT_ENUMERATION_COMPLETE",
    "CHAT_ENUMERATION_COMPLETE",
    "NO_OTHER_ACCOUNT_ACTIVITY",
)


def validate_preapproval_no_charge_evidence(
    evidence: Mapping[str, object], *, now: datetime | None = None
) -> dict[str, object]:
    """Validate complete, exact TEST-lane evidence without inferring from balance alone."""
    if frozenset(evidence) != _EVIDENCE_KEYS:
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INCOMPLETE",
            "pre-approval evidence fields are incomplete or unsupported",
        )
    if evidence.get("SCHEMA_VERSION") != _SCHEMA_VERSION:
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", "pre-approval evidence schema is invalid"
        )
    run_id = evidence.get("D2_RUN_ID")
    ledger_sha = evidence.get("CANONICAL_LEDGER_SHA256")
    commit = evidence.get("IMPLEMENTATION_COMMIT")
    if (
        not isinstance(run_id, str)
        or not _RUN_ID_RE.fullmatch(run_id)
        or not isinstance(ledger_sha, str)
        or not _SHA256_RE.fullmatch(ledger_sha)
        or not isinstance(commit, str)
        or not _COMMIT_RE.fullmatch(commit)
    ):
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", "run, digest, or commit identity is invalid"
        )
    if (
        evidence.get("TEST_PROFILE_KEY") != "GFLOW_V2_TEST_AUTH"
        or evidence.get("PROVIDER_ACCOUNT_ID") != "PVA_0001"
        or evidence.get("PROVIDER_PROJECT_ID") != "34e60953-e534-46e9-ac65-993893e61065"
        or not isinstance(evidence.get("SESSION_ID"), str)
        or not re.fullmatch(r"D2TS_[a-f0-9]{16,32}", str(evidence.get("SESSION_ID")))
        or not isinstance(evidence.get("RUNTIME_STATE_ROOT"), str)
        or not evidence.get("RUNTIME_STATE_ROOT")
    ):
        raise PhaseGError(
            "PHASE_G_EXECUTION_IDENTITY_MISMATCH", "evidence is not bound to the exact TEST lane"
        )
    for key in _REQUIRED_TRUE:
        if evidence.get(key) is not True:
            raise PhaseGError(
                "PHASE_G_TEST_SESSION_EVIDENCE_INCOMPLETE", "required positive evidence is absent"
            )
    for key in _REQUIRED_FALSE:
        if evidence.get(key) is not False:
            raise PhaseGError(
                "PHASE_G_TEST_SESSION_EVIDENCE_INCOMPLETE", "provider progress/refusal is ambiguous"
            )
    if (
        evidence.get("CURRENT_SUBMISSION_STATE") != "UNKNOWN_SUBMISSION_STATE"
        or evidence.get("CURRENT_TERMINAL_STATE") is not None
        or evidence.get("GENERATE_ACTION_COUNT") != 1
        or isinstance(evidence.get("GENERATE_ACTION_COUNT"), bool)
        or evidence.get("APPROVAL_ACTION_COUNT") != 0
        or isinstance(evidence.get("APPROVAL_ACTION_COUNT"), bool)
        or evidence.get("REQUESTED_OUTPUT_COUNT") != 1
        or isinstance(evidence.get("REQUESTED_OUTPUT_COUNT"), bool)
        or evidence.get("AGENT_TURN_DISPATCHED") is not True
        or evidence.get("OBSERVED_VIDEO_COUNT") != 1
        or isinstance(evidence.get("OBSERVED_VIDEO_COUNT"), bool)
        or evidence.get("PROVIDER_EXECUTION_ID") is not None
        or evidence.get("PROVIDER_MEDIA_ID") is not None
        or evidence.get("PROVIDER_MEDIA_IDS") != []
    ):
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INVALID",
            "attempt action or provider identity evidence differs",
        )
    confirmation_id = evidence.get("OBSERVED_CONFIRMATION_ID")
    quote_value = evidence.get("OBSERVED_CREDIT_COST")
    if (
        not isinstance(confirmation_id, str)
        or not confirmation_id
        or not isinstance(quote_value, str)
    ):
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", "observed confirmation or quote is invalid"
        )
    try:
        quote = Decimal(quote_value)
    except InvalidOperation as exc:
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", "observed quote is invalid"
        ) from exc
    if not quote.is_finite() or quote <= 0:
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", "observed quote must be positive and finite"
        )
    for key in (
        "BASELINE_MEDIA_IDS",
        "CURRENT_PROJECT_MEDIA_IDS",
        "BASELINE_EXECUTION_IDS",
        "CURRENT_PROJECT_EXECUTION_IDS",
        "BASELINE_CONFIRMATION_IDS",
        "CURRENT_PROJECT_CONFIRMATION_IDS",
        "NEW_FINISHED_MEDIA_IDS",
        "NEW_AGENT_ASSOCIATED_MEDIA_IDS",
    ):
        value = evidence.get(key)
        if not isinstance(value, list):
            raise PhaseGError(
                "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", f"{key} is invalid or duplicated"
            )
        items = cast(list[object], value)
        if any(not isinstance(item, str) or not _IDENTITY_RE.fullmatch(item) for item in items):
            raise PhaseGError(
                "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", f"{key} is invalid or duplicated"
            )
        string_items = cast(list[str], items)
        if len(string_items) != len(set(string_items)):
            raise PhaseGError(
                "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", f"{key} is invalid or duplicated"
            )
    baseline_media = cast(list[str], evidence["BASELINE_MEDIA_IDS"])
    current_media = cast(list[str], evidence["CURRENT_PROJECT_MEDIA_IDS"])
    baseline_execution = cast(list[str], evidence["BASELINE_EXECUTION_IDS"])
    current_execution = cast(list[str], evidence["CURRENT_PROJECT_EXECUTION_IDS"])
    baseline_confirmations = cast(list[str], evidence["BASELINE_CONFIRMATION_IDS"])
    current_confirmations = cast(list[str], evidence["CURRENT_PROJECT_CONFIRMATION_IDS"])
    baseline_chat_count = evidence.get("BASELINE_CHAT_COUNT")
    current_chat_count = evidence.get("CURRENT_PROJECT_CHAT_COUNT")
    directive_sha = evidence.get("PROVIDER_DIRECTIVE_SHA256")
    prompt_bubble_index = evidence.get("NEW_AGENT_PROMPT_BUBBLE_INDEX")
    directive_match_count = evidence.get("PROVIDER_DIRECTIVE_MATCH_COUNT")
    if (
        isinstance(baseline_chat_count, bool)
        or not isinstance(baseline_chat_count, int)
        or baseline_chat_count < 0
        or isinstance(current_chat_count, bool)
        or not isinstance(current_chat_count, int)
        or current_chat_count <= baseline_chat_count
        or not isinstance(directive_sha, str)
        or not _SHA256_RE.fullmatch(directive_sha)
        or isinstance(prompt_bubble_index, bool)
        or not isinstance(prompt_bubble_index, int)
        or not baseline_chat_count <= prompt_bubble_index < current_chat_count
        or directive_match_count != 1
        or isinstance(directive_match_count, bool)
    ):
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INCOMPLETE",
            "exact post-baseline provider directive turn is not proven",
        )
    if (
        confirmation_id in baseline_confirmations
        or set(current_media) != set(baseline_media)
        or set(current_execution) != set(baseline_execution)
        or set(current_confirmations) != set(baseline_confirmations) | {confirmation_id}
        or evidence["NEW_FINISHED_MEDIA_IDS"] != []
        or evidence["NEW_AGENT_ASSOCIATED_MEDIA_IDS"] != []
    ):
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INCOMPLETE",
            "current project contains activity not explained by the attempt baseline and gate",
        )
    for key in ("BASELINE_PENDING_COUNT", "CURRENT_PROJECT_PENDING_COUNT"):
        count = evidence.get(key)
        if isinstance(count, bool) or not isinstance(count, int) or count != 0:
            raise PhaseGError(
                "PHASE_G_TEST_SESSION_EVIDENCE_INCOMPLETE", "pending generation must be absent"
            )
    try:
        before = Decimal(str(evidence.get("CREDITS_BEFORE")))
        after = Decimal(str(evidence.get("CREDITS_AFTER")))
    except InvalidOperation as exc:
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", "provider balance evidence is invalid"
        ) from exc
    if not before.is_finite() or not after.is_finite() or before != after:
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INCOMPLETE", "provider balance changed during attempt"
        )
    visible_balance_value = evidence.get("OBSERVED_VISIBLE_BALANCE")
    if visible_balance_value is not None:
        try:
            visible_balance = Decimal(str(visible_balance_value))
        except InvalidOperation as exc:
            raise PhaseGError(
                "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", "observed gate balance is invalid"
            ) from exc
        if not visible_balance.is_finite() or visible_balance != before:
            raise PhaseGError(
                "PHASE_G_TEST_SESSION_EVIDENCE_INCOMPLETE",
                "visible gate balance differs from captured session balance",
            )
    captured_at = evidence.get("CAPTURED_AT")
    if not isinstance(captured_at, str):
        raise PhaseGError("PHASE_G_TEST_SESSION_EVIDENCE_INVALID", "evidence timestamp is invalid")
    try:
        parsed_at = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", "evidence timestamp is invalid"
        ) from exc
    current = now or datetime.now(UTC)
    if (
        parsed_at.tzinfo is None
        or parsed_at.utcoffset() is None
        or current.tzinfo is None
        or current.utcoffset() is None
        or parsed_at > current + timedelta(minutes=2)
        or current - parsed_at > timedelta(minutes=15)
    ):
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_STALE", "evidence timestamp is stale or invalid"
        )
    return {key: evidence[key] for key in sorted(_EVIDENCE_KEYS)}


def preapproval_no_charge_evidence_digest(evidence: Mapping[str, object]) -> str:
    """Return the canonical SHA-256 for a valid pre-approval evidence package."""
    summary = validate_preapproval_no_charge_evidence(evidence)
    encoded = json.dumps(
        summary,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_preapproval_no_charge_evidence(path: Path) -> tuple[dict[str, object], str]:
    """Read and validate one sanitized evidence JSON without exposing its content."""
    if path.is_symlink() or not path.is_file():
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_MISSING", "evidence file is missing or unsafe"
        )
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", "evidence JSON is unreadable"
        ) from exc
    if not isinstance(value, dict):
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_INVALID", "evidence JSON must be an object"
        )
    evidence = cast(dict[str, object], value)
    summary = validate_preapproval_no_charge_evidence(evidence)
    return summary, preapproval_no_charge_evidence_digest(summary)


def assert_evidence_matches_ledger(
    ledger_path: Path, *, d2_run_id: str, evidence: Mapping[str, object]
) -> None:
    """Check the dry-run evidence against the current read-only ledger snapshot."""
    try:
        value: object = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhaseGError("PHASE_G_LEDGER_CORRUPT", "D2 ledger is unreadable") from exc
    if not isinstance(value, dict):
        raise PhaseGError("PHASE_G_LEDGER_CORRUPT", "D2 ledger root is invalid")
    payload = cast(dict[str, object], value)
    rows = payload.get("attempts")
    sessions = payload.get("test_sessions")
    if not isinstance(rows, list) or not isinstance(sessions, list):
        raise PhaseGError("PHASE_G_LEDGER_CORRUPT", "D2 ledger data is invalid")
    attempts: list[dict[str, object]] = []
    for row_value in cast(list[object], rows):
        if isinstance(row_value, dict):
            row = cast(dict[str, object], row_value)
            if row.get("d2_run_id") == d2_run_id:
                attempts.append(row)
    if len(attempts) != 1:
        raise PhaseGError("PHASE_G_ATTEMPT_NOT_UNIQUE", "D2 run must exist exactly once")
    attempt = attempts[0]
    baseline_agent_turn_id = attempt.get("baseline_agent_turn_id")
    baseline_chat_count = (
        int(baseline_agent_turn_id.split(":", 1)[0])
        if isinstance(baseline_agent_turn_id, str)
        and baseline_agent_turn_id.split(":", 1)[0].isdigit()
        else None
    )
    session_id = evidence.get("SESSION_ID")
    matching_sessions: list[dict[str, object]] = []
    for session_value in cast(list[object], sessions):
        if isinstance(session_value, dict):
            session_row = cast(dict[str, object], session_value)
            if session_row.get("session_id") == session_id:
                matching_sessions.append(session_row)
    if len(matching_sessions) != 1:
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_NOT_FOUND", "evidence session does not exist exactly once"
        )
    session = matching_sessions[0]
    if (
        attempt.get("submission_state") != evidence.get("CURRENT_SUBMISSION_STATE")
        or attempt.get("terminal_state") != evidence.get("CURRENT_TERMINAL_STATE")
        or attempt.get("generate_action_count") != evidence.get("GENERATE_ACTION_COUNT")
        or attempt.get("approval_action_count") != evidence.get("APPROVAL_ACTION_COUNT")
        or attempt.get("requested_output_count") != evidence.get("REQUESTED_OUTPUT_COUNT")
        or attempt.get("agent_turn_dispatched") is not True
        or attempt.get("observed_confirmation_id") != evidence.get("OBSERVED_CONFIRMATION_ID")
        or attempt.get("observed_video_count") != evidence.get("OBSERVED_VIDEO_COUNT")
        or attempt.get("observed_credit_cost") != evidence.get("OBSERVED_CREDIT_COST")
        or attempt.get("observed_visible_balance") != evidence.get("OBSERVED_VISIBLE_BALANCE")
        or attempt.get("provider_execution_id") != evidence.get("PROVIDER_EXECUTION_ID")
        or attempt.get("provider_media_id") != evidence.get("PROVIDER_MEDIA_ID")
        or attempt.get("provider_media_ids") != evidence.get("PROVIDER_MEDIA_IDS")
        or attempt.get("test_session_id") != session_id
        or attempt.get("provider_project_id") != evidence.get("PROVIDER_PROJECT_ID")
        or attempt.get("provider_account_id") != evidence.get("PROVIDER_ACCOUNT_ID")
        or not isinstance(attempt.get("baseline_media_ids"), list)
        or set(cast(list[object], attempt["baseline_media_ids"]))
        != set(cast(list[object], evidence["BASELINE_MEDIA_IDS"]))
        or not isinstance(attempt.get("baseline_execution_ids"), list)
        or set(cast(list[object], attempt["baseline_execution_ids"]))
        != set(cast(list[object], evidence["BASELINE_EXECUTION_IDS"]))
        or not isinstance(attempt.get("baseline_confirmation_ids"), list)
        or set(cast(list[object], attempt["baseline_confirmation_ids"]))
        != set(cast(list[object], evidence["BASELINE_CONFIRMATION_IDS"]))
        or attempt.get("baseline_pending_count") != evidence.get("BASELINE_PENDING_COUNT")
        or baseline_chat_count != evidence.get("BASELINE_CHAT_COUNT")
        or attempt.get("provider_directive_sha256") != evidence.get("PROVIDER_DIRECTIVE_SHA256")
        or session.get("in_flight_run_id") != d2_run_id
        or session.get("status") != "STOPPED"
        or session.get("stopped_reason") != "UNKNOWN_OR_NONTERMINAL_OUTCOME"
        or str(evidence.get("RUNTIME_STATE_ROOT")) != str(ledger_path.parent.parent.resolve())
    ):
        raise PhaseGError(
            "PHASE_G_TEST_SESSION_EVIDENCE_MISMATCH",
            "current ledger attempt/session differs from the evidence package",
        )
