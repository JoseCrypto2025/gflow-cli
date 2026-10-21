from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_phase_g import phase_g_request

from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    FlowSubmissionBaseline,
    PhaseGError,
    evaluate_d6_release,
)
from gflow_cli.v2_runtime.phase_g_forensic_closure_v2 import (
    forensic_closure_preview_v2,
    forensic_evidence_digest_v2,
    validate_forensic_evidence_v2,
)


def _evidence(*, ledger_sha: str = "a" * 64, run_id: str = "D2_test-run-0001") -> dict[str, object]:
    bubble_hashes = [hashlib.sha256(f"chat-{index}".encode()).hexdigest() for index in range(4)]
    bubbles = [
        {
            "TEXT_SHA256": digest,
            "EXACT_PROMPT_MATCH": index == 0,
            "EXACT_SUBSTRING_MATCH": index == 0,
            "MEDIA_UUIDS": [],
            "D2_RUN_ID_PRESENT": False,
            "PROVIDER_EXECUTION_IDS": [],
            "STABLE_ID_AVAILABLE": False,
            "STABLE_ID_SHA256": None,
            "TIMESTAMP_AVAILABLE": False,
            "TIMESTAMP_UTC": None,
        }
        for index, digest in enumerate(bubble_hashes)
    ]
    return {
        "SCHEMA_VERSION": "D2_FORENSIC_CLOSURE_V2",
        "D2_RUN_ID": run_id,
        "CANONICAL_LEDGER_SHA256": ledger_sha,
        "CANONICAL_LEDGER_FOUND": True,
        "LEDGER_SHA_VERIFIED": True,
        "RECOVERY_JOURNAL_CLEAN": True,
        "PROVIDER_EXECUTION_ID_ABSENT": True,
        "PROVIDER_MEDIA_IDS_ABSENT": True,
        "PHASE_G_RESULT_ABSENT": True,
        "HISTORICAL_TEMP_OUTPUT_ABSENT": True,
        "EXACT_TEST_LANE_RECOVERED": True,
        "EXACT_TEST_IDENTITY_VERIFIED": True,
        "EXACT_TEST_PROJECT_VERIFIED": True,
        "CHAT_SNAPSHOT_HYDRATED": True,
        "CHAT_ENUMERATION_COMPLETE": True,
        "PROVIDER_ACCOUNT_ID": "PVA_0001",
        "PROVIDER_PROJECT_ID": "34e60953-e534-46e9-ac65-993893e61065",
        "PROVIDER_PROJECT_NAME": "AI Video Automation V2 - TEST",
        "GENERATE_ACTION_COUNT": 1,
        "APPROVAL_ACTION_COUNT": 0,
        "CURRENT_PROJECT_MEDIA_COUNT": 0,
        "CURRENT_PROJECT_EXECUTION_COUNT": 0,
        "CURRENT_PROJECT_PENDING_COUNT": 0,
        "CURRENT_PROJECT_CONFIRMATION_COUNT": 0,
        "CURRENT_PROJECT_CHAT_COUNT": 4,
        "CURRENT_PROJECT_MEDIA_IDS": [],
        "CURRENT_PROJECT_EXECUTION_IDS": [],
        "CURRENT_PROJECT_CONFIRMATION_IDS": [],
        "BASELINE_AGENT_TURN_ID": "0:",
        "BASELINE_CONFIRMATION_IDS": [],
        "BASELINE_CHAT_COUNT": 0,
        "BASELINE_CHAT_TAIL_HASH": "",
        "BASELINE_CHAT_BUBBLE_SHA256S": [],
        "CURRENT_CHAT_TAIL_HASH": bubble_hashes[-1],
        "CURRENT_CHAT_BUBBLE_SHA256S": bubble_hashes,
        "CHAT_BUBBLES": bubbles,
        "PROMPT_SHA256": "b" * 64,
        "ACCOUNTED_CREDIT_COST_EXPECTED": "50",
        "PROVIDER_OUTCOME": "UNKNOWN",
        "CREDIT_OUTCOME": "UNKNOWN",
        "PROVIDER_BILLING_OUTCOME": "UNKNOWN",
        "OPERATING_CLASSIFICATION": "CHAT_ONLY_UNRESOLVED_ACTIVITY",
        "SAFE_TO_RETRY": "NO",
        "FORENSIC_COMPLETED_AT": datetime.now(UTC).isoformat(),
        "FORENSIC_REASON": "Provider outcome remains unresolved after read only inspection",
    }


def _unknown_ledger(tmp_path: Path) -> tuple[D2DailyCreditLedger, str]:
    request = phase_g_request(tmp_path)
    ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
    ledger.create_attempt(request)
    run_id = request.d2_run_id
    ledger.capture_baseline(
        run_id,
        FlowSubmissionBaseline(
            "34e60953-e534-46e9-ac65-993893e61065",
            frozenset(),
            frozenset(),
            0,
            frozenset(),
            "0:",
            (),
        ),
    )
    ledger.mark_generate_action(run_id)
    ledger.record_state(run_id, submission_state="UNKNOWN_SUBMISSION_STATE")
    return ledger, run_id


def test_v2_accepts_complete_chat_only_unresolved_evidence() -> None:
    assert (
        validate_forensic_evidence_v2(_evidence())["OPERATING_CLASSIFICATION"]
        == "CHAT_ONLY_UNRESOLVED_ACTIVITY"
    )


def test_v2_preserves_existing_baseline_confirmation_ids() -> None:
    evidence = _evidence()
    evidence["BASELINE_CONFIRMATION_IDS"] = ["previous-confirmation"]
    assert validate_forensic_evidence_v2(evidence)["BASELINE_CONFIRMATION_IDS"] == [
        "previous-confirmation"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("CURRENT_PROJECT_MEDIA_COUNT", 1),
        ("CURRENT_PROJECT_EXECUTION_COUNT", 1),
        ("CURRENT_PROJECT_PENDING_COUNT", 1),
        ("CURRENT_PROJECT_CONFIRMATION_COUNT", 1),
        ("CURRENT_PROJECT_MEDIA_IDS", ["00000000-0000-0000-0000-000000000001"]),
        ("CURRENT_PROJECT_EXECUTION_IDS", ["exec-1"]),
        ("CURRENT_PROJECT_CONFIRMATION_IDS", ["confirmation-1"]),
    ],
)
def test_v2_rejects_non_chat_provider_activity(field: str, value: object) -> None:
    evidence = _evidence()
    evidence[field] = value
    with pytest.raises(PhaseGError):
        validate_forensic_evidence_v2(evidence)


def test_v2_rejects_provider_ids_in_chat_evidence() -> None:
    evidence = _evidence()
    bubbles = evidence["CHAT_BUBBLES"]
    assert isinstance(bubbles, list)
    bubbles[0]["PROVIDER_EXECUTION_IDS"] = ["exec-1"]
    with pytest.raises(PhaseGError, match="provider or run identifier"):
        validate_forensic_evidence_v2(evidence)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda item: item.pop("CURRENT_CHAT_TAIL_HASH"),
        lambda item: item["CHAT_BUBBLES"].pop(),
        lambda item: item["CHAT_BUBBLES"][0].pop("TIMESTAMP_AVAILABLE"),
        lambda item: item.__setitem__("CURRENT_CHAT_BUBBLE_SHA256S", ["bad"]),
        lambda item: item.__setitem__("SCHEMA_VERSION", "unknown"),
        lambda item: item.__setitem__("BASELINE_AGENT_TURN_ID", "bad"),
    ],
)
def test_v2_rejects_incomplete_or_malformed_evidence(mutation) -> None:
    evidence = _evidence()
    mutation(evidence)
    with pytest.raises(PhaseGError):
        validate_forensic_evidence_v2(evidence)


def test_v2_requires_captured_hashes_for_nonzero_baseline() -> None:
    evidence = _evidence()
    evidence["BASELINE_AGENT_TURN_ID"] = "1:" + "c" * 64
    evidence["BASELINE_CHAT_COUNT"] = 1
    evidence["BASELINE_CHAT_TAIL_HASH"] = "c" * 64
    with pytest.raises(PhaseGError, match="ordered baseline bubble hashes"):
        validate_forensic_evidence_v2(evidence)


def test_prompt_match_does_not_change_unresolved_classification() -> None:
    evidence = _evidence()
    bubbles = evidence["CHAT_BUBBLES"]
    assert isinstance(bubbles, list)
    bubbles[0]["EXACT_PROMPT_MATCH"] = False
    bubbles[0]["EXACT_SUBSTRING_MATCH"] = True
    assert validate_forensic_evidence_v2(evidence)["OPERATING_CLASSIFICATION"] == (
        "CHAT_ONLY_UNRESOLVED_ACTIVITY"
    )


def test_v2_dry_run_preserves_ledger_and_closure_keeps_unknown_and_reservation(
    tmp_path: Path,
) -> None:
    ledger, run_id = _unknown_ledger(tmp_path)
    before = ledger.path.read_bytes()
    ledger_sha = hashlib.sha256(before).hexdigest()
    evidence = _evidence(ledger_sha=ledger_sha)
    evidence_sha = forensic_evidence_digest_v2(evidence)
    preview = forensic_closure_preview_v2(
        ledger.root.parent,
        d2_run_id=run_id,
        expected_ledger_sha256=ledger_sha,
        expected_state="UNKNOWN_SUBMISSION_STATE",
        expected_evidence_sha256=evidence_sha,
        implementation_commit="a" * 40,
        evidence=evidence,
        evidence_sha256=evidence_sha,
    )
    assert preview["attempt"]["accounted_credit_cost"] == "50"
    assert ledger.path.read_bytes() == before

    closed = ledger.close_unknown_forensic_v2(
        run_id,
        expected_ledger_sha256=ledger_sha,
        evidence_sha256=evidence_sha,
        implementation_commit="a" * 40,
        evidence=evidence,
    )
    assert closed["submission_state"] == "FORENSICALLY_CLOSED_UNKNOWN"
    assert closed["accounted_credit_cost"] == "50"
    assert closed["provider_outcome"] == "UNKNOWN"
    assert closed["credit_outcome"] == "UNKNOWN"
    assert closed["provider_billing_outcome"] == "UNKNOWN"
    assert closed["forensic_closure_contract_version"] == "D2_FORENSIC_CLOSURE_V2"

    with pytest.raises(PhaseGError) as same_run_retry:
        ledger.mark_generate_action(run_id)
    assert same_run_retry.value.code == "PHASE_G_DUPLICATE_SUBMIT_FORBIDDEN"
    with pytest.raises(PhaseGError) as duplicate:
        ledger.create_attempt(phase_g_request(tmp_path))
    assert duplicate.value.code == "PHASE_G_DUPLICATE_RUN_ID"
    assert (
        ledger.create_attempt(replace(phase_g_request(tmp_path), d2_run_id="D2_test-run-0002"))[
            "d2_run_id"
        ]
        == "D2_test-run-0002"
    )
    d6_request = replace(
        phase_g_request(tmp_path),
        d2_run_id="D6_test-run-0001",
        prerequisite_d2_run_id=run_id,
    )
    d6 = evaluate_d6_release(
        prerequisite_d2_run_id=run_id,
        d6_request=d6_request,
        ledger=ledger,
    )
    assert not d6.eligible
    assert d6.failure_code == "D6_PREREQUISITE_NOT_SUCCEEDED"
