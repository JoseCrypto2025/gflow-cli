from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.phase_g import D2DailyCreditLedger, PhaseGError
from scripts.operations.reconcile_legacy_prod_attempt import _read_ledger

_ATTEMPT_ID = "D2_13c62112d9881710019049d95cb6da57"
_PRODUCTION_JOB_ID = "PRD_000001"
_SCENE_JOB_ID = "SJB_000001"
_CORRELATIONS = (
    "SCENE_ID_MATCH",
    "PROMPT_ID_MATCH",
    "PROMPT_HASH_MATCH",
    "GENERATION_PROFILE_MATCH",
    "PROVIDER_ACCOUNT_MATCH",
    "FLOW_PROJECT_MATCH",
    "ENVIRONMENT_MATCH",
    "RESERVATION_LINK_MATCH",
    "TIMESTAMP_COMPATIBLE",
    "POLICY_AUDIT_MATCH",
    "RECONCILIATION_AUDIT_MATCH",
)


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _attempt(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "d2_run_id": _ATTEMPT_ID,
        "scene_id": "SCN_000001",
        "scene_prompt_id": "SPV_000007",
        "generation_profile_id": "GPR_0002",
        "provider_id": "PRV_0001",
        "provider_account_id": "PVA_0002",
        "provider_project_id": "aafe9282-071f-4333-baa4-92b9f76a9c27",
        "prompt_sha256": "e7c3c55a742f6d73427e13333989e335174e70175ff8702cefe6d6ea2d6befa4",
        "started_at": "2026-10-25T11:04:53Z",
        "updated_at": "2026-10-25T11:05:09Z",
        "generate_action_count": 1,
        "approval_action_count": 0,
        "submission_state": "UNKNOWN_SUBMISSION_STATE",
        "accounted_credit_cost": "50",
        "provider_submit_request_count": 0,
        "provider_execution_id": None,
        "provider_media_id": None,
        "provider_media_ids": [],
        "confirmation_id": None,
    }
    row.update(updates)
    return row


def _proof(attempt: dict[str, object]) -> dict[str, object]:
    return {
        "contract_version": "D2_PROD_LEGACY_JOB_LINKAGE_REPAIR_V1",
        "linkage_confidence": "PROVEN",
        "attempt_id": _ATTEMPT_ID,
        "production_job_id": _PRODUCTION_JOB_ID,
        "scene_job_id": _SCENE_JOB_ID,
        "unique_match_counts": {
            "d2_attempt": 1,
            "production_job": 1,
            "scene_job": 1,
            "credit_reservation": 1,
        },
        "correlations": {name: True for name in _CORRELATIONS},
        "source_snapshot_sha256": {
            name: hashlib.sha256(name.encode()).hexdigest()
            for name in (
                "D2_LEDGER",
                "CREDIT_LEDGER",
                "PRODUCTION_JOBS",
                "SCENE_JOBS",
                "SCENES",
                "SCENE_PROMPT_VERSION",
                "GENERATION_PROFILE_MASTER",
                "PROVIDER_ACCOUNT_MASTER",
            )
        },
        "source_snapshot_observed_at": {
            name: "2026-10-29T12:29:00Z"
            for name in (
                "D2_LEDGER",
                "CREDIT_LEDGER",
                "PRODUCTION_JOBS",
                "SCENE_JOBS",
                "SCENES",
                "SCENE_PROMPT_VERSION",
                "GENERATION_PROFILE_MASTER",
                "PROVIDER_ACCOUNT_MASTER",
            )
        },
        "relevant_source_sha256": {
            name: hashlib.sha256(name.encode()).hexdigest()
            for name in (
                "D2_ATTEMPT",
                "CREDIT_RESERVATION",
                "PRODUCTION_JOB",
                "SCENE_JOB",
                "SCENE",
                "PROMPT_VERSION",
                "GENERATION_PROFILE",
                "PROVIDER_ACCOUNT",
            )
        },
        "policy_audit_sha256": "a" * 64,
        "reconciliation_audit_sha256": "b" * 64,
        "target_attempt_sha256": _digest(
            {
                key: value
                for key, value in attempt.items()
                if key not in {"production_job_id", "scene_job_id"}
            }
        ),
        "verified_at": "2026-10-29T12:30:00Z",
        "software_commit": "c" * 40,
    }


def _write_ledger(
    root: Path, attempts: list[dict[str, object]], *, schema_version: int = 3
) -> D2DailyCreditLedger:
    ledger = D2DailyCreditLedger(root)
    ledger.root.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": schema_version,
        "daily_budget": 50,
        "attempts": attempts,
    }
    if schema_version >= 2:
        payload["test_sessions"] = [{"session_id": "keep-session"}]
    if schema_version >= 3:
        payload["legacy_replacement_authorizations"] = [{"authorization_id": "keep-auth"}]
    if schema_version >= 4:
        payload["legacy_job_linkage_repairs"] = []
    ledger.path.write_text(json.dumps(payload), encoding="utf-8")
    return ledger


def _repair(ledger: D2DailyCreditLedger, proof: dict[str, object]) -> dict[str, object]:
    source_hashes = proof["source_snapshot_sha256"]
    assert isinstance(source_hashes, dict)
    source_hashes["D2_LEDGER"] = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    return ledger.repair_legacy_job_linkage(proof)


def test_proven_linkage_backfills_only_two_fields_and_appends_audit(tmp_path: Path) -> None:
    target = _attempt()
    unrelated = _attempt(d2_run_id="D2_modern-12345678", production_job_id="PRD_999999")
    original_target = deepcopy(target)
    original_unrelated = deepcopy(unrelated)
    ledger = _write_ledger(tmp_path, [target, unrelated])

    result = _repair(ledger, _proof(target))
    saved = ledger._read()
    attempts = saved["attempts"]
    assert isinstance(attempts, list)
    saved_target, saved_unrelated = attempts

    assert result["attempt_id"] == _ATTEMPT_ID
    assert result["production_job_id"] == _PRODUCTION_JOB_ID
    assert result["scene_job_id"] == _SCENE_JOB_ID
    assert result["provenance_class"] == "DERIVED_CANONICAL_JOIN"
    assert saved_target == {
        **original_target,
        "production_job_id": _PRODUCTION_JOB_ID,
        "scene_job_id": _SCENE_JOB_ID,
    }
    assert saved_unrelated == original_unrelated
    assert saved["schema_version"] == 4
    assert saved["legacy_replacement_authorizations"] == [{"authorization_id": "keep-auth"}]
    assert saved["test_sessions"] == [{"session_id": "keep-session"}]
    audits = saved["legacy_job_linkage_repairs"]
    assert isinstance(audits, list) and len(audits) == 1
    assert audits[0]["before_attempt_sha256"] == _digest(original_target)
    assert audits[0]["after_attempt_sha256"] == _digest(saved_target)


def test_exact_repair_replay_returns_same_audit_without_another_event(tmp_path: Path) -> None:
    target = _attempt()
    ledger = _write_ledger(tmp_path, [target])
    proof = _proof(target)

    first = _repair(ledger, proof)
    after_first = ledger.path.read_bytes()
    repaired_target = {
        **target,
        "production_job_id": _PRODUCTION_JOB_ID,
        "scene_job_id": _SCENE_JOB_ID,
    }
    second = _repair(ledger, _proof(repaired_target))

    assert first == second
    assert ledger.path.read_bytes() == after_first
    saved = ledger._read()
    assert len(saved["legacy_job_linkage_repairs"]) == 1


@pytest.mark.parametrize(
    "field",
    ["production_job_id", "scene_job_id"],
)
def test_conflicting_existing_job_linkage_fails_without_write(tmp_path: Path, field: str) -> None:
    target = _attempt(**{field: "PRD_999999" if field == "production_job_id" else "SJB_999999"})
    ledger = _write_ledger(tmp_path, [target])
    proof = _proof(target)
    before = ledger.path.read_bytes()

    with pytest.raises(PhaseGError, match="conflict"):
        _repair(ledger, proof)

    assert ledger.path.read_bytes() == before


def test_ambiguous_candidate_count_fails_without_write(tmp_path: Path) -> None:
    target = _attempt()
    ledger = _write_ledger(tmp_path, [target])
    proof = _proof(target)
    proof["unique_match_counts"] = {
        "d2_attempt": 1,
        "production_job": 2,
        "scene_job": 1,
        "credit_reservation": 1,
    }
    before = ledger.path.read_bytes()

    with pytest.raises(PhaseGError, match="ambiguous"):
        _repair(ledger, proof)

    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_id", "D2_00000000000000000000000000000000"),
        ("production_job_id", "PRD_000002"),
        ("scene_job_id", "SJB_000002"),
    ],
)
def test_repair_contract_rejects_targets_outside_the_named_parent(
    tmp_path: Path, field: str, value: str
) -> None:
    target = _attempt()
    ledger = _write_ledger(tmp_path, [target])
    proof = _proof(target)
    proof[field] = value
    before = ledger.path.read_bytes()

    with pytest.raises(PhaseGError, match="proof identity"):
        _repair(ledger, proof)

    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("correlation", _CORRELATIONS)
def test_mismatched_authoritative_correlation_fails_without_write(
    tmp_path: Path, correlation: str
) -> None:
    target = _attempt()
    ledger = _write_ledger(tmp_path, [target])
    proof = _proof(target)
    correlations = dict(proof["correlations"])
    correlations[correlation] = False
    proof["correlations"] = correlations
    before = ledger.path.read_bytes()

    with pytest.raises(PhaseGError, match="proof"):
        _repair(ledger, proof)

    assert ledger.path.read_bytes() == before


def test_stale_attempt_digest_fails_without_mutation(tmp_path: Path) -> None:
    target = _attempt()
    ledger = _write_ledger(tmp_path, [target])
    proof = _proof(target)
    current = ledger._read()
    rows = current["attempts"]
    assert isinstance(rows, list)
    rows[0]["updated_at"] = "2026-10-29T12:31:00Z"
    ledger._write(current)
    before = ledger.path.read_bytes()

    with pytest.raises(PhaseGError, match="stale"):
        _repair(ledger, proof)

    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize("schema_version", [1, 2, 3])
def test_legacy_schema_migrates_to_v4_without_changing_existing_data(
    tmp_path: Path, schema_version: int
) -> None:
    target = _attempt()
    ledger = _write_ledger(tmp_path, [target], schema_version=schema_version)
    migrated = ledger._read()

    assert migrated["schema_version"] == 4
    assert migrated["attempts"] == [target]
    assert migrated["legacy_job_linkage_repairs"] == []
    if schema_version >= 2:
        assert migrated["test_sessions"] == [{"session_id": "keep-session"}]
    if schema_version >= 3:
        assert migrated["legacy_replacement_authorizations"] == [{"authorization_id": "keep-auth"}]


def test_already_correct_values_are_passed_without_changing_attempt(tmp_path: Path) -> None:
    target = _attempt(production_job_id=_PRODUCTION_JOB_ID, scene_job_id=_SCENE_JOB_ID)
    original = deepcopy(target)
    ledger = _write_ledger(tmp_path, [target])

    result = _repair(ledger, _proof(target))

    assert result["production_job_id"] == _PRODUCTION_JOB_ID
    saved = ledger._read()
    assert saved["attempts"] == [original]
    assert len(saved["legacy_job_linkage_repairs"]) == 1


def test_repair_preserves_reservation_billing_and_provider_evidence(tmp_path: Path) -> None:
    target = _attempt(
        provider_submit_request_count=0,
        provider_execution_id=None,
        provider_media_id=None,
        provider_media_ids=[],
        confirmation_id=None,
    )
    ledger = _write_ledger(tmp_path, [target])
    credit_path = tmp_path / "production-credit" / "shared-ledger.json"
    credit_path.parent.mkdir(parents=True)
    credit_path.write_text(
        '{"schema_version":1,"attempts":[{"attempt_id":"D2_13c62112d9881710019049d95cb6da57",'
        '"reserved_credits":"50","state":"UNKNOWN_SUBMISSION_STATE"}]}\n',
        encoding="utf-8",
    )
    credit_before = credit_path.read_bytes()
    policy_audit = tmp_path / "policy-audit.json"
    reconciliation_audit = tmp_path / "reconciliation-audit.json"
    policy_audit.write_text("policy-audit-unchanged\n", encoding="utf-8")
    reconciliation_audit.write_text("reconciliation-audit-unchanged\n", encoding="utf-8")
    policy_before = policy_audit.read_bytes()
    reconciliation_before = reconciliation_audit.read_bytes()

    _repair(ledger, _proof(target))

    saved = ledger._read()
    repaired = saved["attempts"][0]
    for field, value in target.items():
        assert repaired[field] == value
    assert repaired["submission_state"] == "UNKNOWN_SUBMISSION_STATE"
    assert repaired["accounted_credit_cost"] == "50"
    assert repaired["generate_action_count"] == 1
    assert credit_path.read_bytes() == credit_before
    assert policy_audit.read_bytes() == policy_before
    assert reconciliation_audit.read_bytes() == reconciliation_before


def test_legacy_reconciliation_reader_accepts_schema_v4_without_changing_provenance(
    tmp_path: Path,
) -> None:
    target = _attempt(
        production_job_id=_PRODUCTION_JOB_ID,
        scene_job_id=_SCENE_JOB_ID,
    )
    ledger = _write_ledger(tmp_path, [target], schema_version=4)
    migrated = ledger._read()
    ledger._write(migrated)

    rows, raw_sha256, observed_at = _read_ledger(ledger.path, kind="D2")

    assert rows == (target,)
    assert len(raw_sha256) == 64
    assert observed_at.endswith("Z")


def test_tampered_repair_audit_breaks_the_v4_hash_chain(tmp_path: Path) -> None:
    target = _attempt()
    ledger = _write_ledger(tmp_path, [target])
    _repair(ledger, _proof(target))
    saved = ledger._read()
    audits = saved["legacy_job_linkage_repairs"]
    assert isinstance(audits, list)
    audits[0]["production_job_id"] = "PRD_999999"
    ledger._write(saved)

    with pytest.raises(PhaseGError, match="audit chain"):
        ledger._read()
