from __future__ import annotations

import json

import pytest

from gflow_cli.v2_runtime.ambiguous_legacy_recovery_policy import (
    AUDIT_NAME,
    RecoveryPolicyError,
    append_policy_event,
    build_policy_record,
    validate_policy_preconditions,
)
from scripts.operations.apply_ambiguous_legacy_recovery_policy import (
    _provider_evidence_present,
)


def _policy() -> dict[str, object]:
    return build_policy_record(
        attempt_id="D2_13c62112d9881710019049d95cb6da57",
        production_job_id="PRD_000001",
        scene_job_id="SJB_000001",
        reservation_id="D2_13c62112d9881710019049d95cb6da57",
        observed_at="2026-10-25T00:00:00Z",
    )


def test_policy_records_unknown_exposure_without_authorizing_replacement() -> None:
    policy = _policy()

    assert policy["reservation_credits"] == 50
    assert policy["reservation_state"] == "UNKNOWN_SUBMISSION_STATE"
    assert policy["billing_state"] == "UNKNOWN"
    assert policy["production_job_target"] == policy["scene_job_target"] == "QUARANTINED"
    assert policy["generate_authorized"] is False
    replacement = policy["replacement"]
    assert isinstance(replacement, dict)
    assert replacement["eligible_after_quarantine"] is True
    assert replacement["authorized"] is False
    assert replacement["attempt_creation_authorized"] is False


def test_audit_event_is_hash_chained_and_exact_replay_is_idempotent(tmp_path) -> None:
    path = tmp_path / AUDIT_NAME
    policy = _policy()

    first = append_policy_event(path, event_type="POLICY_INTENT", policy=policy)
    replay = append_policy_event(path, event_type="POLICY_INTENT", policy=policy)
    append_policy_event(path, event_type="POLICY_APPLIED", policy=policy)
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert first == replay
    assert len(payload["events"]) == 2
    assert payload["events"][1]["previous_event_sha256"] == first


def test_corrupt_audit_chain_fails_closed(tmp_path) -> None:
    path = tmp_path / AUDIT_NAME
    policy = _policy()
    append_policy_event(path, event_type="POLICY_INTENT", policy=policy)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][0]["policy"]["reservation_credits"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(RecoveryPolicyError, match="hash chain"):
        append_policy_event(path, event_type="POLICY_APPLIED", policy=policy)


def test_preconditions_reject_changed_reservation_and_provider_evidence() -> None:
    policy = _policy()
    args = {
        "policy": policy,
        "attempt_id": "D2_13c62112d9881710019049d95cb6da57",
        "reconciliation": "AMBIGUOUS_AFTER_RECONCILIATION",
        "evidence_recovery": "AMBIGUOUS_EVIDENCE_EXHAUSTED",
        "reservation_state": "UNKNOWN_SUBMISSION_STATE",
        "reservation_credits": 50,
        "provider_evidence_present": False,
    }
    validate_policy_preconditions(**args)

    with pytest.raises(RecoveryPolicyError, match="reservation"):
        validate_policy_preconditions(**{**args, "reservation_credits": 49})
    with pytest.raises(RecoveryPolicyError, match="provider evidence"):
        validate_policy_preconditions(**{**args, "provider_evidence_present": True})


def test_operator_evidence_guard_uses_only_existing_d2_row_fields() -> None:
    assert not _provider_evidence_present(
        {
            "provider_submit_request_count": 0,
            "provider_submit_rpc_observed": False,
            "provider_execution_id": None,
            "provider_media_ids": [],
        }
    )
    assert _provider_evidence_present({"provider_execution_id": "execution-1"})
