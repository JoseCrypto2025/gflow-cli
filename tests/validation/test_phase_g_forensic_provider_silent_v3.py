from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_phase_g import phase_g_request

import gflow_cli.v2_runtime.phase_g as phase_g_module
from gflow_cli.v2_runtime.agent_recovery import AgentSettingsRecoveryJournal
from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    FlowSubmissionBaseline,
    PhaseGError,
    evaluate_d6_release,
)
from gflow_cli.v2_runtime.phase_g_forensic_provider_silent_v3 import (
    forensic_provider_silent_digest_v3,
    forensic_provider_silent_preview_v3,
    validate_provider_silent_evidence_v3,
)

_RUN_ID = "D2_8d892c38cc8f4c99"
_PROJECT = "34e60953-e534-46e9-ac65-993893e61065"
_EXECUTION = "9dc0380b-0a38-44f7-9c6c-8fe5262fdc2a"
_MEDIA = "d6e85926-120f-40ab-bfc0-6a788c521339"
_COMMIT = "a" * 40


def _fixture(tmp_path: Path) -> tuple[D2DailyCreditLedger, dict[str, object]]:
    request = replace(phase_g_request(tmp_path), d2_run_id=_RUN_ID)
    ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
    ledger.create_attempt(request)
    ledger.capture_baseline(
        _RUN_ID,
        FlowSubmissionBaseline(
            _PROJECT,
            frozenset({_MEDIA}),
            frozenset({_EXECUTION}),
            0,
            frozenset(),
            "4:" + "c" * 64,
            (),
        ),
    )
    ledger.mark_generate_action(_RUN_ID)
    ledger.record_state(
        _RUN_ID,
        submission_state="UNKNOWN_SUBMISSION_STATE",
        accounted_credit_cost=50,
    )
    ledger._mutate(lambda payload: _set_accounting_date(payload, _RUN_ID, "2026-10-23"))
    journal = AgentSettingsRecoveryJournal(ledger.root.parent, "PVA_0001", _PROJECT)
    journal.begin(correlation_id="v3-test", original_settings={}, intended_settings={})
    journal.set_state("CLEAN")
    row = next(item for item in ledger.attempts() if item["d2_run_id"] == _RUN_ID)
    sha = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    snapshots = [
        {
            "ELAPSED_SECONDS": elapsed,
            "HYDRATED": True,
            "ENUMERATION_COMPLETE": True,
            "PENDING_COUNT": 0,
            "CONFIRMATION_IDS": [],
            "EXECUTION_IDS": [_EXECUTION],
            "MEDIA_IDS": [_MEDIA],
            "DOM_MEDIA_IDS": [_MEDIA],
            "DOM_FINISHED_MEDIA_IDS": [_MEDIA],
        }
        for elapsed in (0, 30, 60)
    ]
    evidence: dict[str, object] = {
        "SCHEMA_VERSION": "D2_FORENSIC_PROVIDER_SILENT_CLOSURE_V3",
        "D2_RUN_ID": _RUN_ID,
        "CANONICAL_LEDGER_SHA256": sha,
        "IMPLEMENTATION_COMMIT": _COMMIT,
        "PROVIDER_ACCOUNT_ID": "PVA_0001",
        "PROVIDER_PROJECT_ID": _PROJECT,
        "PROVIDER_PROJECT_NAME": "AI Video Automation V2 - TEST",
        "CURRENT_SUBMISSION_STATE": "UNKNOWN_SUBMISSION_STATE",
        "CURRENT_TERMINAL_STATE": "NONE",
        "GENERATE_ACTION_COUNT": 1,
        "APPROVAL_ACTION_COUNT": 0,
        "ACCOUNTING_DATE_EXPECTED": "2026-10-23",
        "ACCOUNTED_CREDIT_COST_EXPECTED": "50",
        "SCENE_ID": row["scene_id"],
        "SCENE_PROMPT_ID": row["scene_prompt_id"],
        "GENERATION_PROFILE_ID": row["generation_profile_id"],
        "PROVIDER_ID": row["provider_id"],
        "MODEL_ID": row["model_id"],
        "GENERATION_MODE": row["generation_mode"],
        "PROMPT_SHA256": row["prompt_sha256"],
        "SOURCE_CONTEXT_SHA256": row["source_context_sha256"],
        "REQUESTED_DURATION_SEC": row["requested_duration_sec"],
        "REQUESTED_ASPECT_RATIO": row["requested_aspect_ratio"],
        "REQUESTED_OUTPUT_COUNT": row["requested_output_count"],
        "BASELINE_CAPTURED": True,
        "BASELINE_PENDING_COUNT": 0,
        "BASELINE_EXECUTION_IDS": [_EXECUTION],
        "BASELINE_MEDIA_IDS": [_MEDIA],
        "BASELINE_CONFIRMATION_IDS": [],
        "RECOVERY_JOURNAL_CLEAN": True,
        "PROVIDER_EXECUTION_ID_ABSENT": True,
        "PROVIDER_MEDIA_ID_ABSENT": True,
        "PROVIDER_MEDIA_IDS_ABSENT": True,
        "LOCAL_ATTEMPT_OUTPUT_ABSENT": True,
        "REMOTE_UPLOAD_ABSENT": True,
        "REMOTE_VERIFIED_ABSENT": True,
        "LIVE_PROJECT_HYDRATED": True,
        "LIVE_SNAPSHOT_COUNT": 3,
        "LIVE_OBSERVATION_DURATION_SECONDS": 60,
        "LIVE_PENDING_COUNT": 0,
        "LIVE_CONFIRMATION_COUNT": 0,
        "LIVE_CONFIRMATION_IDS": [],
        "LIVE_STRUCTURED_RECORDS_ENUMERATED": True,
        "LIVE_STRUCTURED_RECORD_COUNT": 1,
        "LIVE_STRUCTURED_RECORDS": [
            {
                "WORKFLOW_ID": _EXECUTION,
                "PROJECT_ID": _PROJECT,
                "MEDIA_ID": _MEDIA,
                "STATUS": 3,
            }
        ],
        "LIVE_SNAPSHOTS": snapshots,
        "HISTORICAL_EXECUTION_IDS": [_EXECUTION],
        "HISTORICAL_MEDIA_IDS": [_MEDIA],
        "CANDIDATE_NEW_EXECUTION_IDS": [],
        "CANDIDATE_NEW_MEDIA_IDS": [],
        "CANDIDATE_NEW_EXECUTION_COUNT": 0,
        "CANDIDATE_NEW_MEDIA_COUNT": 0,
        "ALL_LIVE_STRUCTURED_IDS_HISTORICAL": True,
        "CHAT_ACTIVITY_PRESENT": True,
        "CHAT_COUNT": 6,
        "CHAT_HASH_PREFIX_STABLE": False,
        "CHAT_STABLE_IDS_AVAILABLE": False,
        "CHAT_TIMESTAMPS_AVAILABLE": False,
        "CHAT_IDENTIFIER_SCAN_COMPLETE": True,
        "CHAT_PROVIDER_IDENTIFIER_PROOF": "NONE",
        "CHAT_PROVIDER_IDENTIFIERS": [],
        "CHAT_ATTRIBUTION_AUTHORITY": "NON_AUTHORITATIVE",
        "ATTRIBUTION_CLASSIFICATION": "NO_PROVIDER_EVIDENCE",
        "PROVIDER_OUTCOME": "UNKNOWN",
        "CREDIT_OUTCOME": "UNKNOWN",
        "PROVIDER_BILLING_OUTCOME": "UNKNOWN",
        "SAFE_AUTOMATIC_RETRY": False,
        "FORENSIC_COMPLETED_AT": datetime.now(UTC).isoformat(),
        "FORENSIC_REASON": "No new structured provider evidence was observed",
    }
    return ledger, evidence


def _set_accounting_date(payload: dict[str, object], run_id: str, date: str) -> dict[str, object]:
    rows = payload["attempts"]
    assert isinstance(rows, list)
    row = next(item for item in rows if item["d2_run_id"] == run_id)
    row["accounting_date"] = date
    return {"d2_run_id": run_id}


def _preview(ledger: D2DailyCreditLedger, evidence: dict[str, object]) -> dict[str, object]:
    ledger_sha = str(evidence["CANONICAL_LEDGER_SHA256"])
    evidence_sha = forensic_provider_silent_digest_v3(evidence)
    evidence["CANONICAL_LEDGER_SHA256"] = ledger_sha
    return forensic_provider_silent_preview_v3(
        ledger.root.parent,
        d2_run_id=_RUN_ID,
        expected_ledger_sha256=ledger_sha,
        expected_state="UNKNOWN_SUBMISSION_STATE",
        expected_evidence_sha256=evidence_sha,
        implementation_commit=_COMMIT,
        evidence=evidence,
        evidence_sha256=evidence_sha,
    )


def test_v3_provider_silent_unknown_attempt_previews_and_allows_chat_activity(
    tmp_path: Path,
) -> None:
    ledger, evidence = _fixture(tmp_path)
    assert validate_provider_silent_evidence_v3(evidence)["ATTRIBUTION_CLASSIFICATION"] == (
        "NO_PROVIDER_EVIDENCE"
    )
    assert _preview(ledger, evidence)["attempt"]["accounted_credit_cost"] == "50"


def test_v3_chat_hash_prefix_instability_is_context_not_success(tmp_path: Path) -> None:
    ledger, evidence = _fixture(tmp_path)
    assert evidence["CHAT_HASH_PREFIX_STABLE"] is False
    assert _preview(ledger, evidence)["evidence"]["PROVIDER_OUTCOME"] == "UNKNOWN"


def test_v3_chat_hash_prefix_stability_is_context_only(tmp_path: Path) -> None:
    ledger, evidence = _fixture(tmp_path)
    evidence["CHAT_HASH_PREFIX_STABLE"] = True
    assert _preview(ledger, evidence)["evidence"]["PROVIDER_OUTCOME"] == "UNKNOWN"


def test_v3_observation_span_allows_first_hydrated_snapshot_after_zero(
    tmp_path: Path,
) -> None:
    _ledger, evidence = _fixture(tmp_path)
    evidence["LIVE_OBSERVATION_DURATION_SECONDS"] = 62
    snapshots = evidence["LIVE_SNAPSHOTS"]
    assert isinstance(snapshots, list)
    for snapshot, elapsed in zip(snapshots, (2, 31, 62), strict=True):
        assert isinstance(snapshot, dict)
        snapshot["ELAPSED_SECONDS"] = elapsed

    assert validate_provider_silent_evidence_v3(evidence)["ATTRIBUTION_CLASSIFICATION"] == (
        "NO_PROVIDER_EVIDENCE"
    )


def test_v3_rejects_observation_span_shorter_than_sixty_seconds(tmp_path: Path) -> None:
    _ledger, evidence = _fixture(tmp_path)
    evidence["LIVE_OBSERVATION_DURATION_SECONDS"] = 61
    snapshots = evidence["LIVE_SNAPSHOTS"]
    assert isinstance(snapshots, list)
    for snapshot, elapsed in zip(snapshots, (2, 31, 61), strict=True):
        assert isinstance(snapshot, dict)
        snapshot["ELAPSED_SECONDS"] = elapsed

    with pytest.raises(PhaseGError, match="observation span is outside"):
        validate_provider_silent_evidence_v3(evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("CANDIDATE_NEW_EXECUTION_IDS", ["new-workflow"]),
        ("CANDIDATE_NEW_EXECUTION_COUNT", 1),
        ("LIVE_PENDING_COUNT", 1),
        ("LIVE_CONFIRMATION_COUNT", 1),
        ("LIVE_CONFIRMATION_IDS", ["new-confirmation"]),
        ("CHAT_PROVIDER_IDENTIFIER_PROOF", "UNMATCHED"),
        ("CHAT_PROVIDER_IDENTIFIERS", ["00000000-0000-0000-0000-000000000001"]),
        ("LIVE_OBSERVATION_DURATION_SECONDS", 59),
        ("LIVE_SNAPSHOT_COUNT", 2),
    ],
)
def test_v3_rejects_ambiguous_or_active_live_evidence(
    tmp_path: Path, field: str, value: object
) -> None:
    _ledger, evidence = _fixture(tmp_path)
    evidence[field] = value
    with pytest.raises(PhaseGError):
        validate_provider_silent_evidence_v3(evidence)


@pytest.mark.parametrize("field", ["DOM_MEDIA_IDS", "DOM_FINISHED_MEDIA_IDS"])
def test_v3_rejects_nonhistorical_dom_media_identity(tmp_path: Path, field: str) -> None:
    _, evidence = _fixture(tmp_path)
    snapshots = evidence["LIVE_SNAPSHOTS"]
    assert isinstance(snapshots, list)
    for snapshot in snapshots:
        assert isinstance(snapshot, dict)
        snapshot[field] = ["00000000-0000-0000-0000-000000000001"]
    with pytest.raises(PhaseGError, match="nonhistorical provider identity"):
        validate_provider_silent_evidence_v3(evidence)


def test_v3_rejects_changing_provider_snapshots(tmp_path: Path) -> None:
    _, evidence = _fixture(tmp_path)
    snapshots = evidence["LIVE_SNAPSHOTS"]
    assert isinstance(snapshots, list)
    assert isinstance(snapshots[1], dict)
    snapshots[1]["DOM_MEDIA_IDS"] = []
    with pytest.raises(PhaseGError, match="snapshots changed"):
        validate_provider_silent_evidence_v3(evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("CURRENT_SUBMISSION_STATE", "GENERATION_SUCCEEDED"),
        ("CURRENT_TERMINAL_STATE", "GENERATION_SUCCEEDED"),
        ("GENERATE_ACTION_COUNT", 2),
        ("APPROVAL_ACTION_COUNT", 1),
        ("ACCOUNTING_DATE_EXPECTED", "2026-10-24"),
        ("ACCOUNTED_CREDIT_COST_EXPECTED", "40"),
        ("PROVIDER_OUTCOME", "SUCCESS"),
        ("CREDIT_OUTCOME", "NO_CHARGE"),
        ("PROVIDER_BILLING_OUTCOME", "CHARGED"),
        ("SAFE_AUTOMATIC_RETRY", True),
        ("CHAT_ATTRIBUTION_AUTHORITY", "AUTHORITATIVE"),
        ("ALL_LIVE_STRUCTURED_IDS_HISTORICAL", False),
    ],
)
def test_v3_rejects_wrong_state_accounting_or_outcome(
    tmp_path: Path, field: str, value: object
) -> None:
    _ledger, evidence = _fixture(tmp_path)
    evidence[field] = value
    with pytest.raises(PhaseGError):
        validate_provider_silent_evidence_v3(evidence)


def test_v3_rejects_new_structured_execution_and_media_candidates(tmp_path: Path) -> None:
    ledger, evidence = _fixture(tmp_path)
    evidence["LIVE_SNAPSHOTS"][1]["EXECUTION_IDS"] = ["new-workflow"]
    with pytest.raises(PhaseGError, match="nonhistorical"):
        _preview(ledger, evidence)

    ledger, evidence = _fixture(tmp_path / "media")
    evidence["LIVE_SNAPSHOTS"][1]["MEDIA_IDS"] = ["00000000-0000-0000-0000-000000000001"]
    with pytest.raises(PhaseGError, match="nonhistorical"):
        _preview(ledger, evidence)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("PROVIDER_EXECUTION_ID_ABSENT", False),
        ("PROVIDER_MEDIA_ID_ABSENT", False),
        ("PROVIDER_MEDIA_IDS_ABSENT", False),
        ("LOCAL_ATTEMPT_OUTPUT_ABSENT", False),
        ("REMOTE_UPLOAD_ABSENT", False),
        ("REMOTE_VERIFIED_ABSENT", False),
        ("RECOVERY_JOURNAL_CLEAN", False),
        ("BASELINE_CAPTURED", False),
    ],
)
def test_v3_requires_every_absence_and_baseline_gate(
    tmp_path: Path, field: str, value: object
) -> None:
    _ledger, evidence = _fixture(tmp_path)
    evidence[field] = value
    with pytest.raises(PhaseGError):
        validate_provider_silent_evidence_v3(evidence)


def test_v3_preview_binds_attempt_providers_artifacts_and_exact_baseline(tmp_path: Path) -> None:
    ledger, evidence = _fixture(tmp_path)
    ledger.record_state(
        _RUN_ID, submission_state="UNKNOWN_SUBMISSION_STATE", provider_execution_id="new"
    )
    evidence["CANONICAL_LEDGER_SHA256"] = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    with pytest.raises(PhaseGError):
        _preview(ledger, evidence)

    ledger, evidence = _fixture(tmp_path / "media-id")
    ledger.record_state(
        _RUN_ID, submission_state="UNKNOWN_SUBMISSION_STATE", provider_media_id=_MEDIA
    )
    evidence["CANONICAL_LEDGER_SHA256"] = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    with pytest.raises(PhaseGError):
        _preview(ledger, evidence)

    ledger, evidence = _fixture(tmp_path / "media-ids")
    ledger.record_state(
        _RUN_ID, submission_state="UNKNOWN_SUBMISSION_STATE", provider_media_ids=[_MEDIA]
    )
    evidence["CANONICAL_LEDGER_SHA256"] = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    with pytest.raises(PhaseGError):
        _preview(ledger, evidence)

    ledger, evidence = _fixture(tmp_path / "remote")
    ledger._mutate(lambda payload: _set_attempt_value(payload, _RUN_ID, "remote_verified", True))
    evidence["CANONICAL_LEDGER_SHA256"] = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    with pytest.raises(PhaseGError):
        _preview(ledger, evidence)


def test_v3_rejects_local_artifact_and_dirty_journal(tmp_path: Path) -> None:
    ledger, evidence = _fixture(tmp_path)
    output = Path(str(ledger.attempts()[0]["temp_root"])) / _RUN_ID
    output.mkdir(parents=True)
    (output / "clip.mp4").write_bytes(b"local artifact")
    with pytest.raises(PhaseGError, match="local attempt output exists"):
        _preview(ledger, evidence)

    ledger, evidence = _fixture(tmp_path / "journal")
    AgentSettingsRecoveryJournal(ledger.root.parent, "PVA_0001", _PROJECT).set_state("DIRTY")
    with pytest.raises(PhaseGError, match="not CLEAN"):
        _preview(ledger, evidence)

    ledger, evidence = _fixture(tmp_path / "baseline")
    ledger._mutate(lambda payload: _set_attempt_value(payload, _RUN_ID, "baseline_media_ids", []))
    evidence["CANONICAL_LEDGER_SHA256"] = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    with pytest.raises(PhaseGError):
        _preview(ledger, evidence)


def _set_attempt_value(
    payload: dict[str, object], run_id: str, key: str, value: object
) -> dict[str, object]:
    rows = payload["attempts"]
    assert isinstance(rows, list)
    row = next(item for item in rows if item["d2_run_id"] == run_id)
    row[key] = value
    return {"d2_run_id": run_id}


def test_v3_evidence_hash_and_ledger_hash_are_required(tmp_path: Path) -> None:
    ledger, evidence = _fixture(tmp_path)
    before = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    digest = forensic_provider_silent_digest_v3(evidence)
    with pytest.raises(PhaseGError, match="SHA differs"):
        forensic_provider_silent_preview_v3(
            ledger.root.parent,
            d2_run_id=_RUN_ID,
            expected_ledger_sha256=before,
            expected_state="UNKNOWN_SUBMISSION_STATE",
            expected_evidence_sha256="0" * 64,
            implementation_commit=_COMMIT,
            evidence=evidence,
            evidence_sha256=digest,
        )


def test_v3_closure_preserves_unknown_reservation_and_d6_block(tmp_path: Path) -> None:
    ledger, evidence = _fixture(tmp_path)
    before_rows = ledger.attempts()
    sha = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    evidence["CANONICAL_LEDGER_SHA256"] = sha
    digest = forensic_provider_silent_digest_v3(evidence)
    preview = forensic_provider_silent_preview_v3(
        ledger.root.parent,
        d2_run_id=_RUN_ID,
        expected_ledger_sha256=sha,
        expected_state="UNKNOWN_SUBMISSION_STATE",
        expected_evidence_sha256=digest,
        implementation_commit=_COMMIT,
        evidence=evidence,
        evidence_sha256=digest,
    )
    assert preview["attempt"]["accounted_credit_cost"] == "50"
    closed = ledger.close_provider_silent_forensic_v3(
        _RUN_ID,
        expected_ledger_sha256=sha,
        evidence_sha256=digest,
        implementation_commit=_COMMIT,
        evidence=evidence,
    )
    assert closed["submission_state"] == "FORENSICALLY_CLOSED_UNKNOWN"
    assert closed["terminal_state"] == "FORENSICALLY_CLOSED_UNKNOWN"
    assert closed["generate_action_count"] == 1
    assert closed["approval_action_count"] == 0
    assert closed["accounting_date"] == "2026-10-23"
    assert closed["accounted_credit_cost"] == "50"
    assert closed["provider_execution_id"] is None
    assert closed["provider_media_id"] is None
    assert closed["provider_media_ids"] == []
    assert closed["provider_outcome"] == "UNKNOWN"
    assert closed["credit_outcome"] == "UNKNOWN"
    assert closed["provider_billing_outcome"] == "UNKNOWN"
    assert closed["forensic_classification"] == "NO_PROVIDER_EVIDENCE"
    assert closed["forensic_closure_contract_version"] == ("D2_FORENSIC_PROVIDER_SILENT_CLOSURE_V3")
    assert ledger._accounted(ledger._read(), "2026-10-23") == 50
    assert len(ledger.attempts()) == len(before_rows)

    with pytest.raises(PhaseGError):
        ledger.mark_generate_action(_RUN_ID)
    d6_request = replace(
        phase_g_request(tmp_path),
        d2_run_id="D6_test-run-0001",
        prerequisite_d2_run_id=_RUN_ID,
    )
    result = evaluate_d6_release(
        prerequisite_d2_run_id=_RUN_ID, d6_request=d6_request, ledger=ledger
    )
    assert not result.eligible


def test_v3_distinct_next_day_attempt_stays_under_normal_daily_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, evidence = _fixture(tmp_path)
    sha = hashlib.sha256(ledger.path.read_bytes()).hexdigest()
    evidence["CANONICAL_LEDGER_SHA256"] = sha
    digest = forensic_provider_silent_digest_v3(evidence)
    ledger.close_provider_silent_forensic_v3(
        _RUN_ID,
        expected_ledger_sha256=sha,
        evidence_sha256=digest,
        implementation_commit=_COMMIT,
        evidence=evidence,
    )
    request = replace(phase_g_request(tmp_path), d2_run_id="D2_test-run-0002")
    assert ledger.create_attempt(request)["d2_run_id"] == "D2_test-run-0002"
    ledger.capture_baseline(
        "D2_test-run-0002",
        FlowSubmissionBaseline(_PROJECT, frozenset(), frozenset(), 0, frozenset(), "0:"),
    )

    class AccountingDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return cls(2026, 10, 23, 12, tzinfo=tz or UTC)

    monkeypatch.setattr(phase_g_module, "datetime", AccountingDateTime)
    with pytest.raises(PhaseGError) as same_day:
        ledger.mark_generate_action("D2_test-run-0002")
    assert same_day.value.code == "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED"

    class TomorrowDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return cls(2026, 10, 24, tzinfo=tz or UTC)

    monkeypatch.setattr(phase_g_module, "datetime", TomorrowDateTime)
    assert ledger.mark_generate_action("D2_test-run-0002")["generate_action_count"] == 1


def test_v3_operator_evidence_is_canonical_compact_json(tmp_path: Path) -> None:
    _ledger, evidence = _fixture(tmp_path)
    canonical = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert json.loads(canonical)["D2_RUN_ID"] == _RUN_ID
