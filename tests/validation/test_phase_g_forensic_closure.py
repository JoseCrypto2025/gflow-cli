from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_phase_g import _capture_ledger_baseline, phase_g_request

from gflow_cli.v2_runtime.phase_g import D2DailyCreditLedger, PhaseGError
from gflow_cli.v2_runtime.phase_g_forensic_closure import (
    forensic_closure_preview,
    forensic_evidence_digest,
    main,
    validate_forensic_evidence,
)


def _evidence(*, ledger_sha: str = "a" * 64, run_id: str = "D2_test-run-0001") -> dict[str, object]:
    return {
        "D2_RUN_ID": run_id,
        "CANONICAL_LEDGER_SHA256": ledger_sha,
        "CANONICAL_LEDGER_FOUND": True,
        "LEDGER_SHA_VERIFIED": True,
        "RECOVERY_JOURNAL_CLEAN": True,
        "GENERATE_ACTION_COUNT": 1,
        "PROVIDER_EXECUTION_ID_ABSENT": True,
        "PROVIDER_MEDIA_ID_ABSENT": True,
        "PHASE_G_RESULT_ABSENT": True,
        "HISTORICAL_TEMP_OUTPUT_ABSENT": True,
        "EXACT_TEST_LANE_RECOVERED": True,
        "EXACT_TEST_IDENTITY_VERIFIED": True,
        "CURRENT_PROJECT_MEDIA_COUNT": 0,
        "CURRENT_PROJECT_EXECUTION_COUNT": 0,
        "CURRENT_PROJECT_PENDING_COUNT": 0,
        "CURRENT_PROJECT_CONFIRMATION_COUNT": 0,
        "CURRENT_PROJECT_CHAT_COUNT": 0,
        "PROVIDER_BILLING_OUTCOME": "UNKNOWN",
        "OPERATING_CLASSIFICATION": "UNKNOWN_SUBMISSION_STATE",
        "SAFE_TO_RETRY": "NO",
        "FORENSIC_COMPLETED_AT": "2026-10-19T00:00:00Z",
        "FORENSIC_REASON": "Historical outcome cannot be deterministically reconstructed",
    }


def _write_evidence(path: Path, *, ledger_sha: str) -> tuple[dict[str, object], str]:
    evidence = _evidence(ledger_sha=ledger_sha)
    raw = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    path.write_bytes(raw)
    return evidence, hashlib.sha256(raw).hexdigest()


def _unknown_ledger(tmp_path: Path) -> tuple[D2DailyCreditLedger, str]:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.create_attempt(phase_g_request(tmp_path))
    run_id = "D2_test-run-0001"
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action(run_id)
    ledger.record_state(run_id, submission_state="UNKNOWN_SUBMISSION_STATE")
    return ledger, run_id


def test_evidence_contract_is_complete_and_rejects_unsupported_fields() -> None:
    assert validate_forensic_evidence(_evidence())["PROVIDER_BILLING_OUTCOME"] == "UNKNOWN"
    incomplete = _evidence()
    incomplete["CURRENT_PROJECT_MEDIA_COUNT"] = 1
    with pytest.raises(PhaseGError, match="zero observed activity"):
        validate_forensic_evidence(incomplete)
    raw_data = _evidence()
    raw_data["cookies"] = "forbidden"
    with pytest.raises(PhaseGError, match="incomplete or contain unsupported"):
        validate_forensic_evidence(raw_data)


def test_v1_contract_still_rejects_any_project_chat() -> None:
    evidence = _evidence()
    evidence["CURRENT_PROJECT_CHAT_COUNT"] = 1
    with pytest.raises(PhaseGError, match="zero observed activity"):
        validate_forensic_evidence(evidence)


def test_dry_run_previews_without_mutating_and_preserves_reservation(tmp_path: Path) -> None:
    ledger, run_id = _unknown_ledger(tmp_path)
    before = ledger.path.read_bytes()
    expected_sha = hashlib.sha256(before).hexdigest()
    evidence_path = tmp_path / "evidence.json"
    evidence, evidence_sha = _write_evidence(evidence_path, ledger_sha=expected_sha)
    preview = forensic_closure_preview(
        tmp_path / "runtime",
        d2_run_id=run_id,
        expected_ledger_sha256=expected_sha,
        expected_state="UNKNOWN_SUBMISSION_STATE",
        expected_evidence_sha256=evidence_sha,
        implementation_commit="a" * 40,
        evidence=evidence,
        evidence_sha256=evidence_sha,
    )
    assert preview["attempt"]["accounted_credit_cost"] == "50"
    assert ledger.path.read_bytes() == before
    result = main(
        [
            "--runtime-state-root",
            str(tmp_path / "runtime"),
            "--d2-run-id",
            run_id,
            "--expected-current-state",
            "UNKNOWN_SUBMISSION_STATE",
            "--expected-ledger-sha256",
            expected_sha,
            "--expected-evidence-sha256",
            evidence_sha,
            "--implementation-commit",
            "a" * 40,
            "--evidence-json",
            str(evidence_path),
        ]
    )
    assert result == 0
    assert ledger.path.read_bytes() == before


def test_apply_closes_old_attempt_terminally_preserving_cost_and_unknown_billing(
    tmp_path: Path,
) -> None:
    ledger, run_id = _unknown_ledger(tmp_path)
    with pytest.raises(PhaseGError) as bypass:
        ledger.record_state(
            run_id,
            submission_state="FORENSICALLY_CLOSED_UNKNOWN",
            terminal_state="FORENSICALLY_CLOSED_UNKNOWN",
        )
    assert bypass.value.code == "PHASE_G_FORENSIC_OPERATOR_REQUIRED"
    expected_sha = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    evidence_path = tmp_path / "evidence.json"
    evidence, evidence_sha = _write_evidence(evidence_path, ledger_sha=expected_sha)
    closed = ledger.close_unknown_forensic(
        run_id,
        expected_ledger_sha256=expected_sha,
        evidence_sha256=evidence_sha,
        implementation_commit="a" * 40,
        evidence=evidence,
    )
    assert closed["submission_state"] == "FORENSICALLY_CLOSED_UNKNOWN"
    assert closed["terminal_state"] == "FORENSICALLY_CLOSED_UNKNOWN"
    assert closed["accounted_credit_cost"] == "50"
    assert closed["provider_billing_outcome"] == "UNKNOWN"
    assert closed["provider_outcome"] == "UNKNOWN"
    assert closed["credit_outcome"] == "UNKNOWN"
    assert closed["forensic_reason"] == evidence["FORENSIC_REASON"]
    assert closed["forensic_evidence_sha256"] == evidence_sha
    assert closed["forensic_implementation_commit"] == "a" * 40
    assert closed["forensic_closed_at"] == evidence["FORENSIC_COMPLETED_AT"]
    with pytest.raises(PhaseGError) as terminal:
        ledger.record_state(run_id, submission_state="GENERATION_PENDING")
    assert terminal.value.code == "PHASE_G_ATTEMPT_TERMINAL"
    with pytest.raises(PhaseGError) as duplicate:
        ledger.mark_generate_action(run_id)
    assert duplicate.value.code == "PHASE_G_DUPLICATE_SUBMIT_FORBIDDEN"


def test_closed_unknown_allows_only_a_new_run_id(tmp_path: Path) -> None:
    ledger, run_id = _unknown_ledger(tmp_path)
    expected_sha = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    evidence = _evidence(ledger_sha=expected_sha)
    evidence_sha = forensic_evidence_digest(evidence)
    ledger.close_unknown_forensic(
        run_id,
        expected_ledger_sha256=expected_sha,
        evidence_sha256=evidence_sha,
        implementation_commit="a" * 40,
        evidence=evidence,
    )
    with pytest.raises(PhaseGError) as same_id:
        ledger.create_attempt(phase_g_request(tmp_path))
    assert same_id.value.code == "PHASE_G_DUPLICATE_RUN_ID"
    next_request = replace(phase_g_request(tmp_path), d2_run_id="D2_test-run-0002")
    assert ledger.create_attempt(next_request)["d2_run_id"] == "D2_test-run-0002"


def test_closure_fails_closed_on_changed_sha_or_provider_identity(tmp_path: Path) -> None:
    ledger, run_id = _unknown_ledger(tmp_path)
    with pytest.raises(PhaseGError) as changed:
        changed_evidence = _evidence(ledger_sha="0" * 64)
        ledger.close_unknown_forensic(
            run_id,
            expected_ledger_sha256="0" * 64,
            evidence_sha256=forensic_evidence_digest(changed_evidence),
            implementation_commit="a" * 40,
            evidence=changed_evidence,
        )
    assert changed.value.code == "PHASE_G_LEDGER_CHANGED"

    ledger.record_state(
        run_id, submission_state="UNKNOWN_SUBMISSION_STATE", provider_media_id="media-1"
    )
    expected_sha = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    evidence = _evidence(ledger_sha=expected_sha)
    with pytest.raises(PhaseGError) as provider_id:
        ledger.close_unknown_forensic(
            run_id,
            expected_ledger_sha256=expected_sha,
            evidence_sha256=forensic_evidence_digest(evidence),
            implementation_commit="a" * 40,
            evidence=evidence,
        )
    assert provider_id.value.code == "PHASE_G_PROVIDER_OUTCOME_PRESENT"


def test_evidence_sha_mismatch_is_rejected(tmp_path: Path) -> None:
    ledger, run_id = _unknown_ledger(tmp_path)
    expected_sha = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    evidence = _evidence(ledger_sha=expected_sha)
    evidence_sha = forensic_evidence_digest(evidence)
    with pytest.raises(PhaseGError) as mismatch:
        forensic_closure_preview(
            tmp_path / "runtime",
            d2_run_id=run_id,
            expected_ledger_sha256=expected_sha,
            expected_state="UNKNOWN_SUBMISSION_STATE",
            expected_evidence_sha256="0" * 64,
            implementation_commit="a" * 40,
            evidence=evidence,
            evidence_sha256=evidence_sha,
        )
    assert mismatch.value.code == "PHASE_G_FORENSIC_EVIDENCE_HASH_MISMATCH"


def test_unknown_blocks_new_attempt_before_closure(tmp_path: Path) -> None:
    ledger, _run_id = _unknown_ledger(tmp_path)
    next_request = replace(phase_g_request(tmp_path), d2_run_id="D2_test-run-0002")
    with pytest.raises(PhaseGError) as blocked:
        ledger.create_attempt(next_request)
    assert blocked.value.code == "PHASE_G_ATTEMPT_UNRESOLVED"
