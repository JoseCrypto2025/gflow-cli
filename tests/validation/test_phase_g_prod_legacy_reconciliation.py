from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    SOURCE_LIVE_GOOGLE_SHEETS,
    GoogleSheetsSnapshot,
)
from gflow_cli.v2_runtime.phase_g_prod_legacy_reconciliation import (
    LEGACY_PROD_CONTRACT_VERSION,
    LegacyProdAttemptSources,
    LegacyProdEvidenceField,
    LegacySourceSnapshot,
    ProdLegacyEvidenceClass,
    ProdLegacyJobDisposition,
    build_legacy_prod_attempt_envelope,
    legacy_recovery_prerequisite_satisfied,
    reconcile_legacy_prod_same_attempt,
    write_legacy_prod_reconciliation_audit,
)
from gflow_cli.v2_runtime.phase_g_prod_reconciliation import (
    ProdAttemptIdentity,
    ProdReconciliationEvidence,
    ProdReconciliationResult,
    classify_prod_reconciliation,
)
from gflow_cli.v2_runtime.production_execution import ProductionCreditGuard
from scripts.operations import reconcile_legacy_prod_attempt as legacy_operator

_SCENE_JOB_ID = "SJB_900001"
_PRODUCTION_JOB_ID = "PRD_900001"
_SCENE_ID = "SCN_900001"
_PROMPT_ID = "SPV_900007"
_PROFILE_ID = "GPR_9002"
_ACCOUNT_ID = "PVA_9002"
_D2_RUN_ID = (
    "D2_" + hashlib.sha256(f"PRODUCTION_SCENE_D2_V1:{_SCENE_JOB_ID}:1".encode()).hexdigest()[:32]
)
_PROMPT_HASH = "e" * 64
_PROJECT_ID = "00000000-0000-4000-8000-000000000002"
_OBSERVED_AT = "2026-10-25T12:57:21Z"
_PROD_LEDGER_PATH = "/var/lib/gflow/production/phase-g/d2-credit-ledger.json"
_SOURCE_NAMES = (
    "PRODUCTION_JOBS",
    "SCENE_JOBS",
    "SCENES",
    "SCENE_PROMPT_VERSION",
    "GENERATION_PROFILE_MASTER",
    "PROVIDER_ACCOUNT_MASTER",
    "D2_LEDGER",
    "CREDIT_LEDGER",
)


def _sources() -> LegacyProdAttemptSources:
    return LegacyProdAttemptSources(
        production_jobs=(
            {
                "Production_Job_ID": _PRODUCTION_JOB_ID,
                "Execution_Environment": "PROD",
                "Job_Status": "RUNNING",
                "Created_At": "2026-10-25T11:04:31Z",
                "Updated_At": "2026-10-25T11:04:34Z",
            },
        ),
        scene_jobs=(
            {
                "Scene_Job_ID": _SCENE_JOB_ID,
                "Production_Job_ID": _PRODUCTION_JOB_ID,
                "Scene_Prompt_ID": _PROMPT_ID,
                "Attempt_No": 1,
                "Job_Status": "RUNNING",
                "Provider_Execution_ID": "",
                "Created_At": "2026-10-25T11:04:31Z",
                "Updated_At": "2026-10-25T11:04:34Z",
            },
        ),
        scenes=({"Scene_ID": _SCENE_ID},),
        scene_prompt_versions=(
            {
                "Scene_Prompt_ID": _PROMPT_ID,
                "Scene_ID": _SCENE_ID,
                "Generation_Profile_ID": _PROFILE_ID,
                "Prompt_SHA256": _PROMPT_HASH,
                "Prompt_Status": "APPROVED",
            },
        ),
        generation_profiles=(
            {
                "Generation_Profile_ID": _PROFILE_ID,
                "Environment": "PROD",
                "Provider_Account_ID": _ACCOUNT_ID,
                "Status": "ACTIVE",
            },
        ),
        provider_accounts=(
            {
                "Provider_Account_ID": _ACCOUNT_ID,
                "Environment": "PROD",
                "Provider_Project_ID": _PROJECT_ID,
                "Status": "ACTIVE",
            },
        ),
        d2_attempts=(
            {
                "d2_run_id": _D2_RUN_ID,
                "scene_id": _SCENE_ID,
                "scene_prompt_id": _PROMPT_ID,
                "generation_profile_id": _PROFILE_ID,
                "provider_account_id": _ACCOUNT_ID,
                "provider_project_id": _PROJECT_ID,
                "prompt_sha256": _PROMPT_HASH,
                "submission_state": "UNKNOWN_SUBMISSION_STATE",
                "generate_action_count": 1,
                "approval_action_count": 0,
                "baseline_captured": True,
                "baseline_pending_count": 0,
                "baseline_execution_ids": [],
                "baseline_media_ids": [],
                "baseline_confirmation_ids": [],
                "provider_submit_request_count": 0,
                "provider_execution_id": None,
                "provider_media_id": None,
                "provider_media_ids": [],
                "confirmation_id": None,
                "observed_confirmation_id": None,
                "updated_at": "2026-10-25T11:05:09Z",
            },
        ),
        credit_reservations=(
            {
                "attempt_id": _D2_RUN_ID,
                "environment": "PROD",
                "provider_account_id": _ACCOUNT_ID,
                "provider_project_id": _PROJECT_ID,
                "state": "UNKNOWN_SUBMISSION_STATE",
                "reserved_credits": "50",
            },
        ),
        source_snapshots={
            name: LegacySourceSnapshot(
                source=name,
                sha256=("a" if index % 2 == 0 else "b") * 64,
                observed_at=_OBSERVED_AT,
            )
            for index, name in enumerate(_SOURCE_NAMES)
        },
        runtime_namespace="PROD",
        d2_ledger_path=_PROD_LEDGER_PATH,
    )


def _operator_snapshot(sources: LegacyProdAttemptSources) -> GoogleSheetsSnapshot:
    return GoogleSheetsSnapshot(
        spreadsheet_id=APPROVED_SPREADSHEET_ID,
        read_at=_OBSERVED_AT,
        source=SOURCE_LIVE_GOOGLE_SHEETS,
        tables={
            "PRODUCTION_JOBS": [dict(row) for row in sources.production_jobs],
            "SCENE_JOBS": [dict(row) for row in sources.scene_jobs],
            "SCENES": [dict(row) for row in sources.scenes],
            "SCENE_PROMPT_VERSION": [dict(row) for row in sources.scene_prompt_versions],
            "GENERATION_PROFILE_MASTER": [dict(row) for row in sources.generation_profiles],
            "PROVIDER_ACCOUNT_MASTER": [dict(row) for row in sources.provider_accounts],
        },
    )


def _write_operator_ledgers(runtime_root, sources: LegacyProdAttemptSources) -> tuple[Path, Path]:
    d2_path = runtime_root / "production" / "phase-g" / "d2-credit-ledger.json"
    d2_path.parent.mkdir(parents=True)
    d2_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "daily_budget": 50,
                "attempts": [dict(row) for row in sources.d2_attempts],
                "test_sessions": [],
            }
        ),
        encoding="utf-8",
    )
    credit_path = runtime_root / "production-credit" / "shared-ledger.json"
    credit_path.parent.mkdir(parents=True)
    credit_path.write_text(
        json.dumps(
            {"schema_version": 1, "attempts": [dict(row) for row in sources.credit_reservations]}
        ),
        encoding="utf-8",
    )
    return d2_path, credit_path


def test_supported_operator_builds_sources_and_classifies_without_report_or_mutation(
    tmp_path: Path,
) -> None:
    sources = _sources()
    d2_path, credit_path = _write_operator_ledgers(tmp_path, sources)
    before = (d2_path.read_bytes(), credit_path.read_bytes())

    decision = legacy_operator.run_legacy_reconciliation(
        _operator_snapshot(sources),
        tmp_path,
        _D2_RUN_ID,
        observed_at=_OBSERVED_AT,
        software_commit="a" * 40,
    )

    assert decision.result is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    assert decision.envelope.recovery_prerequisite_satisfied
    assert "operator_report" not in decision.envelope.as_dict()
    assert decision.audit_event["historical_event_time"] is None
    assert decision.audit_event["reconciliation_observed_at"] == _OBSERVED_AT
    assert decision.audit_event["mutation_performed"] is False
    assert decision.audit_event["reservation_action"] == "UNCHANGED"
    assert (d2_path.read_bytes(), credit_path.read_bytes()) == before
    assert not (tmp_path / "phase-g" / "prod-legacy-reconciliation-audit.json").exists()


@pytest.mark.parametrize(
    ("table_name", "changed_rows"),
    [
        ("PRODUCTION_JOBS", []),
        ("PRODUCTION_JOBS", [{"Production_Job_ID": _PRODUCTION_JOB_ID}] * 2),
        ("SCENE_JOBS", []),
        ("SCENE_JOBS", [{"Scene_Job_ID": _SCENE_JOB_ID}] * 2),
        (
            "SCENE_PROMPT_VERSION",
            [
                {
                    "Scene_Prompt_ID": _PROMPT_ID,
                    "Scene_ID": _SCENE_ID,
                    "Generation_Profile_ID": _PROFILE_ID,
                    "Prompt_SHA256": "0" * 64,
                    "Prompt_Status": "APPROVED",
                }
            ],
        ),
        (
            "PROVIDER_ACCOUNT_MASTER",
            [
                {
                    "Provider_Account_ID": _ACCOUNT_ID,
                    "Environment": "PROD",
                    "Provider_Project_ID": "00000000-0000-4000-8000-000000000099",
                }
            ],
        ),
        (
            "PROVIDER_ACCOUNT_MASTER",
            [
                {
                    "Provider_Account_ID": "PVA_9999",
                    "Environment": "PROD",
                    "Provider_Project_ID": _PROJECT_ID,
                }
            ],
        ),
        (
            "GENERATION_PROFILE_MASTER",
            [
                {
                    "Generation_Profile_ID": _PROFILE_ID,
                    "Environment": "TEST",
                    "Provider_Account_ID": _ACCOUNT_ID,
                }
            ],
        ),
    ],
)
def test_supported_operator_fails_closed_before_classification_on_bad_sheet_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, table_name: str, changed_rows: list[dict]
) -> None:
    sources = _sources()
    _write_operator_ledgers(tmp_path, sources)
    tables = dict(_operator_snapshot(sources).tables)
    tables[table_name] = changed_rows
    snapshot = GoogleSheetsSnapshot(
        APPROVED_SPREADSHEET_ID, _OBSERVED_AT, SOURCE_LIVE_GOOGLE_SHEETS, tables
    )

    def classifier_must_not_run(*args, **kwargs):
        raise AssertionError("classifier must not run with an invalid legacy prerequisite")

    monkeypatch.setattr(
        legacy_operator, "reconcile_legacy_prod_same_attempt", classifier_must_not_run
    )
    with pytest.raises(legacy_operator.LegacyProdOperatorError):
        legacy_operator.run_legacy_reconciliation(
            snapshot,
            tmp_path,
            _D2_RUN_ID,
            observed_at=_OBSERVED_AT,
            software_commit="a" * 40,
        )


def test_supported_operator_requires_live_validated_snapshot_and_provenance(
    tmp_path: Path,
) -> None:
    sources = _sources()
    _write_operator_ledgers(tmp_path, sources)
    good = _operator_snapshot(sources)
    bad = GoogleSheetsSnapshot(good.spreadsheet_id, good.read_at, "COPIED_CHAT", good.tables)

    with pytest.raises(legacy_operator.LegacyProdOperatorError):
        legacy_operator.run_legacy_reconciliation(
            bad,
            tmp_path,
            _D2_RUN_ID,
            observed_at=_OBSERVED_AT,
            software_commit="a" * 40,
        )


def test_supported_operator_rejects_missing_canonical_table(tmp_path: Path) -> None:
    sources = _sources()
    _write_operator_ledgers(tmp_path, sources)
    good = _operator_snapshot(sources)
    tables = dict(good.tables)
    tables.pop("SCENES")
    snapshot = GoogleSheetsSnapshot(good.spreadsheet_id, good.read_at, good.source, tables)

    with pytest.raises(legacy_operator.LegacyProdOperatorError):
        legacy_operator.run_legacy_reconciliation(
            snapshot,
            tmp_path,
            _D2_RUN_ID,
            observed_at=_OBSERVED_AT,
            software_commit="a" * 40,
        )


def test_supported_operator_cli_accepts_only_attempt_and_optional_audit_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from types import SimpleNamespace

    sources = _sources()
    _write_operator_ledgers(tmp_path, sources)
    monkeypatch.setenv("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", str(tmp_path / "creds.json"))
    monkeypatch.setattr(
        legacy_operator,
        "load_runtime_settings",
        lambda: SimpleNamespace(environment="production", runtime_state_root=tmp_path),
    )
    monkeypatch.setattr(
        legacy_operator, "_read_live_snapshot", lambda *_: _operator_snapshot(sources)
    )
    monkeypatch.setattr(legacy_operator, "_software_commit", lambda *_: "a" * 40)
    monkeypatch.setattr(
        sys,
        "argv",
        ["reconcile_legacy_prod_attempt.py", "--d2-run-id", _D2_RUN_ID],
    )

    assert legacy_operator.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "CLASSIFIED"
    assert result["result"] == "AMBIGUOUS_AFTER_RECONCILIATION"
    assert result["operator_report_for_legacy"] == "NOT_APPLICABLE"
    assert result["mutation_performed"] is False
    assert result["audit_persisted"] is False


def test_supported_operator_has_no_generation_approval_or_credit_release_api() -> None:
    source = Path(legacy_operator.__file__).read_text(encoding="utf-8")

    assert "FlowGenerationPort" not in source
    assert "approve_confirmation" not in source
    assert "ProductionCreditGuard" not in source
    assert "create_attempt" not in source


def test_supported_operator_audit_is_optional_and_does_not_release_credit(
    tmp_path: Path,
) -> None:
    sources = _sources()
    d2_path, credit_path = _write_operator_ledgers(tmp_path, sources)
    before = (d2_path.read_bytes(), credit_path.read_bytes())

    decision = legacy_operator.run_legacy_reconciliation(
        _operator_snapshot(sources),
        tmp_path,
        _D2_RUN_ID,
        observed_at=_OBSERVED_AT,
        software_commit="a" * 40,
        persist_audit=True,
    )

    assert decision.audit_event["reservation_action"] == "UNCHANGED"
    assert (d2_path.read_bytes(), credit_path.read_bytes()) == before
    audit_path = tmp_path / "phase-g" / "prod-legacy-reconciliation-audit.json"
    assert audit_path.is_file()
    assert _D2_RUN_ID in audit_path.read_text(encoding="utf-8")


def test_exact_legacy_join_builds_identity_with_field_provenance() -> None:
    envelope = build_legacy_prod_attempt_envelope(_sources(), _D2_RUN_ID)

    assert envelope.identity is not None
    assert envelope.identity.production_job_id == _PRODUCTION_JOB_ID
    assert envelope.identity.scene_job_id == _SCENE_JOB_ID
    assert envelope.identity.d2_run_id == _D2_RUN_ID
    assert envelope.identity.scene_id == _SCENE_ID
    assert envelope.identity.scene_prompt_id == _PROMPT_ID
    assert envelope.identity.generation_profile_id == _PROFILE_ID
    assert envelope.identity.provider_account_id == _ACCOUNT_ID
    assert envelope.identity.provider_project_id == _PROJECT_ID
    assert envelope.field("production_job_id").provenance_class is (
        ProdLegacyEvidenceClass.DERIVED_CANONICAL_JOIN
    )
    assert envelope.field("attempt_id").provenance_class is (
        ProdLegacyEvidenceClass.PERSISTED_DIRECT
    )
    assert envelope.identity_status == "IDENTITY_VALIDATED"


def test_valid_legacy_envelope_satisfies_prerequisite_without_operator_report() -> None:
    envelope = build_legacy_prod_attempt_envelope(_sources(), _D2_RUN_ID)

    assert legacy_recovery_prerequisite_satisfied(envelope)
    assert "operator_report" not in envelope.as_dict()


def test_ambiguous_legacy_envelope_does_not_satisfy_prerequisite() -> None:
    sources = _sources().with_rows("scene_jobs", ())
    envelope = build_legacy_prod_attempt_envelope(sources, _D2_RUN_ID)

    assert not legacy_recovery_prerequisite_satisfied(envelope)


def test_missing_identity_provenance_does_not_satisfy_prerequisite() -> None:
    envelope = build_legacy_prod_attempt_envelope(_sources(), _D2_RUN_ID)
    fields = dict(envelope.fields)
    original = fields["provider_account_id"]
    fields["provider_account_id"] = LegacyProdEvidenceField(
        original.value, original.provenance_class, ()
    )
    envelope = replace(envelope, fields=tuple(fields.items()))

    assert not legacy_recovery_prerequisite_satisfied(envelope)


def test_missing_prompt_hash_does_not_satisfy_prerequisite() -> None:
    attempt = dict(_sources().d2_attempts[0])
    attempt.pop("prompt_sha256")
    sources = _sources().with_rows("d2_attempts", (attempt,))
    envelope = build_legacy_prod_attempt_envelope(sources, _D2_RUN_ID)

    assert not envelope.recovery_prerequisite_satisfied


def test_missing_source_snapshot_does_not_satisfy_prerequisite() -> None:
    sources = _sources()
    snapshots = dict(sources.source_snapshots)
    snapshots.pop("CREDIT_LEDGER")
    envelope = build_legacy_prod_attempt_envelope(
        sources.with_values(source_snapshots=snapshots), _D2_RUN_ID
    )

    assert not envelope.recovery_prerequisite_satisfied


@pytest.mark.parametrize(
    ("source", "field", "value"),
    [
        ("scene_prompt_versions", "Prompt_SHA256", "0" * 64),
        ("provider_accounts", "Provider_Project_ID", "00000000-0000-0000-0000-000000000000"),
        ("provider_accounts", "Provider_Account_ID", "PVA_9003"),
    ],
)
def test_conflicting_legacy_identity_does_not_satisfy_prerequisite(
    source: str, field: str, value: str
) -> None:
    sources = _sources()
    rows = list(getattr(sources, source))
    rows[0][field] = value
    envelope = build_legacy_prod_attempt_envelope(
        sources.with_rows(source, tuple(rows)), _D2_RUN_ID
    )

    assert not legacy_recovery_prerequisite_satisfied(envelope)


def test_envelope_is_immutable_and_detached_from_source_rows() -> None:
    sources = _sources()
    envelope = build_legacy_prod_attempt_envelope(sources, _D2_RUN_ID)

    sources.d2_attempts[0]["baseline_execution_ids"].append("later-edit")  # type: ignore[union-attr]

    assert envelope.field("baseline_execution_ids").value == ()
    assert envelope.attempt_record["baseline_execution_ids"] == ()
    with pytest.raises(TypeError):
        envelope.attempt_record["submission_state"] = "GENERATION_SUCCEEDED"  # type: ignore[index]


@pytest.mark.parametrize("missing_field", ["production_job_id", "scene_job_id"])
def test_missing_job_linkage_is_derived_without_backfilling_d2_row(missing_field: str) -> None:
    sources = _sources()
    original_attempt = deepcopy(sources.d2_attempts[0])

    envelope = build_legacy_prod_attempt_envelope(sources, _D2_RUN_ID)

    assert envelope.identity is not None
    assert missing_field not in sources.d2_attempts[0]
    assert sources.d2_attempts[0] == original_attempt


def test_prod_environment_is_derived_from_agreeing_canonical_sources() -> None:
    envelope = build_legacy_prod_attempt_envelope(_sources(), _D2_RUN_ID)

    environment = envelope.field("execution_environment")
    assert environment.value == "PROD"
    assert environment.provenance_class is ProdLegacyEvidenceClass.DERIVED_CANONICAL_JOIN
    assert len(environment.sources) >= 4


@pytest.mark.parametrize(
    ("source", "field", "value"),
    [
        ("production_jobs", "Execution_Environment", "TEST"),
        ("generation_profiles", "Environment", "TEST"),
        ("provider_accounts", "Environment", "TEST"),
        ("credit_reservations", "environment", "TEST"),
    ],
)
def test_conflicting_environment_fails_closed(source: str, field: str, value: str) -> None:
    sources = _sources()
    rows = list(getattr(sources, source))
    rows[0][field] = value
    sources = sources.with_rows(source, tuple(rows))

    envelope = build_legacy_prod_attempt_envelope(sources, _D2_RUN_ID)

    assert envelope.identity is None
    assert envelope.identity_status == "LEGACY_IDENTITY_AMBIGUOUS"


def test_non_production_runtime_path_fails_closed() -> None:
    sources = _sources().with_values(
        d2_ledger_path="/var/lib/gflow/test/phase-g/d2-credit-ledger.json"
    )

    envelope = build_legacy_prod_attempt_envelope(sources, _D2_RUN_ID)

    assert envelope.identity is None
    assert envelope.identity_status == "LEGACY_IDENTITY_AMBIGUOUS"


def test_missing_generate_action_at_is_unavailable_and_never_derived() -> None:
    sources = _sources()
    attempt = dict(sources.d2_attempts[0])
    attempt.pop("generate_action_at", None)
    sources = sources.with_rows("d2_attempts", (attempt,))

    envelope = build_legacy_prod_attempt_envelope(sources, _D2_RUN_ID)

    action_time = envelope.field("generate_action_at")
    assert action_time.value is None
    assert action_time.provenance_class is ProdLegacyEvidenceClass.UNAVAILABLE
    assert "updated_at" not in action_time.sources[0].record_key


def test_explicit_generate_action_at_keeps_its_persisted_provenance() -> None:
    sources = _sources()
    attempt = dict(sources.d2_attempts[0])
    attempt["generate_action_at"] = "2026-10-25T11:05:00Z"
    sources = sources.with_rows("d2_attempts", (attempt,))

    envelope = build_legacy_prod_attempt_envelope(sources, _D2_RUN_ID)

    assert envelope.field("generate_action_at").value == "2026-10-25T11:05:00Z"
    assert envelope.field("generate_action_at").provenance_class is (
        ProdLegacyEvidenceClass.PERSISTED_DIRECT
    )


@pytest.mark.parametrize(
    ("source", "field", "value"),
    [
        ("scene_prompt_versions", "Prompt_SHA256", "0" * 64),
        ("provider_accounts", "Provider_Project_ID", "00000000-0000-0000-0000-000000000000"),
        ("provider_accounts", "Provider_Account_ID", "PVA_0003"),
        ("generation_profiles", "Provider_Account_ID", "PVA_0003"),
    ],
)
def test_conflicting_prompt_profile_account_or_project_fails_closed(
    source: str, field: str, value: str
) -> None:
    sources = _sources()
    rows = list(getattr(sources, source))
    rows[0][field] = value

    envelope = build_legacy_prod_attempt_envelope(
        sources.with_rows(source, tuple(rows)), _D2_RUN_ID
    )

    assert envelope.identity is None
    assert envelope.identity_status == "LEGACY_IDENTITY_AMBIGUOUS"


@pytest.mark.parametrize("source", ["scene_jobs", "production_jobs"])
def test_duplicate_canonical_job_candidate_fails_closed(source: str) -> None:
    sources = _sources()
    rows = getattr(sources, source)
    sources = sources.with_rows(source, rows + (dict(rows[0]),))

    envelope = build_legacy_prod_attempt_envelope(sources, _D2_RUN_ID)

    assert envelope.identity is None
    assert envelope.identity_status == "LEGACY_IDENTITY_AMBIGUOUS"


def test_credit_reservation_must_match_exact_legacy_attempt() -> None:
    sources = _sources()
    reservation = dict(sources.credit_reservations[0])
    reservation["provider_project_id"] = "wrong-project"
    sources = sources.with_rows("credit_reservations", (reservation,))

    envelope = build_legacy_prod_attempt_envelope(sources, _D2_RUN_ID)

    assert envelope.identity is None
    assert envelope.identity_status == "LEGACY_IDENTITY_AMBIGUOUS"


def test_duplicate_credit_reservation_fails_closed() -> None:
    sources = _sources()
    reservations = sources.credit_reservations
    sources = sources.with_rows("credit_reservations", reservations + (dict(reservations[0]),))

    envelope = build_legacy_prod_attempt_envelope(sources, _D2_RUN_ID)

    assert envelope.identity is None
    assert envelope.identity_status == "LEGACY_IDENTITY_AMBIGUOUS"


def test_legacy_running_without_execution_proof_is_reported_as_premature() -> None:
    decision = reconcile_legacy_prod_same_attempt(
        _sources(), _D2_RUN_ID, observed_at=_OBSERVED_AT, software_commit="a" * 40
    )

    assert decision.job_disposition is ProdLegacyJobDisposition.LEGACY_PREMATURE_RUNNING
    assert decision.audit_event["mutation_performed"] is False
    assert decision.audit_event["reservation_action"] == "UNCHANGED"


def test_current_legacy_fixture_is_ambiguous_and_keeps_reservation() -> None:
    sources = _sources()
    d2_before = deepcopy(sources.d2_attempts[0])
    credit_before = deepcopy(sources.credit_reservations[0])
    decision = reconcile_legacy_prod_same_attempt(
        sources, _D2_RUN_ID, observed_at=_OBSERVED_AT, software_commit="a" * 40
    )

    assert decision.envelope.identity is not None
    assert decision.envelope.recovery_prerequisite_satisfied
    assert decision.result is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    assert decision.audit_event["reservation_action"] == "UNCHANGED"
    assert decision.envelope.field("credit_reservation_state").value == ("UNKNOWN_SUBMISSION_STATE")
    assert decision.envelope.field("reserved_credits").value == "50"
    assert sources.d2_attempts[0] == d2_before
    assert sources.credit_reservations[0] == credit_before
    assert sources.d2_attempts[0]["generate_action_count"] == 1
    assert sources.d2_attempts[0]["approval_action_count"] == 0


def test_legacy_missing_provider_evidence_never_proves_non_submission() -> None:
    decision = reconcile_legacy_prod_same_attempt(
        _sources(), _D2_RUN_ID, observed_at=_OBSERVED_AT, software_commit="a" * 40
    )

    assert decision.result is not ProdReconciliationResult.PROVEN_NOT_SUBMITTED


def test_legacy_timestamp_free_refusal_does_not_prove_non_submission() -> None:
    sources = _sources()
    attempt = dict(sources.d2_attempts[0])
    attempt.update(
        {
            "provider_submit_rpc_observed": True,
            "provider_submit_rpc_id": "YhhmEf",
            "provider_submit_http_method": "POST",
            "provider_submit_observed_at": "2026-10-25T11:05:02Z",
            "provider_submit_request_count": 1,
            "provider_submit_dispatch_window_completed": True,
            "provider_submit_project_id": _PROJECT_ID,
            "provider_submit_response_observed": True,
            "provider_submit_response_status": 403,
            "provider_submit_generation_record_observed": False,
            "provider_submit_refusal_detected": True,
            "provider_submit_refusal_code": "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
            "provider_submit_request_failed": False,
        }
    )
    sources = sources.with_rows("d2_attempts", (attempt,))

    decision = reconcile_legacy_prod_same_attempt(
        sources, _D2_RUN_ID, observed_at=_OBSERVED_AT, software_commit="a" * 40
    )

    assert decision.result is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    assert decision.audit_event["reservation_action"] == "UNCHANGED"


def test_exact_attempt_bound_execution_can_be_classified_without_action_timestamp() -> None:
    sources = _sources()
    attempt = dict(sources.d2_attempts[0])
    attempt.update(
        submission_state="GENERATION_SUCCEEDED",
        provider_execution_id="exec_exact_1",
    )
    sources = sources.with_rows("d2_attempts", (attempt,))

    decision = reconcile_legacy_prod_same_attempt(
        sources, _D2_RUN_ID, observed_at=_OBSERVED_AT, software_commit="a" * 40
    )

    assert decision.result is ProdReconciliationResult.SUBMISSION_PROVEN_EXECUTION_FOUND
    assert decision.envelope.field("generate_action_at").value is None


def test_execution_identity_present_in_baseline_is_rejected() -> None:
    sources = _sources()
    attempt = dict(sources.d2_attempts[0])
    attempt.update(
        submission_state="GENERATION_PENDING",
        provider_execution_id="exec_old_1",
        baseline_execution_ids=["exec_old_1"],
    )
    sources = sources.with_rows("d2_attempts", (attempt,))

    decision = reconcile_legacy_prod_same_attempt(
        sources, _D2_RUN_ID, observed_at=_OBSERVED_AT, software_commit="a" * 40
    )

    assert decision.result is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION


def test_modern_positive_refusal_rule_remains_available_only_for_complete_modern_row() -> None:
    identity = ProdAttemptIdentity(
        production_job_id=_PRODUCTION_JOB_ID,
        scene_job_id=_SCENE_JOB_ID,
        d2_run_id=_D2_RUN_ID,
        provider_account_id=_ACCOUNT_ID,
        provider_project_id=_PROJECT_ID,
        scene_id=_SCENE_ID,
        scene_prompt_id=_PROMPT_ID,
        generation_profile_id=_PROFILE_ID,
        prompt_sha256=_PROMPT_HASH,
    )
    attempt = {
        "d2_run_id": _D2_RUN_ID,
        "production_job_id": _PRODUCTION_JOB_ID,
        "scene_job_id": _SCENE_JOB_ID,
        "execution_environment": "PROD",
        "provider_account_id": _ACCOUNT_ID,
        "provider_project_id": _PROJECT_ID,
        "scene_id": _SCENE_ID,
        "scene_prompt_id": _PROMPT_ID,
        "generation_profile_id": _PROFILE_ID,
        "prompt_sha256": _PROMPT_HASH,
        "generate_action_count": 1,
        "generate_action_at": "2026-10-25T11:05:00Z",
        "baseline_captured": True,
        "baseline_execution_ids": [],
        "baseline_media_ids": [],
        "baseline_confirmation_ids": [],
        "provider_submit_rpc_observed": True,
        "provider_submit_rpc_id": "YhhmEf",
        "provider_submit_http_method": "POST",
        "provider_submit_observed_at": "2026-10-25T11:05:01Z",
        "provider_submit_request_count": 1,
        "provider_submit_dispatch_window_completed": True,
        "provider_submit_project_id": _PROJECT_ID,
        "provider_submit_response_observed": True,
        "provider_submit_response_status": 403,
        "provider_submit_generation_record_observed": False,
        "provider_submit_refusal_detected": True,
        "provider_submit_refusal_code": "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        "provider_submit_request_failed": False,
        "provider_execution_id": None,
        "provider_media_id": None,
        "provider_media_ids": [],
        "confirmation_id": None,
        "observed_confirmation_id": None,
        "submission_state": "UNKNOWN_SUBMISSION_STATE",
    }
    evidence = ProdReconciliationEvidence(
        identity=identity,
        observed_at="2026-10-25T11:05:02Z",
        provider_submit_request_count=1,
        provider_submit_dispatch_window_completed=True,
        provider_submit_project_id=_PROJECT_ID,
        provider_submit_response_observed=True,
        provider_submit_response_status=403,
        provider_submit_generation_record_observed=False,
        provider_submit_refusal_code="PUBLIC_ERROR_UNUSUAL_ACTIVITY",
        provider_submit_request_failed=False,
    )

    result = classify_prod_reconciliation(identity, evidence, persisted_attempt=attempt)

    assert result is ProdReconciliationResult.PROVEN_NOT_SUBMITTED


def test_legacy_audit_is_separate_idempotent_and_keeps_provenance(tmp_path) -> None:
    sources = _sources()
    before = deepcopy(sources.d2_attempts[0])
    decision = reconcile_legacy_prod_same_attempt(
        sources, _D2_RUN_ID, observed_at=_OBSERVED_AT, software_commit="a" * 40
    )

    first = write_legacy_prod_reconciliation_audit(tmp_path, decision.audit_event)
    second = write_legacy_prod_reconciliation_audit(tmp_path, decision.audit_event)

    assert first == second
    assert first["contract_version"] == LEGACY_PROD_CONTRACT_VERSION
    assert first["envelope_sha256"] == decision.envelope.sha256
    assert first["evidence_provenance"]
    assert first["historical_event_time"] is None
    assert first["reconciliation_observed_at"] == _OBSERVED_AT
    assert first["software_commit"] == "a" * 40
    assert sources.d2_attempts[0] == before
    assert not (tmp_path / "phase-g" / "d2-credit-ledger.json").exists()
    assert not (tmp_path / "production-credit" / "shared-ledger.json").exists()


def test_positive_legacy_release_is_exact_and_idempotent(tmp_path) -> None:
    sources = _sources()
    attempt = dict(sources.d2_attempts[0])
    attempt.update(
        {
            "generate_action_at": "2026-10-25T11:05:00Z",
            "provider_submit_rpc_observed": True,
            "provider_submit_rpc_id": "YhhmEf",
            "provider_submit_http_method": "POST",
            "provider_submit_observed_at": "2026-10-25T11:05:01Z",
            "provider_submit_request_count": 1,
            "provider_submit_dispatch_window_completed": True,
            "provider_submit_project_id": _PROJECT_ID,
            "provider_submit_response_observed": True,
            "provider_submit_response_status": 403,
            "provider_submit_generation_record_observed": False,
            "provider_submit_refusal_detected": True,
            "provider_submit_refusal_code": "PUBLIC_ERROR_UNUSUAL_ACTIVITY",
            "provider_submit_request_failed": False,
        }
    )
    sources = sources.with_rows("d2_attempts", (attempt,))
    decision = reconcile_legacy_prod_same_attempt(
        sources, _D2_RUN_ID, observed_at=_OBSERVED_AT, software_commit="a" * 40
    )
    assert decision.result is ProdReconciliationResult.PROVEN_NOT_SUBMITTED

    runtime_root = tmp_path / "runtime"
    d2_path = runtime_root / "phase-g" / "d2-credit-ledger.json"
    d2_path.parent.mkdir(parents=True)
    d2_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "daily_budget": 50,
                "attempts": [attempt],
                "test_sessions": [],
            }
        ),
        encoding="utf-8",
    )
    d2_bytes_before = d2_path.read_bytes()
    write_legacy_prod_reconciliation_audit(runtime_root, decision.audit_event)

    credit_path = runtime_root / "production-credit" / "shared-ledger.json"
    credit_path.parent.mkdir(parents=True)
    credit_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "attempts": [
                    {
                        "attempt_id": _D2_RUN_ID,
                        "environment": "PROD",
                        "provider_id": "PRV_9001",
                        "provider_account_id": _ACCOUNT_ID,
                        "provider_project_id": _PROJECT_ID,
                        "date": "2026-10-25",
                        "state": "UNKNOWN_SUBMISSION_STATE",
                        "generate_click_count": 1,
                        "billing_outcome": "UNKNOWN",
                        "quoted_credits": None,
                        "reserved_credits": "50",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    guard = ProductionCreditGuard(runtime_root)
    arguments = {
        "production_job_id": _PRODUCTION_JOB_ID,
        "scene_job_id": _SCENE_JOB_ID,
        "provider_account_id": _ACCOUNT_ID,
        "provider_project_id": _PROJECT_ID,
        "classification_event_sha256": str(decision.audit_event["event_sha256"]),
        "released_at": _OBSERVED_AT,
        "software_commit": "a" * 40,
    }

    released = guard.release_legacy_proven_not_submitted(_D2_RUN_ID, **arguments)
    repeated = guard.release_legacy_proven_not_submitted(_D2_RUN_ID, **arguments)

    assert released["reserved_credits"] == "0"
    assert released["state"] == "WAF_REJECTION"
    assert released["billing_outcome"] == "UNKNOWN"
    assert repeated == released
    assert guard._read()["attempts"][0] == released  # noqa: SLF001 - verifies persisted idempotency
    assert d2_path.read_bytes() == d2_bytes_before
    audit = json.loads(
        (runtime_root / "phase-g" / "prod-legacy-reconciliation-audit.json").read_text(
            encoding="utf-8"
        )
    )
    release_events = [
        event for event in audit["events"] if event.get("event_type") == "RESERVATION_RELEASE"
    ]
    assert len(release_events) == 1
    assert release_events[0]["mutation_performed"] is True
    assert release_events[0]["reservation_action"] == "RELEASED"
    assert (
        release_events[0]["classification_event_sha256"] == arguments["classification_event_sha256"]
    )


def test_modern_and_frozen_test_contract_constants_are_unchanged() -> None:
    assert LEGACY_PROD_CONTRACT_VERSION == "D2_PROD_LEGACY_ATTEMPT_RECOVERY_V1"
    assert ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION.value == (
        "AMBIGUOUS_AFTER_RECONCILIATION"
    )
