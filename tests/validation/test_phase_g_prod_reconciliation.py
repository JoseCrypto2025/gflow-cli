from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from gflow_cli.v2_runtime import phase_g_run
from gflow_cli.v2_runtime.phase_g import D2DailyCreditLedger
from gflow_cli.v2_runtime.phase_g_prod_reconciliation import (
    ProdAttemptIdentity,
    ProdReconciliationEvidence,
    ProdReconciliationResult,
    classify_prod_reconciliation,
    evidence_audit_record,
)
from gflow_cli.v2_runtime.production_execution import (
    CreditBudgetContext,
    ProductionExecutionContext,
)


def _identity() -> ProdAttemptIdentity:
    return ProdAttemptIdentity(
        production_job_id="PRD_000009",
        scene_job_id="SJB_000009",
        d2_run_id="D2_prod-run-0009",
        provider_account_id="PVA_0002",
        provider_project_id="prod-project-9",
        scene_id="SCN_000009",
        scene_prompt_id="SPV_000009",
        generation_profile_id="GPR_0002",
        prompt_sha256="a" * 64,
    )


def _attempt(identity: ProdAttemptIdentity | None = None, **updates: object) -> dict[str, object]:
    expected = identity or _identity()
    value: dict[str, object] = {
        "d2_run_id": expected.d2_run_id,
        "production_job_id": expected.production_job_id,
        "scene_job_id": expected.scene_job_id,
        "provider_account_id": expected.provider_account_id,
        "provider_project_id": expected.provider_project_id,
        "scene_id": expected.scene_id,
        "scene_prompt_id": expected.scene_prompt_id,
        "generation_profile_id": expected.generation_profile_id,
        "prompt_sha256": expected.prompt_sha256,
        "submission_state": "UNKNOWN_SUBMISSION_STATE",
        "generate_action_count": 1,
        "updated_at": "2026-10-25T12:00:00Z",
        "generate_action_at": "2026-10-25T12:00:00Z",
        "baseline_captured": True,
        "provider_execution_id": None,
        "provider_media_id": None,
        "provider_media_ids": [],
        "baseline_execution_ids": [],
        "baseline_media_ids": [],
        "baseline_confirmation_ids": [],
    }
    value.update(updates)
    return value


def _evidence(**updates: object) -> ProdReconciliationEvidence:
    values: dict[str, object] = {
        "identity": _identity(),
        "observed_at": "2026-10-25T12:01:00Z",
    }
    values.update(updates)
    return ProdReconciliationEvidence(**values)  # type: ignore[arg-type]


def test_ui_click_without_provider_receipt_remains_ambiguous() -> None:
    identity = _identity()
    result = classify_prod_reconciliation(
        identity,
        None,
        persisted_attempt=_attempt(identity, generate_action_count=1),
    )
    assert result is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION


def test_exact_provider_acceptance_proves_pending_submission() -> None:
    identity = _identity()
    evidence = _evidence(
        submission_state="GENERATION_PENDING",
        provider_execution_id="exec-9",
        provider_submit_request_count=1,
        provider_submit_dispatch_window_completed=True,
        provider_submit_project_id=identity.provider_project_id,
        provider_submit_response_observed=True,
        provider_submit_generation_record_observed=True,
    )
    assert (
        classify_prod_reconciliation(identity, evidence, persisted_attempt=_attempt(identity))
        is ProdReconciliationResult.SUBMISSION_PROVEN_PENDING
    )


def test_existing_exact_execution_proves_execution_found() -> None:
    identity = _identity()
    evidence = _evidence(
        submission_state="SUBMISSION_ACKNOWLEDGED",
        provider_execution_id="exec-9",
    )
    assert (
        classify_prod_reconciliation(identity, evidence, persisted_attempt=_attempt(identity))
        is ProdReconciliationResult.SUBMISSION_PROVEN_EXECUTION_FOUND
    )


def test_late_confirmation_is_detected_without_approval() -> None:
    identity = _identity()
    evidence = _evidence(confirmation_id="confirmation-9")
    assert (
        classify_prod_reconciliation(identity, evidence, persisted_attempt=_attempt(identity))
        is ProdReconciliationResult.LATE_CONFIRMATION_FOUND
    )


def test_existing_unique_media_is_reconciled_without_local_execution_id() -> None:
    identity = _identity()
    evidence = _evidence(
        submission_state="GENERATION_SUCCEEDED",
        provider_media_ids=("media-9",),
    )
    assert (
        classify_prod_reconciliation(identity, evidence, persisted_attempt=_attempt(identity))
        is ProdReconciliationResult.OUTPUT_ALREADY_EXISTS
    )


def test_only_correlated_provider_waf_refusal_proves_no_submission() -> None:
    identity = _identity()
    evidence = _evidence(
        submission_state="WAF_REJECTED",
        provider_submit_request_count=1,
        provider_submit_dispatch_window_completed=True,
        provider_submit_project_id=identity.provider_project_id,
        provider_submit_response_observed=True,
        provider_submit_response_status=403,
        provider_submit_generation_record_observed=False,
        provider_submit_refusal_code="PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        provider_submit_request_failed=False,
    )
    attempt = _attempt(
        identity,
        provider_submit_rpc_observed=True,
        provider_submit_rpc_id="YhhmEf",
        provider_submit_http_method="POST",
        provider_submit_observed_at="2026-10-25T12:00:30Z",
        provider_submit_request_count=1,
        provider_submit_dispatch_window_completed=True,
        provider_submit_project_id=identity.provider_project_id,
        provider_submit_response_observed=True,
        provider_submit_response_status=403,
        provider_submit_generation_record_observed=False,
        provider_submit_refusal_detected=True,
        provider_submit_refusal_code="PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        provider_submit_request_failed=False,
    )
    assert (
        classify_prod_reconciliation(identity, evidence, persisted_attempt=attempt)
        is ProdReconciliationResult.PROVEN_NOT_SUBMITTED
    )


@pytest.mark.parametrize(
    "mismatch",
    [
        {"provider_project_id": "other-project"},
        {"provider_account_id": "PVA_0001"},
        {"scene_id": "SCN_000001"},
        {"scene_prompt_id": "SPV_000001"},
        {"generation_profile_id": "GPR_0001"},
        {"prompt_sha256": "b" * 64},
        {"scene_job_id": "SJB_000001"},
        {"production_job_id": "PRD_000001"},
    ],
)
def test_wrong_attempt_identity_remains_ambiguous(mismatch: dict[str, str]) -> None:
    identity = _identity()
    evidence = _evidence(
        submission_state="GENERATION_PENDING",
        provider_execution_id="exec-9",
        identity=replace(identity, **mismatch),
    )
    assert (
        classify_prod_reconciliation(identity, evidence, persisted_attempt=_attempt(identity))
        is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    )


@pytest.mark.parametrize(
    "evidence",
    [
        _evidence(
            submission_state="GENERATION_PENDING",
            provider_execution_id="old-exec",
            baseline_execution_ids=("old-exec",),
        ),
        _evidence(
            submission_state="GENERATION_SUCCEEDED",
            provider_media_ids=("old-media",),
            baseline_media_ids=("old-media",),
        ),
        _evidence(
            confirmation_id="old-confirmation",
            baseline_confirmation_ids=("old-confirmation",),
        ),
        _evidence(
            submission_state="GENERATION_SUCCEEDED",
            provider_media_ids=("media-a", "media-b"),
        ),
        _evidence(
            submission_state="GENERATION_PENDING",
            provider_execution_id="exec-9",
            provider_submit_request_count=2,
        ),
    ],
)
def test_stale_or_duplicate_provider_candidates_remain_ambiguous(
    evidence: ProdReconciliationEvidence,
) -> None:
    identity = _identity()
    assert (
        classify_prod_reconciliation(identity, evidence, persisted_attempt=_attempt(identity))
        is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    )


def test_absent_receipt_or_unchanged_balance_never_proves_no_submission() -> None:
    identity = _identity()
    evidence = _evidence(
        submission_state="UNKNOWN_SUBMISSION_STATE",
        provider_submit_request_count=1,
        provider_submit_dispatch_window_completed=True,
        provider_submit_project_id=identity.provider_project_id,
        provider_submit_response_observed=True,
        provider_submit_response_status=200,
        provider_submit_generation_record_observed=False,
        provider_submit_request_failed=False,
    )
    assert (
        classify_prod_reconciliation(identity, evidence, persisted_attempt=_attempt(identity))
        is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    )


def test_stale_positive_refusal_evidence_remains_ambiguous() -> None:
    identity = _identity()
    evidence = _evidence(
        observed_at="2026-10-25T11:59:59Z",
        submission_state="WAF_REJECTED",
        provider_submit_request_count=1,
        provider_submit_dispatch_window_completed=True,
        provider_submit_project_id=identity.provider_project_id,
        provider_submit_response_observed=True,
        provider_submit_response_status=403,
        provider_submit_generation_record_observed=False,
        provider_submit_refusal_code="PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        provider_submit_request_failed=False,
    )
    assert (
        classify_prod_reconciliation(identity, evidence, persisted_attempt=_attempt(identity))
        is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    )


def test_refusal_conflicting_with_confirmation_remains_ambiguous() -> None:
    identity = _identity()
    evidence = _evidence(
        submission_state="WAF_REJECTED",
        confirmation_id="confirmation-9",
        provider_submit_request_count=1,
        provider_submit_dispatch_window_completed=True,
        provider_submit_project_id=identity.provider_project_id,
        provider_submit_response_observed=True,
        provider_submit_response_status=403,
        provider_submit_generation_record_observed=False,
        provider_submit_refusal_code="PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        provider_submit_request_failed=False,
    )
    assert (
        classify_prod_reconciliation(identity, evidence, persisted_attempt=_attempt(identity))
        is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    )


def test_missing_job_lineage_in_persisted_attempt_fails_closed() -> None:
    identity = _identity()
    attempt = _attempt(identity)
    attempt.pop("scene_job_id")
    evidence = _evidence(provider_execution_id="exec-9", submission_state="GENERATION_PENDING")
    assert (
        classify_prod_reconciliation(identity, evidence, persisted_attempt=attempt)
        is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    )


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        (None, ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION),
        (
            _evidence(confirmation_id="confirmation-late"),
            ProdReconciliationResult.LATE_CONFIRMATION_FOUND,
        ),
    ],
)
def test_prod_entrypoint_audits_without_releasing_or_generating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence: ProdReconciliationEvidence | None,
    expected: ProdReconciliationResult,
) -> None:
    identity = _identity()
    attempt = _attempt(identity, execution_environment="PROD")
    audits: list[dict[str, object]] = []

    class Ledger:
        def __init__(self, _root) -> None:
            pass

        def attempts(self):
            return (attempt,)

        def record_prod_reconciliation(self, _run_id, audit):
            audits.append(dict(audit))
            return dict(audit)

    class Guard:
        def release_proven_not_submitted(self, *_args, **_kwargs):
            raise AssertionError("ambiguous reconciliation must not release credits")

    monkeypatch.setattr(phase_g_run, "D2DailyCreditLedger", Ledger)
    context = ProductionExecutionContext(
        "PROD",
        "PRV_0001",
        identity.provider_account_id,
        identity.provider_project_id,
        "GFLOW_PROD_AUTH",
        identity.scene_id,
        identity.generation_profile_id,
        identity.scene_prompt_id,
        identity.d2_run_id,
        CreditBudgetContext(),
        production_job_id=identity.production_job_id,
        scene_job_id=identity.scene_job_id,
    )

    result = phase_g_run.reconcile_prod_same_attempt(
        tmp_path,
        context,
        identity,
        credit_guard=Guard(),  # type: ignore[arg-type]
        evidence=evidence,
    )

    assert result is expected
    assert len(audits) == 1
    assert audits[0]["contract_version"] == "D2_PROD_SAME_ATTEMPT_RECOVERY_V1"


def test_d2_prod_reconciliation_audit_is_idempotent(tmp_path: Path) -> None:
    identity = _identity()
    attempt = _attempt(identity, execution_environment="PROD")
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger._mutate(  # noqa: SLF001 - insert a focused persisted attempt fixture
        lambda payload: payload["attempts"].append(attempt) or {}
    )
    evidence = _evidence()
    audit = {
        **evidence_audit_record(ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION, evidence),
        "contract_version": "D2_PROD_SAME_ATTEMPT_RECOVERY_V1",
    }

    first = ledger.record_prod_reconciliation(identity.d2_run_id, audit)
    second = ledger.record_prod_reconciliation(identity.d2_run_id, audit)

    assert first == second
    saved = ledger.attempts()[0]
    assert len(saved["prod_reconciliation_history"]) == 1
