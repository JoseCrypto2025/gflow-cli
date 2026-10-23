from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_phase_g import request_for

from gflow_cli.v2_runtime.agent_recovery import AgentSettingsRecoveryJournal
from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    D2RetryAuthorization,
    FlowSubmissionBaseline,
    PhaseGError,
    PhaseGExecutionRequest,
)
from gflow_cli.v2_runtime.phase_g_artifacts import sha256_file
from gflow_cli.v2_runtime.phase_g_forensic_misattribution_closure import (
    forensic_misattribution_closure_preview,
    forensic_misattribution_evidence_digest,
    load_misattribution_evidence,
    main,
    validate_forensic_misattribution_evidence,
)

_RUN_ID = "D2_4f46a55259b54aa2"
_PREDECESSOR = "D2_2aebc5af59bd423b"
_EXECUTION_ID = "9dc0380b-0a38-44f7-9c6c-8fe5262fdc2a"
_MEDIA_ID = "d6e85926-120f-40ab-bfc0-6a788c521339"
_PROJECT_ID = "34e60953-e534-46e9-ac65-993893e61065"
_COMMIT = "a" * 40


def _attempt_fixture(tmp_path: Path) -> tuple[D2DailyCreditLedger, Path]:
    root = tmp_path / "runtime"
    request = request_for(tmp_path)
    ledger = D2DailyCreditLedger(root)
    predecessor_request = PhaseGExecutionRequest.from_presubmit(request, _PREDECESSOR)
    ledger.create_attempt(predecessor_request)
    for run_id in (_PREDECESSOR,):
        ledger.capture_baseline(
            run_id,
            FlowSubmissionBaseline(_PROJECT_ID, frozenset(), frozenset(), 0, frozenset(), "0:", ()),
        )
        ledger.mark_generate_action(run_id)
        ledger.record_state(run_id, submission_state="SUBMISSION_ACKNOWLEDGED")
        ledger.record_state(
            run_id,
            submission_state="UNEXPECTED_MULTI_OUTPUT",
            terminal_state="UNEXPECTED_MULTI_OUTPUT",
            provider_execution_id=_EXECUTION_ID,
            provider_media_ids=[_EXECUTION_ID, _MEDIA_ID],
            accounted_credit_cost=10,
        )
    retry_request = PhaseGExecutionRequest.from_presubmit(request, _RUN_ID)
    ledger.create_authorized_retry_attempt(
        retry_request,
        D2RetryAuthorization(_PREDECESSOR, "UNEXPECTED_MULTI_OUTPUT"),
    )
    ledger.capture_baseline(
        _RUN_ID,
        FlowSubmissionBaseline(_PROJECT_ID, frozenset(), frozenset(), 0, frozenset(), "0:", ()),
    )
    ledger.mark_generate_action(_RUN_ID)
    ledger.record_state(
        _RUN_ID,
        submission_state="GENERATION_SUCCEEDED",
        terminal_state=None,
        provider_execution_id=_EXECUTION_ID,
        provider_media_id=_MEDIA_ID,
        provider_media_ids=[_MEDIA_ID],
        accounted_credit_cost=40,
    )
    local_path = request.temp_root / _RUN_ID / "clip.mp4"
    local_path.parent.mkdir(parents=True)
    local_path.write_bytes(b"historically attributed local artifact")
    ledger.record_local_artifact_validated(
        _RUN_ID,
        path=local_path,
        size_bytes=local_path.stat().st_size,
        sha256=sha256_file(local_path),
    )
    ledger._mutate(lambda payload: _set_accounting_dates(payload, _PREDECESSOR, _RUN_ID))
    journal = AgentSettingsRecoveryJournal(root, "PVA_0001", _PROJECT_ID)
    journal.begin(
        correlation_id="test-journal",
        original_settings={},
        intended_settings={},
    )
    journal.set_state("CLEAN")
    return ledger, local_path


def _set_accounting_dates(payload: dict[str, object], *run_ids: str) -> dict[str, object]:
    attempts = payload["attempts"]
    assert isinstance(attempts, list)
    for row in attempts:
        if row["d2_run_id"] in run_ids:
            row["accounting_date"] = "2026-10-22"
    return {"updated": True}


def _evidence(ledger: D2DailyCreditLedger, *, ledger_sha: str | None = None) -> dict[str, object]:
    retry = next(row for row in ledger.attempts() if row["d2_run_id"] == _RUN_ID)
    return {
        "SCHEMA_VERSION": "D2_FORENSIC_MISATTRIBUTION_CLOSURE_V1",
        "D2_RUN_ID": _RUN_ID,
        "CANONICAL_LEDGER_SHA256": ledger_sha
        or hashlib.sha256(ledger.path.read_bytes()).hexdigest(),
        "IMPLEMENTATION_COMMIT": _COMMIT,
        "PROVIDER_ACCOUNT_ID": "PVA_0001",
        "PROVIDER_PROJECT_ID": _PROJECT_ID,
        "CURRENT_SUBMISSION_STATE": "GENERATION_SUCCEEDED",
        "CURRENT_TERMINAL_STATE": "NONE",
        "GENERATE_ACTION_COUNT": 1,
        "APPROVAL_ACTION_COUNT": 0,
        "ACCOUNTED_CREDIT_COST_EXPECTED": "40",
        "ACCOUNTING_DATE_EXPECTED": "2026-10-22",
        "RETRY_AUTHORIZATION_PRESENT": True,
        "RETRY_PREDECESSOR": _PREDECESSOR,
        "RECORDED_PROVIDER_EXECUTION_ID": retry["provider_execution_id"],
        "RECORDED_PROVIDER_MEDIA_ID": retry["provider_media_id"],
        "RECORDED_PROVIDER_MEDIA_IDS": retry["provider_media_ids"],
        "RECORDED_PROVIDER_IDS_HISTORICAL": True,
        "HISTORICAL_EXECUTION_MATCH": True,
        "HISTORICAL_MEDIA_MATCH": True,
        "LIVE_STRUCTURED_ENUMERATION_COMPLETE": True,
        "CANDIDATE_NEW_EXECUTION_COUNT": 0,
        "CANDIDATE_NEW_MEDIA_COUNT": 0,
        "POST_BASELINE_NEW_MEDIA_ASSOCIATION": False,
        "ATTRIBUTION_CLASSIFICATION": "NO_NEW_RETRY_OUTPUT_PROVEN",
        "REMOTE_UPLOAD_OCCURRED": False,
        "REMOTE_VERIFIED": False,
        "PROVIDER_OUTCOME": "UNKNOWN",
        "CREDIT_OUTCOME": "UNKNOWN",
        "PROVIDER_BILLING_OUTCOME": "UNKNOWN",
        "RECOVERY_JOURNAL_CLEAN": True,
        "SAFE_TO_CLOSE": True,
        "FORENSIC_COMPLETED_AT": datetime.now(UTC).isoformat(),
        "FORENSIC_REASON": "Recorded provider identities match earlier same-project evidence",
    }


def _preview(
    ledger: D2DailyCreditLedger, evidence: dict[str, object], *, expected_sha: str | None = None
) -> dict[str, object]:
    sha = expected_sha or str(evidence["CANONICAL_LEDGER_SHA256"])
    digest = forensic_misattribution_evidence_digest(evidence)
    return forensic_misattribution_closure_preview(
        ledger.root.parent,
        d2_run_id=_RUN_ID,
        expected_ledger_sha256=sha,
        expected_state="GENERATION_SUCCEEDED",
        expected_evidence_sha256=digest,
        implementation_commit=_COMMIT,
        evidence=evidence,
        evidence_sha256=digest,
    )


def test_ordinary_generation_success_cannot_use_record_state_for_forensic_close(
    tmp_path: Path,
) -> None:
    ledger, _ = _attempt_fixture(tmp_path)
    with pytest.raises(PhaseGError) as rejected:
        ledger.record_state(
            _RUN_ID,
            submission_state="FORENSICALLY_CLOSED_UNKNOWN",
            terminal_state="FORENSICALLY_CLOSED_UNKNOWN",
        )
    assert rejected.value.code == "PHASE_G_FORENSIC_OPERATOR_REQUIRED"


def test_misattribution_closure_preview_and_apply_preserve_history(tmp_path: Path) -> None:
    ledger, local_path = _attempt_fixture(tmp_path)
    before = {row["d2_run_id"]: dict(row) for row in ledger.attempts()}
    evidence = _evidence(ledger)
    preview = _preview(ledger, evidence)
    assert preview["attempt"]["submission_state"] == "GENERATION_SUCCEEDED"
    assert ledger.attempts()[1]["submission_state"] == "GENERATION_SUCCEEDED"
    expected_sha = str(evidence["CANONICAL_LEDGER_SHA256"])
    digest = forensic_misattribution_evidence_digest(evidence)
    closed = ledger.close_misattributed_generation_forensic(
        _RUN_ID,
        expected_ledger_sha256=expected_sha,
        evidence_sha256=digest,
        implementation_commit=_COMMIT,
        evidence=evidence,
    )
    assert closed["submission_state"] == "FORENSICALLY_CLOSED_UNKNOWN"
    assert closed["terminal_state"] == "FORENSICALLY_CLOSED_UNKNOWN"
    assert closed["accounted_credit_cost"] == before[_RUN_ID]["accounted_credit_cost"] == "40"
    assert closed["accounting_date"] == before[_RUN_ID]["accounting_date"] == "2026-10-22"
    assert closed["generate_action_count"] == 1
    assert closed["approval_action_count"] == 0
    assert closed["provider_execution_id"] == before[_RUN_ID]["provider_execution_id"]
    assert closed["provider_media_id"] == before[_RUN_ID]["provider_media_id"]
    assert closed["provider_media_ids"] == before[_RUN_ID]["provider_media_ids"]
    assert closed["local_artifact_path"] == str(local_path.resolve())
    assert closed["local_artifact_sha256"] == sha256_file(local_path)
    assert closed["attribution_invalidated"] is True
    assert closed["artifact_attribution_valid"] is False
    assert closed["provider_billing_outcome"] == "UNKNOWN"
    assert closed["provider_outcome"] == "UNKNOWN"
    assert closed["credit_outcome"] == "UNKNOWN"
    assert ledger.attempts()[0] == before[_PREDECESSOR]


def test_operator_rejects_wrong_ledger_sha(tmp_path: Path) -> None:
    ledger, _ = _attempt_fixture(tmp_path)
    evidence = _evidence(ledger, ledger_sha="0" * 64)
    with pytest.raises(PhaseGError):
        _preview(ledger, evidence, expected_sha="0" * 64)


def test_operator_rejects_wrong_evidence_sha(tmp_path: Path) -> None:
    ledger, _ = _attempt_fixture(tmp_path)
    evidence = _evidence(ledger)
    digest = forensic_misattribution_evidence_digest(evidence)
    with pytest.raises(PhaseGError) as rejected:
        forensic_misattribution_closure_preview(
            ledger.root.parent,
            d2_run_id=_RUN_ID,
            expected_ledger_sha256=str(evidence["CANONICAL_LEDGER_SHA256"]),
            expected_state="GENERATION_SUCCEEDED",
            expected_evidence_sha256="0" * 64,
            implementation_commit=_COMMIT,
            evidence=evidence,
            evidence_sha256=digest,
        )
    assert rejected.value.code == "PHASE_G_FORENSIC_EVIDENCE_HASH_MISMATCH"


def test_operator_rejects_state_other_than_generation_succeeded(tmp_path: Path) -> None:
    ledger, _ = _attempt_fixture(tmp_path)
    evidence = _evidence(ledger)
    with pytest.raises(PhaseGError):
        forensic_misattribution_closure_preview(
            ledger.root.parent,
            d2_run_id=_RUN_ID,
            expected_ledger_sha256=str(evidence["CANONICAL_LEDGER_SHA256"]),
            expected_state="UNKNOWN_SUBMISSION_STATE",
            expected_evidence_sha256=forensic_misattribution_evidence_digest(evidence),
            implementation_commit=_COMMIT,
            evidence=evidence,
            evidence_sha256=forensic_misattribution_evidence_digest(evidence),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.update(CANDIDATE_NEW_MEDIA_COUNT=1),
        lambda item: item.update(REMOTE_VERIFIED=True),
        lambda item: item.update(GENERATE_ACTION_COUNT=2),
        lambda item: item.update(CURRENT_TERMINAL_STATE="GENERATION_SUCCEEDED"),
    ],
)
def test_evidence_rejects_new_output_remote_verification_or_wrong_attempt_shape(
    tmp_path: Path, mutation
) -> None:
    ledger, _ = _attempt_fixture(tmp_path)
    evidence = _evidence(ledger)
    mutation(evidence)
    with pytest.raises(PhaseGError):
        validate_forensic_misattribution_evidence(evidence)


def test_operator_rejects_d2_succeeded(tmp_path: Path) -> None:
    ledger, _ = _attempt_fixture(tmp_path)
    ledger.record_state(
        _RUN_ID,
        submission_state="D2_SUCCEEDED",
        terminal_state="D2_SUCCEEDED",
    )
    evidence = _evidence(ledger)
    with pytest.raises(PhaseGError):
        _preview(ledger, evidence)


def test_operator_rejects_provider_ids_without_historical_match(tmp_path: Path) -> None:
    ledger, _ = _attempt_fixture(tmp_path)
    ledger.record_state(
        _RUN_ID, submission_state="GENERATION_SUCCEEDED", provider_media_id="new-id"
    )
    evidence = _evidence(ledger)
    with pytest.raises(PhaseGError):
        _preview(ledger, evidence)


def test_operator_rejects_duplicate_run_id_and_unclean_journal(tmp_path: Path) -> None:
    ledger, _ = _attempt_fixture(tmp_path)
    ledger._mutate(lambda payload: _append_duplicate_attempt(payload, _RUN_ID))
    evidence = _evidence(ledger)
    with pytest.raises(PhaseGError) as duplicate:
        _preview(ledger, evidence)
    assert duplicate.value.code == "PHASE_G_ATTEMPT_NOT_UNIQUE"

    ledger, _ = _attempt_fixture(tmp_path / "unclean")
    AgentSettingsRecoveryJournal(ledger.root.parent, "PVA_0001", _PROJECT_ID).set_state("DIRTY")
    evidence = _evidence(ledger)
    with pytest.raises(PhaseGError) as dirty:
        _preview(ledger, evidence)
    assert dirty.value.code == "PHASE_G_RECOVERY_JOURNAL_UNCLEAN"


def _append_duplicate_attempt(payload: dict[str, object], run_id: str) -> dict[str, object]:
    attempts = payload["attempts"]
    assert isinstance(attempts, list)
    original = next(row for row in attempts if row["d2_run_id"] == run_id)
    attempts.append(dict(original))
    return {"duplicated": run_id}


def test_operator_cli_requires_canonical_evidence_and_is_dry_run_by_default(
    tmp_path: Path,
) -> None:
    ledger, _ = _attempt_fixture(tmp_path)
    before = ledger.path.read_bytes()
    evidence = _evidence(ledger)
    path = tmp_path / "evidence.json"
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    path.write_bytes(canonical)
    loaded, digest = load_misattribution_evidence(path)
    assert digest == forensic_misattribution_evidence_digest(loaded)
    assert (
        main(
            [
                "--runtime-state-root",
                str(ledger.root.parent),
                "--d2-run-id",
                _RUN_ID,
                "--expected-current-state",
                "GENERATION_SUCCEEDED",
                "--expected-ledger-sha256",
                str(evidence["CANONICAL_LEDGER_SHA256"]),
                "--expected-evidence-sha256",
                digest,
                "--implementation-commit",
                _COMMIT,
                "--evidence-json",
                str(path),
            ]
        )
        == 0
    )
    assert ledger.path.read_bytes() == before


def test_closed_retry_consumes_only_its_exact_predecessor(tmp_path: Path) -> None:
    ledger, _ = _attempt_fixture(tmp_path)
    evidence = _evidence(ledger)
    digest = forensic_misattribution_evidence_digest(evidence)
    ledger.close_misattributed_generation_forensic(
        _RUN_ID,
        expected_ledger_sha256=str(evidence["CANONICAL_LEDGER_SHA256"]),
        evidence_sha256=digest,
        implementation_commit=_COMMIT,
        evidence=evidence,
    )
    next_request = PhaseGExecutionRequest.from_presubmit(
        request_for(tmp_path), "D2_next-clean-0001"
    )
    assert ledger.create_attempt(next_request)["d2_run_id"] == "D2_next-clean-0001"


def test_unrelated_paid_failure_still_blocks_after_exact_chain_closure(tmp_path: Path) -> None:
    ledger, _ = _attempt_fixture(tmp_path)
    evidence = _evidence(ledger)
    digest = forensic_misattribution_evidence_digest(evidence)
    ledger.close_misattributed_generation_forensic(
        _RUN_ID,
        expected_ledger_sha256=str(evidence["CANONICAL_LEDGER_SHA256"]),
        evidence_sha256=digest,
        implementation_commit=_COMMIT,
        evidence=evidence,
    )
    unrelated_id = "D2_unrelated-paid-0001"
    ledger.create_attempt(
        PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), unrelated_id)
    )
    ledger.capture_baseline(
        unrelated_id,
        FlowSubmissionBaseline(_PROJECT_ID, frozenset(), frozenset(), 0, frozenset(), "0:", ()),
    )
    ledger.mark_generate_action(unrelated_id)
    ledger.record_state(unrelated_id, submission_state="SUBMISSION_ACKNOWLEDGED")
    ledger.record_state(
        unrelated_id,
        submission_state="GENERATION_FAILED",
        terminal_state="GENERATION_FAILED",
        accounted_credit_cost=0,
    )
    with pytest.raises(PhaseGError) as blocked:
        ledger.create_attempt(
            PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), "D2_next-clean-0002")
        )
    assert blocked.value.code == "PHASE_G_PAID_ATTEMPT_ALREADY_STARTED"
