"""Durable policy records for legacy attempts with exhausted ambiguous evidence."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import cast

POLICY_VERSION = "AMBIGUOUS_LEGACY_RECOVERY_POLICY_V1"
POLICY_ID_PREFIX = "ALRP_"
AUDIT_NAME = "ambiguous-legacy-recovery-policy-audit.json"
UNKNOWN = "UNKNOWN"


class RecoveryPolicyError(ValueError):
    """Policy precondition or durable audit validation failure."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RecoveryPolicyError("policy audit payload is not canonical JSON") from exc


def build_policy_record(
    *,
    attempt_id: str,
    production_job_id: str,
    scene_job_id: str,
    reservation_id: str,
    observed_at: str,
) -> dict[str, object]:
    """Describe quarantine and unresolved exposure without changing credit state."""
    if not all((attempt_id, production_job_id, scene_job_id, reservation_id, observed_at)):
        raise RecoveryPolicyError(
            "exact attempt and linked job/reservation identities are required"
        )
    lineage_key = hashlib.sha256(f"legacy-replacement-lineage-v1:{attempt_id}".encode()).hexdigest()
    policy_id = (
        POLICY_ID_PREFIX
        + hashlib.sha256(f"{POLICY_VERSION}:{attempt_id}".encode()).hexdigest()[:24].upper()
    )
    return {
        "policy_id": policy_id,
        "policy_version": POLICY_VERSION,
        "attempt_id": attempt_id,
        "production_job_id": production_job_id,
        "scene_job_id": scene_job_id,
        "reservation_id": reservation_id,
        "reservation_credits": 50,
        "reservation_state": "UNKNOWN_SUBMISSION_STATE",
        "reconciliation_result": "AMBIGUOUS_AFTER_RECONCILIATION",
        "evidence_recovery_result": "AMBIGUOUS_EVIDENCE_EXHAUSTED",
        "provider_submission": UNKNOWN,
        "billing_state": UNKNOWN,
        "production_job_target": "QUARANTINED",
        "scene_job_target": "QUARANTINED",
        "automatic_retry": "PROHIBITED",
        "automatic_resume": "PROHIBITED",
        "generate_authorized": False,
        "reservation_mutation_authorized": False,
        "unresolved_credit_exposure": {
            "held_reservation_credits": 50,
            "held_reservation_state": "UNKNOWN_SUBMISSION_STATE",
            "provider_cost_realized": UNKNOWN,
            "replacement_reserve_credits": UNKNOWN,
            "combined_exposure": "UNKNOWN_UNQUOTED",
        },
        "replacement": {
            "eligible_after_quarantine": True,
            "authorized": False,
            "attempt_creation_authorized": False,
            "predecessor_attempt_id": attempt_id,
            "lineage_key": lineage_key,
            "unique_replacement_per_predecessor": True,
        },
        "source": "USER_CONFIRMED_HANDOFF",
        "observed_at": observed_at,
    }


def _load_audit(path: Path) -> dict[str, object]:
    if path.is_symlink():
        raise RecoveryPolicyError("policy audit path may not be a symlink")
    if not path.exists():
        return {"schema_version": 1, "events": []}
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryPolicyError("policy audit is unreadable") from exc
    if not isinstance(value, dict):
        raise RecoveryPolicyError("policy audit schema is invalid")
    audit_value = cast(dict[str, object], value)
    if audit_value.get("schema_version") != 1:
        raise RecoveryPolicyError("policy audit schema is invalid")
    events = audit_value.get("events")
    if not isinstance(events, list):
        raise RecoveryPolicyError("policy audit event list is invalid")
    event_values = cast(list[object], events)
    if any(not isinstance(event, dict) for event in event_values):
        raise RecoveryPolicyError("policy audit event list is invalid")
    previous = "0" * 64
    lineage_owner: dict[str, str] = {}
    for raw in event_values:
        event = cast(dict[str, object], raw)
        digest = event.get("event_sha256")
        payload = {key: item for key, item in event.items() if key != "event_sha256"}
        expected = hashlib.sha256(previous.encode("ascii") + _canonical(payload)).hexdigest()
        if event.get("previous_event_sha256") != previous or digest != expected:
            raise RecoveryPolicyError("policy audit hash chain is invalid")
        event_policy = event.get("policy")
        if not isinstance(event_policy, dict):
            raise RecoveryPolicyError("policy audit event has no policy record")
        policy_record = cast(dict[str, object], event_policy)
        replacement = policy_record.get("replacement")
        if not isinstance(replacement, dict):
            raise RecoveryPolicyError("policy audit replacement lineage is invalid")
        replacement_record = cast(dict[str, object], replacement)
        lineage_key = str(replacement_record.get("lineage_key", ""))
        policy_id = str(event.get("policy_id", ""))
        if not lineage_key or not policy_id:
            raise RecoveryPolicyError("policy audit lineage identity is missing")
        owner = lineage_owner.setdefault(lineage_key, policy_id)
        if owner != policy_id:
            raise RecoveryPolicyError("predecessor has conflicting recovery policy lineage")
        previous = str(digest)
    return audit_value


def append_policy_event(path: Path, *, event_type: str, policy: Mapping[str, object]) -> str:
    """Append one hash-chained event atomically; exact replay is idempotent."""
    if event_type not in {"POLICY_INTENT", "POLICY_APPLIED"}:
        raise RecoveryPolicyError("unsupported policy audit event")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RecoveryPolicyError("policy audit is locked; retry after operator review") from exc
    try:
        os.close(descriptor)
        audit = _load_audit(path)
        events = cast(list[dict[str, object]], audit["events"])
        policy_id = str(policy.get("policy_id", ""))
        if not policy_id:
            raise RecoveryPolicyError("policy id is required")
        for event in events:
            if event.get("policy_id") == policy_id and event.get("event_type") == event_type:
                if event.get("policy") != dict(policy):
                    raise RecoveryPolicyError("policy event replay conflicts with durable audit")
                return str(event["event_sha256"])
        previous = str(events[-1]["event_sha256"]) if events else "0" * 64
        payload: dict[str, object] = {
            "event_type": event_type,
            "policy_id": policy_id,
            "policy": dict(policy),
            "previous_event_sha256": previous,
        }
        payload["event_sha256"] = hashlib.sha256(
            previous.encode("ascii") + _canonical(payload)
        ).hexdigest()
        events.append(payload)
        encoded = _canonical(audit) + b"\n"
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return str(payload["event_sha256"])
    except OSError as exc:
        raise RecoveryPolicyError("policy audit could not be persisted") from exc
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


def existing_policy_event(
    path: Path, *, event_type: str, policy_id: str
) -> dict[str, object] | None:
    """Return the validated exact policy payload for safe partial-apply replay."""
    audit = _load_audit(path)
    events = cast(list[dict[str, object]], audit["events"])
    matches = [
        event
        for event in events
        if event.get("event_type") == event_type and event.get("policy_id") == policy_id
    ]
    if len(matches) > 1:
        raise RecoveryPolicyError("policy audit contains duplicate policy events")
    if not matches:
        return None
    policy = matches[0].get("policy")
    if not isinstance(policy, dict):
        raise RecoveryPolicyError("policy audit event has an invalid policy payload")
    return cast(dict[str, object], policy)


def validate_policy_preconditions(
    policy: Mapping[str, object],
    *,
    attempt_id: str,
    reconciliation: str,
    evidence_recovery: str,
    reservation_state: str,
    reservation_credits: int,
    provider_evidence_present: bool,
) -> None:
    """Fail closed unless the user-authorized ambiguous legacy case still matches."""
    if policy.get("attempt_id") != attempt_id:
        raise RecoveryPolicyError("policy attempt id does not match exact target")
    if reconciliation != "AMBIGUOUS_AFTER_RECONCILIATION":
        raise RecoveryPolicyError("reconciliation is not in the approved ambiguous state")
    if evidence_recovery != "AMBIGUOUS_EVIDENCE_EXHAUSTED":
        raise RecoveryPolicyError("evidence recovery state is not the approved exhausted result")
    if reservation_state != "UNKNOWN_SUBMISSION_STATE" or reservation_credits != 50:
        raise RecoveryPolicyError("reservation does not match the unchanged 50-credit exposure")
    if provider_evidence_present:
        raise RecoveryPolicyError("provider evidence requires a separate reassessment")
