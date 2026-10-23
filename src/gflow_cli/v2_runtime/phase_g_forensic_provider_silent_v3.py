"""Fail-closed V3 contract for provider-silent unknown D2 closure."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

import click

from .agent_recovery import AgentSettingsRecoveryJournal, RecoveryJournalError
from .phase_g import D2DailyCreditLedger, PhaseGError
from .phase_g_artifacts import sha256_file
from .phase_g_forensic_common import (
    add_operator_arguments,
    preview_result,
    sanitized_reason,
    timestamp_error,
    verified_ledger_attempt,
)

_SCHEMA_VERSION = "D2_FORENSIC_PROVIDER_SILENT_CLOSURE_V3"
_RUN_ID = "D2_8d892c38cc8f4c99"
_ACCOUNT_ID = "PVA_0001"
_PROJECT_ID = "34e60953-e534-46e9-ac65-993893e61065"
_PROJECT_NAME = "AI Video Automation V2 - TEST"
_ACCOUNTING_DATE = "2026-10-23"
_ACCOUNTED_CREDIT_COST = "50"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_REASON_RE = re.compile(r"^[A-Za-z0-9 .,:;()/_-]{1,240}$")
_EVIDENCE_KEYS = frozenset(
    {
        "SCHEMA_VERSION",
        "D2_RUN_ID",
        "CANONICAL_LEDGER_SHA256",
        "IMPLEMENTATION_COMMIT",
        "PROVIDER_ACCOUNT_ID",
        "PROVIDER_PROJECT_ID",
        "PROVIDER_PROJECT_NAME",
        "CURRENT_SUBMISSION_STATE",
        "CURRENT_TERMINAL_STATE",
        "GENERATE_ACTION_COUNT",
        "APPROVAL_ACTION_COUNT",
        "ACCOUNTING_DATE_EXPECTED",
        "ACCOUNTED_CREDIT_COST_EXPECTED",
        "SCENE_ID",
        "SCENE_PROMPT_ID",
        "GENERATION_PROFILE_ID",
        "PROVIDER_ID",
        "MODEL_ID",
        "GENERATION_MODE",
        "PROMPT_SHA256",
        "SOURCE_CONTEXT_SHA256",
        "REQUESTED_DURATION_SEC",
        "REQUESTED_ASPECT_RATIO",
        "REQUESTED_OUTPUT_COUNT",
        "BASELINE_CAPTURED",
        "BASELINE_PENDING_COUNT",
        "BASELINE_EXECUTION_IDS",
        "BASELINE_MEDIA_IDS",
        "BASELINE_CONFIRMATION_IDS",
        "RECOVERY_JOURNAL_CLEAN",
        "PROVIDER_EXECUTION_ID_ABSENT",
        "PROVIDER_MEDIA_ID_ABSENT",
        "PROVIDER_MEDIA_IDS_ABSENT",
        "LOCAL_ATTEMPT_OUTPUT_ABSENT",
        "REMOTE_UPLOAD_ABSENT",
        "REMOTE_VERIFIED_ABSENT",
        "LIVE_PROJECT_HYDRATED",
        "LIVE_SNAPSHOT_COUNT",
        "LIVE_OBSERVATION_DURATION_SECONDS",
        "LIVE_PENDING_COUNT",
        "LIVE_CONFIRMATION_COUNT",
        "LIVE_CONFIRMATION_IDS",
        "LIVE_STRUCTURED_RECORDS_ENUMERATED",
        "LIVE_STRUCTURED_RECORD_COUNT",
        "LIVE_STRUCTURED_RECORDS",
        "LIVE_SNAPSHOTS",
        "HISTORICAL_EXECUTION_IDS",
        "HISTORICAL_MEDIA_IDS",
        "CANDIDATE_NEW_EXECUTION_IDS",
        "CANDIDATE_NEW_MEDIA_IDS",
        "CANDIDATE_NEW_EXECUTION_COUNT",
        "CANDIDATE_NEW_MEDIA_COUNT",
        "ALL_LIVE_STRUCTURED_IDS_HISTORICAL",
        "CHAT_ACTIVITY_PRESENT",
        "CHAT_COUNT",
        "CHAT_HASH_PREFIX_STABLE",
        "CHAT_STABLE_IDS_AVAILABLE",
        "CHAT_TIMESTAMPS_AVAILABLE",
        "CHAT_IDENTIFIER_SCAN_COMPLETE",
        "CHAT_PROVIDER_IDENTIFIER_PROOF",
        "CHAT_PROVIDER_IDENTIFIERS",
        "CHAT_ATTRIBUTION_AUTHORITY",
        "ATTRIBUTION_CLASSIFICATION",
        "PROVIDER_OUTCOME",
        "CREDIT_OUTCOME",
        "PROVIDER_BILLING_OUTCOME",
        "SAFE_AUTOMATIC_RETRY",
        "FORENSIC_COMPLETED_AT",
        "FORENSIC_REASON",
    }
)


def _int(value: object, *, field: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", f"{field} is invalid")
    return value


def _string_list(value: object, *, field: str, pattern: re.Pattern[str]) -> list[str]:
    if not isinstance(value, list):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", f"{field} is invalid")
    items = cast(list[object], value)
    if any(not isinstance(item, str) or not pattern.fullmatch(item) for item in items):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", f"{field} is invalid")
    strings = cast(list[str], items)
    if len(strings) != len(set(strings)):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", f"{field} contains duplicates")
    return strings


def _check_snapshot(
    snapshot: object, *, historical_exec: set[str], historical_media: set[str]
) -> tuple[set[str], set[str]]:
    if not isinstance(snapshot, dict):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "live snapshot shape is invalid")
    row = cast(dict[str, object], snapshot)
    if frozenset(row) != frozenset(
        {
            "ELAPSED_SECONDS",
            "HYDRATED",
            "ENUMERATION_COMPLETE",
            "PENDING_COUNT",
            "CONFIRMATION_IDS",
            "EXECUTION_IDS",
            "MEDIA_IDS",
            "DOM_MEDIA_IDS",
            "DOM_FINISHED_MEDIA_IDS",
        }
    ):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "live snapshot shape is invalid")
    if row.get("HYDRATED") is not True or row.get("ENUMERATION_COMPLETE") is not True:
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "live snapshot is not complete")
    _int(row.get("ELAPSED_SECONDS"), field="snapshot elapsed seconds")
    if _int(row.get("PENDING_COUNT"), field="snapshot pending count") != 0:
        raise PhaseGError("PHASE_G_PROVIDER_OUTCOME_PRESENT", "project has pending generation")
    confirmations = _string_list(
        row.get("CONFIRMATION_IDS"), field="snapshot confirmation IDs", pattern=_IDENTIFIER_RE
    )
    if confirmations:
        raise PhaseGError("PHASE_G_PROVIDER_OUTCOME_PRESENT", "project has a confirmation")
    executions = set(
        _string_list(
            row.get("EXECUTION_IDS"), field="snapshot execution IDs", pattern=_IDENTIFIER_RE
        )
    )
    media = set(_string_list(row.get("MEDIA_IDS"), field="snapshot media IDs", pattern=_UUID_RE))
    dom_media = set(_string_list(row.get("DOM_MEDIA_IDS"), field="DOM media IDs", pattern=_UUID_RE))
    dom_finished_media = set(
        _string_list(
            row.get("DOM_FINISHED_MEDIA_IDS"),
            field="DOM finished media IDs",
            pattern=_UUID_RE,
        )
    )
    if (
        not executions.issubset(historical_exec)
        or not media.issubset(historical_media)
        or not dom_media.issubset(historical_media)
        or not dom_finished_media.issubset(historical_media)
    ):
        raise PhaseGError(
            "PHASE_G_PROVIDER_OUTCOME_PRESENT",
            "snapshot contains a nonhistorical provider identity",
        )
    return executions, media


def validate_provider_silent_evidence_v3(evidence: Mapping[str, object]) -> dict[str, object]:
    """Validate the exact sanitized V3 package, treating chat only as context."""
    if frozenset(evidence) != _EVIDENCE_KEYS:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "V3 evidence is incomplete or unsupported"
        )
    if evidence.get("SCHEMA_VERSION") != _SCHEMA_VERSION or evidence.get("D2_RUN_ID") != _RUN_ID:
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "V3 target identity is invalid")
    for key in ("CANONICAL_LEDGER_SHA256", "PROMPT_SHA256", "SOURCE_CONTEXT_SHA256"):
        value = evidence.get(key)
        if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
            raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", f"{key} is invalid")
    commit = evidence.get("IMPLEMENTATION_COMMIT")
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise PhaseGError("PHASE_G_FORENSIC_COMMIT_INVALID", "implementation commit is invalid")
    if (
        evidence.get("PROVIDER_ACCOUNT_ID") != _ACCOUNT_ID
        or evidence.get("PROVIDER_PROJECT_ID") != _PROJECT_ID
        or evidence.get("PROVIDER_PROJECT_NAME") != _PROJECT_NAME
        or evidence.get("CURRENT_SUBMISSION_STATE") != "UNKNOWN_SUBMISSION_STATE"
        or evidence.get("CURRENT_TERMINAL_STATE") != "NONE"
        or evidence.get("GENERATE_ACTION_COUNT") != 1
        or isinstance(evidence.get("GENERATE_ACTION_COUNT"), bool)
        or evidence.get("APPROVAL_ACTION_COUNT") != 0
        or isinstance(evidence.get("APPROVAL_ACTION_COUNT"), bool)
        or evidence.get("ACCOUNTING_DATE_EXPECTED") != _ACCOUNTING_DATE
        or evidence.get("ACCOUNTED_CREDIT_COST_EXPECTED") != _ACCOUNTED_CREDIT_COST
        or evidence.get("GENERATION_MODE") != "TEXT_TO_VIDEO"
        or evidence.get("REQUESTED_OUTPUT_COUNT") != 1
        or isinstance(evidence.get("REQUESTED_OUTPUT_COUNT"), bool)
    ):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "evidence does not match V3 attempt")
    for field in (
        "SCENE_ID",
        "SCENE_PROMPT_ID",
        "GENERATION_PROFILE_ID",
        "PROVIDER_ID",
        "MODEL_ID",
    ):
        value = evidence.get(field)
        if not isinstance(value, str) or not value or len(value) > 128:
            raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", f"{field} is invalid")
    duration = evidence.get("REQUESTED_DURATION_SEC")
    if isinstance(duration, bool) or not isinstance(duration, (float, int)) or duration <= 0:
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "requested duration is invalid")
    aspect = evidence.get("REQUESTED_ASPECT_RATIO")
    if not isinstance(aspect, str) or not aspect or len(aspect) > 16:
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "requested aspect ratio is invalid")
    required_true = (
        "BASELINE_CAPTURED",
        "RECOVERY_JOURNAL_CLEAN",
        "PROVIDER_EXECUTION_ID_ABSENT",
        "PROVIDER_MEDIA_ID_ABSENT",
        "PROVIDER_MEDIA_IDS_ABSENT",
        "LOCAL_ATTEMPT_OUTPUT_ABSENT",
        "REMOTE_UPLOAD_ABSENT",
        "REMOTE_VERIFIED_ABSENT",
        "LIVE_PROJECT_HYDRATED",
        "LIVE_STRUCTURED_RECORDS_ENUMERATED",
        "ALL_LIVE_STRUCTURED_IDS_HISTORICAL",
        "CHAT_IDENTIFIER_SCAN_COMPLETE",
    )
    if any(evidence.get(key) is not True for key in required_true):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "required V3 evidence is not verified"
        )
    if (
        evidence.get("BASELINE_CAPTURED") is not True
        or _int(evidence.get("BASELINE_PENDING_COUNT"), field="baseline pending count") != 0
    ):
        raise PhaseGError(
            "PHASE_G_BASELINE_INVALID", "baseline must be captured with no pending work"
        )
    baseline_exec = _string_list(
        evidence.get("BASELINE_EXECUTION_IDS"),
        field="baseline execution IDs",
        pattern=_IDENTIFIER_RE,
    )
    baseline_media = _string_list(
        evidence.get("BASELINE_MEDIA_IDS"), field="baseline media IDs", pattern=_UUID_RE
    )
    _string_list(
        evidence.get("BASELINE_CONFIRMATION_IDS"),
        field="baseline confirmation IDs",
        pattern=_IDENTIFIER_RE,
    )
    historical_exec = set(
        _string_list(
            evidence.get("HISTORICAL_EXECUTION_IDS"),
            field="historical execution IDs",
            pattern=_IDENTIFIER_RE,
        )
    )
    historical_media = set(
        _string_list(
            evidence.get("HISTORICAL_MEDIA_IDS"), field="historical media IDs", pattern=_UUID_RE
        )
    )
    if not set(baseline_exec).issubset(historical_exec) or not set(baseline_media).issubset(
        historical_media
    ):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "baseline IDs are not historical")
    if _int(evidence.get("LIVE_PENDING_COUNT"), field="live pending count") != 0:
        raise PhaseGError("PHASE_G_PROVIDER_OUTCOME_PRESENT", "project has pending generation")
    if _int(evidence.get("LIVE_CONFIRMATION_COUNT"), field="live confirmation count") != 0:
        raise PhaseGError("PHASE_G_PROVIDER_OUTCOME_PRESENT", "project has a confirmation")
    live_confirmations = _string_list(
        evidence.get("LIVE_CONFIRMATION_IDS"), field="live confirmation IDs", pattern=_IDENTIFIER_RE
    )
    if live_confirmations:
        raise PhaseGError("PHASE_G_PROVIDER_OUTCOME_PRESENT", "project has a confirmation")

    records_value = evidence.get("LIVE_STRUCTURED_RECORDS")
    if not isinstance(records_value, list):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "structured records are invalid")
    records = cast(list[object], records_value)
    if _int(evidence.get("LIVE_STRUCTURED_RECORD_COUNT"), field="structured record count") != len(
        records
    ):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "structured record count differs")
    record_exec: set[str] = set()
    record_media: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "structured record is malformed")
        record = cast(dict[str, object], item)
        if frozenset(record) != frozenset({"WORKFLOW_ID", "PROJECT_ID", "MEDIA_ID", "STATUS"}):
            raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "structured record is malformed")
        workflow = record.get("WORKFLOW_ID")
        media_id = record.get("MEDIA_ID")
        status = record.get("STATUS")
        if (
            not isinstance(workflow, str)
            or not _IDENTIFIER_RE.fullmatch(workflow)
            or record.get("PROJECT_ID") != _PROJECT_ID
            or not isinstance(media_id, str)
            or not _UUID_RE.fullmatch(media_id)
            or isinstance(status, bool)
            or not isinstance(status, int)
        ):
            raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "structured record is malformed")
        record_exec.add(workflow)
        record_media.add(media_id.lower())
    if not record_exec.issubset(historical_exec) or not record_media.issubset(historical_media):
        raise PhaseGError(
            "PHASE_G_PROVIDER_OUTCOME_PRESENT", "new structured provider output exists"
        )

    snapshots_value = evidence.get("LIVE_SNAPSHOTS")
    if not isinstance(snapshots_value, list):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "live snapshots are invalid")
    snapshots = cast(list[object], snapshots_value)
    snapshot_count = _int(evidence.get("LIVE_SNAPSHOT_COUNT"), field="snapshot count", minimum=3)
    if len(snapshots) != snapshot_count:
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "snapshot count differs")
    first_elapsed: int | None = None
    last_elapsed: int | None = None
    previous_elapsed: int | None = None
    snapshot_exec: set[str] = set()
    snapshot_media: set[str] = set()
    previous_snapshot_identity: tuple[frozenset[str], ...] | None = None
    for snapshot in snapshots:
        exec_ids, media_ids = _check_snapshot(
            snapshot, historical_exec=historical_exec, historical_media=historical_media
        )
        snap = cast(dict[str, object], snapshot)
        elapsed = cast(int, snap["ELAPSED_SECONDS"])
        if previous_elapsed is not None and elapsed <= previous_elapsed:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INVALID", "live snapshot times must strictly increase"
            )
        first_elapsed = elapsed if first_elapsed is None else first_elapsed
        last_elapsed = elapsed
        previous_elapsed = elapsed
        snapshot_exec.update(exec_ids)
        snapshot_media.update(media_ids)
        snapshot_identity = (
            frozenset(exec_ids),
            frozenset(media_ids),
            frozenset(
                _string_list(snap.get("DOM_MEDIA_IDS"), field="DOM media IDs", pattern=_UUID_RE)
            ),
            frozenset(
                _string_list(
                    snap.get("DOM_FINISHED_MEDIA_IDS"),
                    field="DOM finished media IDs",
                    pattern=_UUID_RE,
                )
            ),
        )
        if (
            previous_snapshot_identity is not None
            and snapshot_identity != previous_snapshot_identity
        ):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                "structured provider snapshots changed during observation",
            )
        previous_snapshot_identity = snapshot_identity
    duration_seconds = _int(
        evidence.get("LIVE_OBSERVATION_DURATION_SECONDS"), field="observation duration"
    )
    if first_elapsed != 0 or last_elapsed != duration_seconds or not 60 <= duration_seconds <= 120:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "observation span is outside 60-120 seconds"
        )
    if not snapshot_exec.issubset(record_exec) or not snapshot_media.issubset(record_media):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
            "snapshot identities lack structured record evidence",
        )
    candidate_exec = _string_list(
        evidence.get("CANDIDATE_NEW_EXECUTION_IDS"),
        field="candidate execution IDs",
        pattern=_IDENTIFIER_RE,
    )
    candidate_media = _string_list(
        evidence.get("CANDIDATE_NEW_MEDIA_IDS"), field="candidate media IDs", pattern=_UUID_RE
    )
    if candidate_exec or candidate_media:
        raise PhaseGError(
            "PHASE_G_PROVIDER_OUTCOME_PRESENT", "new structured provider candidate exists"
        )
    if (
        _int(evidence.get("CANDIDATE_NEW_EXECUTION_COUNT"), field="candidate execution count") != 0
        or _int(evidence.get("CANDIDATE_NEW_MEDIA_COUNT"), field="candidate media count") != 0
    ):
        raise PhaseGError(
            "PHASE_G_PROVIDER_OUTCOME_PRESENT", "new structured provider candidate exists"
        )
    chat_count = _int(evidence.get("CHAT_COUNT"), field="chat count")
    if evidence.get("CHAT_ACTIVITY_PRESENT") is not (chat_count > 0):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "chat activity flag differs")
    if (
        not isinstance(evidence.get("CHAT_HASH_PREFIX_STABLE"), bool)
        or evidence.get("CHAT_ATTRIBUTION_AUTHORITY") != "NON_AUTHORITATIVE"
        or evidence.get("CHAT_PROVIDER_IDENTIFIER_PROOF") != "NONE"
        or evidence.get("CHAT_PROVIDER_IDENTIFIERS") != []
    ):
        raise PhaseGError(
            "PHASE_G_PROVIDER_OUTCOME_PRESENT",
            "chat identifiers are ambiguous or chat became authoritative",
        )
    if not isinstance(evidence.get("CHAT_STABLE_IDS_AVAILABLE"), bool) or not isinstance(
        evidence.get("CHAT_TIMESTAMPS_AVAILABLE"), bool
    ):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "chat metadata flags are invalid")
    if (
        evidence.get("ATTRIBUTION_CLASSIFICATION") != "NO_PROVIDER_EVIDENCE"
        or evidence.get("PROVIDER_OUTCOME") != "UNKNOWN"
        or evidence.get("CREDIT_OUTCOME") != "UNKNOWN"
        or evidence.get("PROVIDER_BILLING_OUTCOME") != "UNKNOWN"
        or evidence.get("SAFE_AUTOMATIC_RETRY") is not False
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "V3 cannot resolve provider outcomes"
        )
    if timestamp_error(evidence.get("FORENSIC_COMPLETED_AT")) is not None:
        raise PhaseGError(
            "PHASE_G_FORENSIC_TIME_INVALID", "forensic completion time needs UTC offset"
        )
    if not sanitized_reason(evidence.get("FORENSIC_REASON"), _REASON_RE):
        raise PhaseGError("PHASE_G_FORENSIC_REASON_INVALID", "forensic reason is invalid")
    return {key: evidence[key] for key in sorted(_EVIDENCE_KEYS)}


def forensic_provider_silent_digest_v3(evidence: Mapping[str, object]) -> str:
    summary = validate_provider_silent_evidence_v3(evidence)
    payload = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def load_provider_silent_evidence_v3(path: Path) -> tuple[dict[str, object], str]:
    if path.is_symlink() or not path.is_file():
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_MISSING", "V3 evidence is missing or unsafe")
    try:
        raw = path.read_bytes()
        value: object = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "V3 evidence JSON is unreadable"
        ) from exc
    if not isinstance(value, dict):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "V3 evidence must be an object")
    summary = validate_provider_silent_evidence_v3(cast(dict[str, object], value))
    canonical = json.dumps(
        summary, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if raw != canonical:
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_NONCANONICAL", "V3 evidence must be canonical JSON"
        )
    return summary, hashlib.sha256(raw).hexdigest()


def _ledger_history(
    payload: Mapping[str, object], run_id: str
) -> tuple[set[str], set[str], Mapping[str, object]]:
    rows_value = payload.get("attempts")
    if not isinstance(rows_value, list):
        raise PhaseGError("PHASE_G_LEDGER_INVALID", "ledger attempts are invalid")
    rows = cast(list[Mapping[str, object]], rows_value)
    matches = [row for row in rows if row.get("d2_run_id") == run_id]
    if len(matches) != 1:
        raise PhaseGError("PHASE_G_ATTEMPT_NOT_UNIQUE", "D2 run ID must exist exactly once")
    target_index = rows.index(matches[0])
    target = matches[0]
    if target.get("provider_project_id") != _PROJECT_ID:
        raise PhaseGError(
            "PHASE_G_FORENSIC_IDENTITY_MISMATCH", "attempt is not bound to the TEST project"
        )
    executions: set[str] = set()
    media: set[str] = set()
    for prior in rows[:target_index]:
        if prior.get("provider_project_id") != _PROJECT_ID:
            continue
        execution = prior.get("provider_execution_id")
        if isinstance(execution, str) and execution:
            executions.add(execution)
        provider_media_id = prior.get("provider_media_id")
        if isinstance(provider_media_id, str) and provider_media_id:
            media.add(provider_media_id.lower())
        media_ids = prior.get("provider_media_ids", [])
        if isinstance(media_ids, list):
            media.update(
                item.lower()
                for item in cast(list[object], media_ids)
                if isinstance(item, str) and item
            )
    baseline_exec = target.get("baseline_execution_ids", [])
    baseline_media = target.get("baseline_media_ids", [])
    if isinstance(baseline_exec, list):
        executions.update(
            item for item in cast(list[object], baseline_exec) if isinstance(item, str)
        )
    if isinstance(baseline_media, list):
        media.update(
            item.lower() for item in cast(list[object], baseline_media) if isinstance(item, str)
        )
    return executions, media, target


def validate_provider_silent_attempt_binding_v3(
    runtime_state_root: Path,
    payload: Mapping[str, object],
    attempt: Mapping[str, object],
    evidence: Mapping[str, object],
) -> None:
    historical_exec, historical_media, target = _ledger_history(payload, str(evidence["D2_RUN_ID"]))
    if dict(target) != dict(attempt):
        raise PhaseGError("PHASE_G_LEDGER_CHANGED", "attempt changed during V3 validation")
    expected_pairs = {
        "provider_account_id": "PROVIDER_ACCOUNT_ID",
        "provider_project_id": "PROVIDER_PROJECT_ID",
        "scene_id": "SCENE_ID",
        "scene_prompt_id": "SCENE_PROMPT_ID",
        "generation_profile_id": "GENERATION_PROFILE_ID",
        "provider_id": "PROVIDER_ID",
        "model_id": "MODEL_ID",
        "generation_mode": "GENERATION_MODE",
        "prompt_sha256": "PROMPT_SHA256",
        "source_context_sha256": "SOURCE_CONTEXT_SHA256",
        "requested_duration_sec": "REQUESTED_DURATION_SEC",
        "requested_aspect_ratio": "REQUESTED_ASPECT_RATIO",
        "requested_output_count": "REQUESTED_OUTPUT_COUNT",
        "accounting_date": "ACCOUNTING_DATE_EXPECTED",
        "accounted_credit_cost": "ACCOUNTED_CREDIT_COST_EXPECTED",
    }
    if any(attempt.get(row) != evidence.get(key) for row, key in expected_pairs.items()):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "V3 evidence differs from persisted attempt"
        )
    generate_count = attempt.get("generate_action_count")
    approval_count = attempt.get("approval_action_count")
    baseline_pending = attempt.get("baseline_pending_count")
    remote_verified = attempt.get("remote_verified")
    if (
        attempt.get("d2_run_id") != _RUN_ID
        or attempt.get("submission_state") != "UNKNOWN_SUBMISSION_STATE"
        or attempt.get("terminal_state") is not None
        or isinstance(generate_count, bool)
        or generate_count != 1
        or isinstance(approval_count, bool)
        or approval_count != 0
        or attempt.get("baseline_captured") is not True
        or isinstance(baseline_pending, bool)
        or baseline_pending != 0
        or attempt.get("provider_execution_id") is not None
        or attempt.get("provider_media_id") is not None
        or attempt.get("provider_media_ids") != []
        or attempt.get("confirmation_id") is not None
        or attempt.get("observed_confirmation_id") is not None
        or attempt.get("provider_outcome", "UNKNOWN") != "UNKNOWN"
        or attempt.get("credit_outcome", "UNKNOWN") != "UNKNOWN"
        or attempt.get("provider_billing_outcome") != "UNKNOWN"
        or attempt.get("remote_file_id") is not None
        or attempt.get("local_sha256") is not None
        or attempt.get("remote_sha256") is not None
        or (remote_verified is not None and not isinstance(remote_verified, bool))
        or remote_verified is True
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_STATE_MISMATCH", "persisted attempt is outside V3 shape"
        )
    if evidence.get("BASELINE_EXECUTION_IDS") != attempt.get("baseline_execution_ids"):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "baseline execution IDs differ")
    if evidence.get("BASELINE_MEDIA_IDS") != attempt.get("baseline_media_ids"):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "baseline media IDs differ")
    if evidence.get("BASELINE_CONFIRMATION_IDS") != attempt.get("baseline_confirmation_ids"):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INVALID", "baseline confirmation IDs differ")
    if (
        set(cast(list[str], evidence["HISTORICAL_EXECUTION_IDS"])) != historical_exec
        or {item.lower() for item in cast(list[str], evidence["HISTORICAL_MEDIA_IDS"])}
        != historical_media
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "historical ID set differs from ledger"
        )
    local_root = attempt.get("temp_root")
    if not isinstance(local_root, str) or not Path(local_root).is_absolute():
        raise PhaseGError("PHASE_G_LEDGER_INVALID", "attempt temp root is invalid")
    local_attempt_root = Path(local_root) / _RUN_ID
    if local_attempt_root.is_symlink() or local_attempt_root.exists():
        raise PhaseGError("PHASE_G_ARTIFACT_PRESENT", "local attempt output exists")
    journal = AgentSettingsRecoveryJournal(runtime_state_root, _ACCOUNT_ID, _PROJECT_ID)
    try:
        record = journal.read()
    except RecoveryJournalError as exc:
        raise PhaseGError(
            "PHASE_G_RECOVERY_JOURNAL_UNCLEAN", "recovery journal is unreadable"
        ) from exc
    if record is None or record.state != "CLEAN":
        raise PhaseGError("PHASE_G_RECOVERY_JOURNAL_UNCLEAN", "recovery journal is not CLEAN")


def forensic_provider_silent_preview_v3(
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
    summary = validate_provider_silent_evidence_v3(evidence)
    if (
        d2_run_id != _RUN_ID
        or summary["D2_RUN_ID"] != d2_run_id
        or summary["IMPLEMENTATION_COMMIT"] != implementation_commit
        or summary["CANONICAL_LEDGER_SHA256"] != expected_ledger_sha256
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INVALID", "V3 run, commit, or ledger binding differs"
        )
    digest = forensic_provider_silent_digest_v3(summary)
    if digest != evidence_sha256 or evidence_sha256 != expected_evidence_sha256:
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_HASH_MISMATCH", "V3 evidence SHA differs")
    actual_sha, attempt = verified_ledger_attempt(
        runtime_state_root, d2_run_id=d2_run_id, expected_sha256=expected_ledger_sha256
    )
    if (
        expected_state != "UNKNOWN_SUBMISSION_STATE"
        or attempt.get("submission_state") != expected_state
    ):
        raise PhaseGError("PHASE_G_FORENSIC_STATE_MISMATCH", "V3 requires unknown submission state")
    ledger = D2DailyCreditLedger(runtime_state_root)
    rows = ledger.attempts()
    if sha256_file(ledger.path) != expected_ledger_sha256:
        raise PhaseGError("PHASE_G_LEDGER_CHANGED", "ledger changed during V3 preview")
    payload = {"attempts": [dict(row) for row in rows]}
    current_attempt = next((row for row in rows if row.get("d2_run_id") == d2_run_id), None)
    if current_attempt is None:
        raise PhaseGError(
            "PHASE_G_ATTEMPT_NOT_FOUND", "D2 attempt does not exist in the local ledger"
        )
    validate_provider_silent_attempt_binding_v3(
        runtime_state_root, payload, current_attempt, summary
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
    add_operator_arguments(parser, apply_help="apply the reviewed V3 provider-silent closure")
    args = parser.parse_args(argv)
    try:
        evidence, evidence_sha = load_provider_silent_evidence_v3(args.evidence_json)
        preview = forensic_provider_silent_preview_v3(
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
        if args.apply:
            closed = D2DailyCreditLedger(args.runtime_state_root).close_provider_silent_forensic_v3(
                args.d2_run_id,
                expected_ledger_sha256=args.expected_ledger_sha256,
                evidence_sha256=evidence_sha,
                implementation_commit=args.implementation_commit,
                evidence=evidence,
            )
            state = str(closed["submission_state"])
        else:
            state = str(attempt["submission_state"])
        click.echo(f"CURRENT_STATE = {attempt['submission_state']}")
        click.echo("PROPOSED_STATE = FORENSICALLY_CLOSED_UNKNOWN")
        click.echo("ATTRIBUTION_CLASSIFICATION = NO_PROVIDER_EVIDENCE")
        click.echo(f"ACCOUNTED_CREDIT_COST = {attempt.get('accounted_credit_cost')} / unchanged")
        click.echo("PROVIDER_OUTCOME = UNKNOWN")
        click.echo("CREDIT_OUTCOME = UNKNOWN")
        click.echo("PROVIDER_BILLING_OUTCOME = UNKNOWN")
        click.echo("SAFE_AUTOMATIC_RETRY = NO")
        click.echo(f"V3_EVIDENCE_SHA256 = {evidence_sha}")
        click.echo(f"DRY_RUN = {not args.apply}")
        click.echo(f"LEDGER_MUTATED = {bool(args.apply)}")
        if args.apply:
            click.echo(f"RESULTING_STATE = {state}")
        return 0
    except (PhaseGError, OSError) as exc:
        click.echo(f"ERROR = {exc}", err=True)
        return 2
