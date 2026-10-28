from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from threading import Barrier

import pytest

from gflow_cli.v2_runtime.ambiguous_legacy_recovery_policy import (
    AUDIT_NAME as POLICY_AUDIT_NAME,
)
from gflow_cli.v2_runtime.ambiguous_legacy_recovery_policy import (
    ambiguous_legacy_policy_id,
    append_policy_event,
    build_policy_record,
)
from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    SOURCE_LIVE_GOOGLE_SHEETS,
    GoogleSheetsSnapshot,
)
from gflow_cli.v2_runtime.legacy_replacement import (
    LegacyReplacementAuthorizationRequest,
    ReplacementGenerationConfig,
    ReplacementQuoteStatus,
    consume_legacy_replacement_authorization,
    create_legacy_replacement_authorization,
    evaluate_replacement_decision,
    quote_generation_reservation,
)
from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    D2RetryAuthorization,
    FlowSubmissionBaseline,
    GoogleFlowAgentOnlyGenerationExecutor,
    PhaseGError,
    PhaseGExecutionRequest,
)
from gflow_cli.v2_runtime.phase_g_prod_legacy_reconciliation import (
    LEGACY_PROD_CONTRACT_VERSION,
    write_legacy_prod_reconciliation_audit,
)
from gflow_cli.v2_runtime.production_execution import (
    CreditBudgetContext,
    ProductionCreditGuard,
    ProductionExecutionContext,
)
from tests.validation.test_phase_g import request_for

_PARENT = "D2_13c62112d9881710019049d95cb6da57"
_PRODUCTION = "PRD_000001"
_SCENE_JOB = "SJB_000001"
_POLICY = ambiguous_legacy_policy_id(_PARENT)
_OBSERVED_AT = datetime(2026, 10, 26, tzinfo=UTC)


def _reconciliation_event() -> dict[str, object]:
    event: dict[str, object] = {
        "contract_version": LEGACY_PROD_CONTRACT_VERSION,
        "legacy_attempt_id": _PARENT,
        "result": "AMBIGUOUS_AFTER_RECONCILIATION",
        "reservation_action": "UNCHANGED",
        "evidence_provenance": {
            "attempt_id": {"value": _PARENT},
            "production_job_id": {"value": _PRODUCTION},
            "scene_job_id": {"value": _SCENE_JOB},
        },
    }
    event["event_sha256"] = hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    return event


_RECONCILIATION = str(_reconciliation_event()["event_sha256"])


def _config(**updates: object) -> ReplacementGenerationConfig:
    values: dict[str, object] = {
        "provider_id": "PRV_0001",
        "provider_account_id": "PVA_0001",
        "model_id": "MDL_0001",
        "generation_profile_id": "GPR_0001",
        "generation_mode": "TEXT_TO_VIDEO",
        "pricing_variant": "STANDARD_LITE",
        "account_pricing_class": "NON_ULTRA",
        "duration_sec": 8,
        "aspect_ratio": "9:16",
        "resolution": None,
        "audio_mode": None,
        "output_count": 1,
        "quality_tier": None,
    }
    values.update(updates)
    return ReplacementGenerationConfig(**values)  # type: ignore[arg-type]


def _profile() -> dict[str, object]:
    return {
        "Generation_Profile_ID": "GPR_0001",
        "Provider_Account_ID": "PVA_0001",
        "Model_ID": "MDL_0001",
        "Generation_Mode": "TEXT_TO_VIDEO",
        "Environment": "PROD",
        "Status": "ACTIVE",
        "Default_Duration_Sec": 8,
        "Default_Aspect_Ratio": "9:16",
        "Default_Resolution": "720p",
        "Default_Output_Count": 1,
        "Audio_Mode": "GENERATED",
        "Quality_Tier": "STANDARD",
    }


def _profile_for_account(
    profile_id: str, account_id: str, *, aspect_ratio: str = "9:16"
) -> dict[str, object]:
    row = _profile()
    row.update(
        Generation_Profile_ID=profile_id,
        Provider_Account_ID=account_id,
        Default_Aspect_Ratio=aspect_ratio,
    )
    return row


def _account(account_id: str) -> dict[str, object]:
    return {
        "Provider_Account_ID": account_id,
        "Provider_ID": "PRV_0001",
        "Environment": "PROD",
        "Provider_Project_ID": "aafe9282-071f-4333-baa4-92b9f76a9c27",
        "Status": "ACTIVE",
    }


def _model() -> dict[str, object]:
    return {
        "Model_ID": "MDL_0001",
        "Provider_ID": "PRV_0001",
        "Model_Name": "Veo 3.1 Lite",
        "Status": "ACTIVE",
    }


def _cost_row(**updates: object) -> dict[str, object]:
    row: dict[str, object] = {
        "Provider_Cost_ID": "PCT_000001",
        "Provider_ID": "PRV_0001",
        "Provider_Account_ID": "PVA_0001",
        "Model_ID": "MDL_0001",
        "Billing_Plan_Name": "NON_ULTRA",
        "Generation_Mode": "TEXT_TO_VIDEO",
        "Quality_Tier": "",
        "Resolution": "",
        "Cost_Unit": "CREDIT",
        "Cost_Rate": "10",
        "Currency": "",
        "Effective_From": "2026-01-01T00:00:00Z",
        "Effective_To": "",
        "Source": "https://support.google.com/flow/answer/16526234?hl=en",
        "Verified_At": "2026-01-01T00:00:00Z",
        "Status": "ACTIVE",
        "Created_At": "2026-01-01T00:00:00Z",
        "Updated_At": "2026-01-01T00:00:00Z",
    }
    row.update(updates)
    return row


def _quote(
    rows: list[dict[str, object]] | None = None,
    *,
    config: ReplacementGenerationConfig | None = None,
):
    return quote_generation_reservation(
        config or _config(),
        snapshot=_snapshot(cost_rows=rows or []),
        quoted_at=_OBSERVED_AT,
    )


def _snapshot(
    *,
    profile: dict[str, object] | None = None,
    cost_rows: list[dict[str, object]] | None = None,
    spreadsheet_id: str = APPROVED_SPREADSHEET_ID,
    source: str = SOURCE_LIVE_GOOGLE_SHEETS,
) -> GoogleSheetsSnapshot:
    return GoogleSheetsSnapshot(
        spreadsheet_id=spreadsheet_id,
        read_at="2026-10-26T00:00:00Z",
        source=source,
        tables={
            "PROVIDER_ACCOUNT_MASTER": [_account("PVA_0001"), _account("PVA_0002")],
            "MODEL_MASTER": [_model()],
            "GENERATION_PROFILE_MASTER": [
                profile or _profile(),
                _profile_for_account("GPR_0002", "PVA_0002"),
            ],
            "PROVIDER_COST_MASTER": cost_rows or [],
        },
    )


def _seed_parent(
    ledger: D2DailyCreditLedger,
    tmp_path: Path,
    *,
    write_audits: bool = True,
) -> PhaseGExecutionRequest:
    request = PhaseGExecutionRequest.from_presubmit(request_for(tmp_path), _PARENT)
    ledger.create_attempt(request)
    ledger.capture_baseline(
        _PARENT,
        FlowSubmissionBaseline(
            request.provider_project_id,
            frozenset(),
            frozenset(),
            0,
            frozenset(),
            "pre-replacement-baseline",
        ),
    )
    ledger.mark_generate_action(_PARENT)
    ledger.record_state(
        _PARENT,
        submission_state="UNKNOWN_SUBMISSION_STATE",
        accounted_credit_cost=50,
    )
    if write_audits:
        _write_audits(ledger)
    return request


def _write_audits(ledger: D2DailyCreditLedger) -> None:
    event = _reconciliation_event()
    write_legacy_prod_reconciliation_audit(ledger.root.parent.parent, event)
    policy = build_policy_record(
        attempt_id=_PARENT,
        production_job_id=_PRODUCTION,
        scene_job_id=_SCENE_JOB,
        reservation_id=_PARENT,
        reconciliation_audit_sha256=_RECONCILIATION,
        observed_at="2026-10-26T00:00:00Z",
    )
    policy_path = ledger.root / POLICY_AUDIT_NAME
    append_policy_event(policy_path, event_type="POLICY_INTENT", policy=policy)
    append_policy_event(policy_path, event_type="POLICY_APPLIED", policy=policy)


def _authorization_request(quote, **updates: object) -> LegacyReplacementAuthorizationRequest:
    values: dict[str, object] = {
        "parent_attempt_id": _PARENT,
        "production_job_id": _PRODUCTION,
        "scene_job_id": _SCENE_JOB,
        "replacement_production_job_id": "PRD_000002",
        "replacement_scene_job_id": "SJB_000002",
        "policy_id": _POLICY,
        "reconciliation_audit_id": _RECONCILIATION,
        "decision": evaluate_replacement_decision(
            gate_a_pass=True,
            gate_b_pass=True,
            quote=quote,
            existing_unresolved_exposure=Decimal("50"),
            replacement_authorization_ready=True,
            parent_linkage_ready=True,
            parent_quarantined=True,
            zero_existing_replacements=True,
            zero_conflicting_authorizations=True,
            evidence_capture_ready=True,
        ),
        "legacy_exposure": Decimal("50"),
        "created_at": "2026-10-26T00:00:00Z",
        "created_by": "controlled-test-fixture",
        "software_commit": "b" * 40,
    }
    values.update(updates)
    return LegacyReplacementAuthorizationRequest(**values)  # type: ignore[arg-type]


def _approved_ledger(tmp_path: Path) -> tuple[D2DailyCreditLedger, dict[str, object]]:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    parent = _seed_parent(ledger, tmp_path)
    quote = _quote([_cost_row()])
    authorization = create_legacy_replacement_authorization(ledger, _authorization_request(quote))
    return ledger, {"parent": parent, "quote": quote, "authorization": authorization}


def _replacement_request(
    tmp_path: Path,
    parent: PhaseGExecutionRequest,
    run_id: str = "D2_test-replacement-0001",
    *,
    authorization: dict[str, object] | None = None,
) -> PhaseGExecutionRequest:
    context = None
    if authorization is not None:
        context = ProductionExecutionContext(
            environment="PROD",
            provider_id=parent.provider_id,
            provider_account_id=parent.provider_account_id,
            provider_project_id=parent.provider_project_id,
            auth_profile="GFLOW_PROD_AUTH",
            scene_id=parent.scene_id,
            generation_profile_id=parent.generation_profile_id,
            scene_prompt_id=parent.scene_prompt_id,
            attempt_id=run_id,
            credit_budget_context=CreditBudgetContext(provider_remaining_free=Decimal("50")),
            production_job_id=str(authorization["replacement_production_job_id"]),
            scene_job_id=str(authorization["replacement_scene_job_id"]),
            legacy_replacement_authorization_id=str(authorization["authorization_id"]),
            legacy_replacement_parent_attempt_id=str(authorization["parent_attempt_id"]),
            legacy_replacement_quote_credits=Decimal(str(authorization["replacement_quote"])),
            legacy_replacement_quote_input_digest=str(
                authorization["replacement_quote_input_digest"]
            ),
            legacy_replacement_quote_source_version=str(
                authorization["replacement_quote_source_version"]
            ),
        )
    return PhaseGExecutionRequest.from_presubmit(
        parent.presubmit_request, run_id, execution_context=context
    )


def test_valid_source_verified_standard_lite_cost_returns_quote() -> None:
    quote = _quote([_cost_row()])

    assert quote.status is ReplacementQuoteStatus.VALID
    assert quote.amount == Decimal("10")
    assert quote.unit == "CREDIT"
    assert quote.source == "https://support.google.com/flow/answer/16526234?hl=en"
    assert quote.source_version
    assert quote.input_digest
    assert quote.quoted_at == _OBSERVED_AT.isoformat()
    assert quote.valid


def test_missing_cost_returns_unavailable_without_a_numeric_fallback() -> None:
    quote = _quote()

    assert quote.status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE
    assert quote.amount is None
    assert not quote.valid


def test_historical_accounted_cost_is_not_an_input_to_quote() -> None:
    quote = _quote()

    assert quote.status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE
    assert quote.amount is None


@pytest.mark.parametrize(
    "updates",
    [
        {"Provider_ID": "PRV_9999"},
        {"Model_ID": "MDL_9999"},
        {"Provider_Account_ID": "PVA_9999"},
        {"Generation_Mode": "IMAGE_TO_VIDEO"},
        {"Billing_Plan_Name": "ULTRA"},
        {"Status": "HOLD"},
        {"Cost_Unit": "USD"},
        {"Verified_At": ""},
        {"Source": ""},
    ],
)
def test_nonmatching_or_unverified_cost_rows_are_unavailable(updates: dict[str, object]) -> None:
    assert _quote([_cost_row(**updates)]).status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE


def test_wrong_generation_profile_is_rejected() -> None:
    profile = _profile()
    profile["Generation_Profile_ID"] = "GPR_9999"
    quote = quote_generation_reservation(
        _config(),
        snapshot=_snapshot(profile=profile, cost_rows=[_cost_row()]),
        quoted_at=_OBSERVED_AT,
    )

    assert quote.status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE


def test_ambiguous_cost_rows_are_unavailable() -> None:
    assert _quote([_cost_row(), _cost_row(Provider_Cost_ID="PCT_000002")]).status is (
        ReplacementQuoteStatus.QUOTE_UNAVAILABLE
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        _snapshot(cost_rows=[_cost_row()], spreadsheet_id="unapproved-sheet"),
        _snapshot(cost_rows=[_cost_row()], source="LOCAL_FIXTURE"),
        _snapshot(cost_rows=[_cost_row()], profile={"Generation_Profile_ID": "GPR_0001"}),
    ],
)
def test_quote_requires_canonical_live_snapshot_and_exact_profile(snapshot) -> None:
    quote = quote_generation_reservation(_config(), snapshot=snapshot, quoted_at=_OBSERVED_AT)

    assert quote.status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE


def test_missing_account_pricing_class_is_unavailable() -> None:
    row = _cost_row()
    row.pop("Billing_Plan_Name")

    quote = _quote([row])

    assert quote.status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE
    assert quote.amount is None


def test_quote_rejects_snapshot_not_current_at_quote_time() -> None:
    snapshot = _snapshot(cost_rows=[_cost_row()])
    stale = GoogleSheetsSnapshot(
        spreadsheet_id=snapshot.spreadsheet_id,
        read_at="2026-10-25T23:59:59Z",
        source=snapshot.source,
        tables=snapshot.tables,
    )

    quote = quote_generation_reservation(_config(), snapshot=stale, quoted_at=_OBSERVED_AT)

    assert quote.status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE


def test_expired_and_future_cost_rows_are_unavailable() -> None:
    expired = _cost_row(Effective_To="2026-10-25T23:59:59Z")
    future = _cost_row(Effective_From="2026-10-27T00:00:00Z")

    assert _quote([expired]).status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE
    assert _quote([future]).status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE


@pytest.mark.parametrize("updates", [{"Resolution": "1080p"}, {"Quality_Tier": "HIGH"}])
def test_non_pricing_cost_dimensions_do_not_change_rate_match(updates: dict[str, object]) -> None:
    quote = _quote([_cost_row(**updates)])

    assert quote.status is ReplacementQuoteStatus.VALID
    assert quote.amount == Decimal("10")


def test_standard_lite_non_ultra_quote_uses_live_account_and_eighteen_columns() -> None:
    config = _config(provider_account_id="PVA_0002", generation_profile_id="GPR_0002")
    row = _cost_row(Provider_Account_ID="PVA_0002")

    quote = _quote([row], config=config)

    assert len(row) == 18
    assert quote.status is ReplacementQuoteStatus.VALID
    assert quote.amount == Decimal("10")


def test_output_count_multiplies_the_per_generation_rate() -> None:
    config = _config(output_count=3)

    quote = _quote([_cost_row()], config=config)

    assert quote.status is ReplacementQuoteStatus.VALID
    assert quote.amount == Decimal("30")


def test_non_pricing_runtime_dimensions_do_not_create_false_mismatch() -> None:
    config = _config(
        duration_sec=8,
        aspect_ratio="9:16",
        resolution=None,
        audio_mode=None,
        quality_tier=None,
    )
    profile = _profile()
    profile["Default_Aspect_Ratio"] = "16:9"
    snapshot = _snapshot(profile=profile, cost_rows=[_cost_row()])

    quote = quote_generation_reservation(config, snapshot=snapshot, quoted_at=_OBSERVED_AT)

    assert quote.status is ReplacementQuoteStatus.VALID
    assert quote.amount == Decimal("10")


@pytest.mark.parametrize(
    "config_updates",
    [
        {"provider_id": "PRV_9999"},
        {"provider_account_id": "PVA_9999"},
        {"model_id": "MDL_9999"},
        {"generation_mode": "IMAGE_TO_VIDEO"},
        {"account_pricing_class": "ULTRA"},
        {"pricing_variant": "LOWER_PRIORITY_LITE"},
    ],
)
def test_wrong_pricing_identity_is_rejected(config_updates: dict[str, object]) -> None:
    assert _quote([_cost_row()], config=_config(**config_updates)).status is (
        ReplacementQuoteStatus.QUOTE_UNAVAILABLE
    )


@pytest.mark.parametrize(
    "source",
    [
        "https://pricing.example.test/veo-lite",
        "https://support.google.com.evil.test/flow/answer/16526234",
        "http://support.google.com/flow/answer/16526234",
    ],
)
def test_non_official_pricing_source_is_rejected(source: str) -> None:
    assert _quote([_cost_row(Source=source)]).status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE


@pytest.mark.parametrize(
    "table,row_updates",
    [
        ("PROVIDER_ACCOUNT_MASTER", {"Provider_ID": "PRV_9999"}),
        ("PROVIDER_ACCOUNT_MASTER", {"Status": "HOLD"}),
        ("MODEL_MASTER", {"Model_Name": "Veo 3.1 - Lite (Lower Priority)"}),
        ("MODEL_MASTER", {"Status": "HOLD"}),
    ],
)
def test_quote_requires_unique_active_account_and_standard_lite_model(
    table: str, row_updates: dict[str, object]
) -> None:
    snapshot = _snapshot(cost_rows=[_cost_row()])
    tables = {name: list(rows) for name, rows in snapshot.tables.items()}
    target_id = "PVA_0001" if table == "PROVIDER_ACCOUNT_MASTER" else "MDL_0001"
    identity_key = "Provider_Account_ID" if table == "PROVIDER_ACCOUNT_MASTER" else "Model_ID"
    rows = tables[table]
    index = next(i for i, row in enumerate(rows) if row.get(identity_key) == target_id)
    rows[index] = {**rows[index], **row_updates}
    invalid = GoogleSheetsSnapshot(
        snapshot.spreadsheet_id, snapshot.read_at, snapshot.source, tables
    )

    result = quote_generation_reservation(_config(), snapshot=invalid, quoted_at=_OBSERVED_AT)
    assert result.status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE


def test_quote_does_not_create_an_attempt_reservation_or_generate_action(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    before = ledger.path.read_bytes() if ledger.path.exists() else None

    quote = _quote([_cost_row()])

    after = ledger.path.read_bytes() if ledger.path.exists() else None
    assert quote.valid
    assert before == after is None


def test_decision_holds_when_quote_is_unavailable() -> None:
    decision = evaluate_replacement_decision(
        gate_a_pass=True,
        gate_b_pass=True,
        quote=_quote(),
        existing_unresolved_exposure=Decimal("50"),
        replacement_authorization_ready=True,
        parent_linkage_ready=True,
        parent_quarantined=True,
        zero_existing_replacements=True,
        zero_conflicting_authorizations=True,
        evidence_capture_ready=True,
    )

    assert decision.status == "HOLD"
    assert decision.maximum_combined_exposure is None


def test_valid_quote_calculates_combined_exposure_without_authorizing() -> None:
    decision = evaluate_replacement_decision(
        gate_a_pass=True,
        gate_b_pass=True,
        quote=_quote([_cost_row()]),
        existing_unresolved_exposure=Decimal("50"),
        replacement_authorization_ready=True,
        parent_linkage_ready=True,
        parent_quarantined=True,
        zero_existing_replacements=True,
        zero_conflicting_authorizations=True,
        evidence_capture_ready=True,
    )

    assert decision.status == "APPROVE"
    assert decision.maximum_combined_exposure == Decimal("60")
    assert decision.authorization_created is False


@pytest.mark.parametrize(
    "failed_gate",
    [
        "gate_a_pass",
        "gate_b_pass",
        "replacement_authorization_ready",
        "parent_linkage_ready",
        "parent_quarantined",
        "zero_existing_replacements",
        "zero_conflicting_authorizations",
        "evidence_capture_ready",
    ],
)
def test_decision_holds_when_any_required_gate_fails(failed_gate: str) -> None:
    arguments: dict[str, object] = {
        "gate_a_pass": True,
        "gate_b_pass": True,
        "quote": _quote([_cost_row()]),
        "existing_unresolved_exposure": Decimal("50"),
        "replacement_authorization_ready": True,
        "parent_linkage_ready": True,
        "parent_quarantined": True,
        "zero_existing_replacements": True,
        "zero_conflicting_authorizations": True,
        "evidence_capture_ready": True,
    }
    arguments[failed_gate] = False

    assert evaluate_replacement_decision(**arguments).status == "HOLD"  # type: ignore[arg-type]


def test_authorization_creation_is_atomic_durable_and_not_generate_authority(
    tmp_path: Path,
) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    _seed_parent(ledger, tmp_path)
    request = _authorization_request(_quote([_cost_row()]))

    record = create_legacy_replacement_authorization(ledger, request)

    assert record["parent_attempt_id"] == _PARENT
    assert record["authorized_replacement_count"] == 1
    assert record["replacement_created"] is False
    assert record["authorization_status"] == "APPROVED"
    assert record["generate_authorized"] is False
    assert ledger._read()["legacy_replacement_authorizations"] == [record]  # noqa: SLF001


def test_authorization_requires_valid_quote_and_approved_decision(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    _seed_parent(ledger, tmp_path)

    with pytest.raises(PhaseGError):
        create_legacy_replacement_authorization(ledger, _authorization_request(_quote()))


@pytest.mark.parametrize(
    "field,value",
    [
        ("parent_attempt_id", "D2_wrong-parent-000000000000"),
        ("production_job_id", "PRD_999999"),
        ("scene_job_id", "SJB_999999"),
        ("policy_id", ""),
        ("reconciliation_audit_id", ""),
        ("created_by", ""),
        ("software_commit", "not-a-commit"),
    ],
)
def test_authorization_rejects_wrong_or_missing_identity_fields(
    tmp_path: Path, field: str, value: str
) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    _seed_parent(ledger, tmp_path)
    request = _authorization_request(_quote([_cost_row()]), **{field: value})

    with pytest.raises(PhaseGError):
        create_legacy_replacement_authorization(ledger, request)


def test_duplicate_active_authorization_for_parent_is_rejected(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    _seed_parent(ledger, tmp_path)
    request = _authorization_request(_quote([_cost_row()]))
    create_legacy_replacement_authorization(ledger, request)

    with pytest.raises(PhaseGError):
        create_legacy_replacement_authorization(ledger, request)


def test_authorization_captures_bounded_exposure_and_decision_digest(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    _seed_parent(ledger, tmp_path)
    request = _authorization_request(_quote([_cost_row()]))

    record = create_legacy_replacement_authorization(ledger, request)

    assert record["legacy_exposure"] == "50"
    assert record["replacement_quote"] == "10"
    assert record["maximum_combined_exposure"] == "60"
    assert record["decision_input_digest"]
    assert record["reconciliation_audit_id"] == _RECONCILIATION


def test_authorization_creation_requires_quarantined_parent(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    _seed_parent(ledger, tmp_path)
    request = _authorization_request(
        _quote([_cost_row()]),
        decision=evaluate_replacement_decision(
            gate_a_pass=True,
            gate_b_pass=True,
            quote=_quote([_cost_row()]),
            existing_unresolved_exposure=Decimal("50"),
            replacement_authorization_ready=True,
            parent_linkage_ready=True,
            parent_quarantined=False,
            zero_existing_replacements=True,
            zero_conflicting_authorizations=True,
            evidence_capture_ready=True,
        ),
    )

    with pytest.raises(PhaseGError):
        create_legacy_replacement_authorization(ledger, request)


def test_missing_policy_audit_rejects_authorization(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    _seed_parent(ledger, tmp_path, write_audits=False)

    with pytest.raises(PhaseGError):
        create_legacy_replacement_authorization(
            ledger, _authorization_request(_quote([_cost_row()]))
        )


def test_missing_reconciliation_audit_rejects_authorization(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    _seed_parent(ledger, tmp_path)
    (ledger.root.parent.parent / "phase-g" / "prod-legacy-reconciliation-audit.json").unlink()

    with pytest.raises(PhaseGError):
        create_legacy_replacement_authorization(
            ledger, _authorization_request(_quote([_cost_row()]))
        )


def test_authorization_concurrent_creation_allows_only_one(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    _seed_parent(ledger, tmp_path)
    request = _authorization_request(_quote([_cost_row()]))
    barrier = Barrier(2)

    def create() -> str:
        barrier.wait()
        try:
            create_legacy_replacement_authorization(ledger, request)
            return "created"
        except PhaseGError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))

    assert sorted(results) == ["created", "rejected"]


def test_missing_parent_and_changed_reservation_reject_authorization(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    request = _authorization_request(_quote([_cost_row()]))

    with pytest.raises(PhaseGError):
        create_legacy_replacement_authorization(ledger, request)

    _seed_parent(ledger, tmp_path)
    ledger.record_state(_PARENT, submission_state="GENERATION_FAILED", accounted_credit_cost=50)
    with pytest.raises(PhaseGError):
        create_legacy_replacement_authorization(ledger, request)


def test_replacement_creation_is_not_available_through_ordinary_create_attempt(
    tmp_path: Path,
) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    parent = _seed_parent(ledger, tmp_path)
    replacement = PhaseGExecutionRequest.from_presubmit(
        parent.presubmit_request, "D2_test-replacement-0001"
    )

    with pytest.raises(PhaseGError):
        ledger.create_attempt(replacement)


def test_consumption_links_exactly_one_replacement_and_never_authorizes_generate(
    tmp_path: Path,
) -> None:
    ledger, state = _approved_ledger(tmp_path)
    parent = state["parent"]
    quote = state["quote"]
    authorization = state["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    replacement = _replacement_request(tmp_path, parent, authorization=authorization)

    created = consume_legacy_replacement_authorization(
        ledger,
        str(authorization["authorization_id"]),
        replacement,
        parent_quarantined=True,
        current_quote=quote,
    )

    lineage = created["legacy_replacement"]
    assert isinstance(lineage, dict)
    assert lineage["parent_attempt_id"] == _PARENT
    assert lineage["authorization_id"] == authorization["authorization_id"]
    assert lineage["generate_authorized"] is False
    assert created["generate_action_count"] == 0
    assert created["approval_action_count"] == 0
    saved_auth = ledger._read()["legacy_replacement_authorizations"][0]  # noqa: SLF001
    assert saved_auth["authorization_status"] == "CONSUMED"
    assert saved_auth["replacement_created"] is True
    assert saved_auth["replacement_attempt_id"] == replacement.d2_run_id
    assert ledger.validate_consumed_legacy_replacement(replacement) == created


def test_consumption_requires_exact_child_job_pair_and_replacement_capability(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    ledger, state = _approved_ledger(tmp_path)
    parent, quote, authorization = state["parent"], state["quote"], state["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    auth_id = str(authorization["authorization_id"])
    ordinary_request = _replacement_request(tmp_path, parent)
    with pytest.raises(PhaseGError, match="V2"):
        consume_legacy_replacement_authorization(
            ledger,
            auth_id,
            ordinary_request,
            parent_quarantined=True,
            current_quote=quote,
        )

    bound_request = _replacement_request(tmp_path, parent, authorization=authorization)
    assert bound_request.execution_context is not None
    wrong_context = replace(bound_request.execution_context, scene_job_id="SJB_000003")
    wrong_request = replace(bound_request, execution_context=wrong_context)
    with pytest.raises(PhaseGError):
        consume_legacy_replacement_authorization(
            ledger,
            auth_id,
            wrong_request,
            parent_quarantined=True,
            current_quote=quote,
        )
    assert len(ledger.attempts()) == 1


def test_phase_g_checks_consumed_capability_before_any_ui_mutation(tmp_path: Path) -> None:
    ledger, state = _approved_ledger(tmp_path)
    parent, authorization = state["parent"], state["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    request = _replacement_request(tmp_path, parent, authorization=authorization)

    class PortSpy:
        bind_called = False

        async def bind_request(self, _request: PhaseGExecutionRequest) -> None:
            self.bind_called = True

    port = PortSpy()
    executor = GoogleFlowAgentOnlyGenerationExecutor(
        port,  # type: ignore[arg-type]
        ledger,
        object(),  # type: ignore[arg-type]
        production_credit_guard=ProductionCreditGuard(tmp_path / "runtime"),
    )
    pending = executor.execute(request)
    try:
        with pytest.raises(PhaseGError, match="not unique"):
            pending.send(None)
    finally:
        pending.close()

    assert port.bind_called is False


def test_consumption_replay_with_same_id_is_idempotent(tmp_path: Path) -> None:
    ledger, state = _approved_ledger(tmp_path)
    parent, quote, authorization = state["parent"], state["quote"], state["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    replacement = _replacement_request(tmp_path, parent, authorization=authorization)
    auth_id = str(authorization["authorization_id"])

    first = consume_legacy_replacement_authorization(
        ledger, auth_id, replacement, parent_quarantined=True, current_quote=quote
    )
    replay = consume_legacy_replacement_authorization(
        ledger, auth_id, replacement, parent_quarantined=True, current_quote=quote
    )

    assert replay == first
    assert len(ledger.attempts()) == 2


def test_consumption_replay_rejects_tampered_consumption_audit(tmp_path: Path) -> None:
    ledger, state = _approved_ledger(tmp_path)
    parent, quote, authorization = state["parent"], state["quote"], state["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    replacement = _replacement_request(tmp_path, parent, authorization=authorization)
    auth_id = str(authorization["authorization_id"])
    consume_legacy_replacement_authorization(
        ledger, auth_id, replacement, parent_quarantined=True, current_quote=quote
    )

    payload = ledger._read()  # noqa: SLF001
    saved = payload["legacy_replacement_authorizations"][0]
    assert isinstance(saved, dict)
    saved["consumed_at"] = "2026-10-27T00:00:00Z"
    ledger._write(payload)  # noqa: SLF001

    with pytest.raises(PhaseGError):
        consume_legacy_replacement_authorization(
            ledger, auth_id, replacement, parent_quarantined=True, current_quote=quote
        )


def test_concurrent_consumption_creates_only_one_linked_replacement(tmp_path: Path) -> None:
    ledger, state = _approved_ledger(tmp_path)
    parent, quote, authorization = state["parent"], state["quote"], state["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    request = _replacement_request(tmp_path, parent, authorization=authorization)
    auth_id = str(authorization["authorization_id"])
    barrier = Barrier(2)

    def consume() -> str:
        barrier.wait()
        try:
            attempt = consume_legacy_replacement_authorization(
                ledger, auth_id, request, parent_quarantined=True, current_quote=quote
            )
            return str(attempt["d2_run_id"])
        except PhaseGError:
            return "REJECTED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: consume(), range(2)))

    assert results == [request.d2_run_id, request.d2_run_id]
    assert len(ledger.attempts()) == 2


def test_consumption_rejects_second_or_different_replacement_id(tmp_path: Path) -> None:
    ledger, state = _approved_ledger(tmp_path)
    parent, quote, authorization = state["parent"], state["quote"], state["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    auth_id = str(authorization["authorization_id"])
    consume_legacy_replacement_authorization(
        ledger,
        auth_id,
        _replacement_request(tmp_path, parent, authorization=authorization),
        parent_quarantined=True,
        current_quote=quote,
    )

    with pytest.raises(PhaseGError):
        consume_legacy_replacement_authorization(
            ledger,
            auth_id,
            _replacement_request(
                tmp_path,
                parent,
                "D2_test-replacement-0002",
                authorization=authorization,
            ),
            parent_quarantined=True,
            current_quote=quote,
        )
    assert len(ledger.attempts()) == 2


def test_consumption_requires_quarantined_parent_and_unchanged_quote(tmp_path: Path) -> None:
    ledger, state = _approved_ledger(tmp_path)
    parent, quote, authorization = state["parent"], state["quote"], state["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    replacement = _replacement_request(tmp_path, parent, authorization=authorization)
    auth_id = str(authorization["authorization_id"])

    with pytest.raises(PhaseGError):
        consume_legacy_replacement_authorization(
            ledger, auth_id, replacement, parent_quarantined=False, current_quote=quote
        )
    with pytest.raises(PhaseGError):
        consume_legacy_replacement_authorization(
            ledger, auth_id, replacement, parent_quarantined=True, current_quote=_quote()
        )
    assert len(ledger.attempts()) == 1


def test_consumption_revalidates_durable_policy_and_reconciliation_audits(
    tmp_path: Path,
) -> None:
    ledger, state = _approved_ledger(tmp_path)
    parent, quote, authorization = state["parent"], state["quote"], state["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    (ledger.root.parent.parent / "phase-g" / "prod-legacy-reconciliation-audit.json").unlink()

    with pytest.raises(PhaseGError):
        consume_legacy_replacement_authorization(
            ledger,
            str(authorization["authorization_id"]),
            _replacement_request(tmp_path, parent, authorization=authorization),
            parent_quarantined=True,
            current_quote=quote,
        )
    assert len(ledger.attempts()) == 1


def test_consumption_rejects_changed_parent_reservation(tmp_path: Path) -> None:
    ledger, state = _approved_ledger(tmp_path)
    parent, quote, authorization = state["parent"], state["quote"], state["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    ledger.record_state(
        _PARENT,
        submission_state="UNKNOWN_SUBMISSION_STATE",
        accounted_credit_cost=49,
    )

    with pytest.raises(PhaseGError):
        consume_legacy_replacement_authorization(
            ledger,
            str(authorization["authorization_id"]),
            _replacement_request(tmp_path, parent, authorization=authorization),
            parent_quarantined=True,
            current_quote=quote,
        )
    assert len(ledger.attempts()) == 1


def test_consumption_rejects_replacement_with_wrong_parent_configuration(tmp_path: Path) -> None:
    from dataclasses import replace

    ledger, state = _approved_ledger(tmp_path)
    parent, quote, authorization = state["parent"], state["quote"], state["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    wrong = replace(
        _replacement_request(tmp_path, parent, authorization=authorization),
        provider_account_id="PVA_9999",
    )

    with pytest.raises(PhaseGError):
        consume_legacy_replacement_authorization(
            ledger,
            str(authorization["authorization_id"]),
            wrong,
            parent_quarantined=True,
            current_quote=quote,
        )
    assert len(ledger.attempts()) == 1


def test_ordinary_retry_authorization_cannot_bypass_ambiguous_parent(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    parent = _seed_parent(ledger, tmp_path)
    retry = _replacement_request(tmp_path, parent)

    with pytest.raises(PhaseGError):
        ledger.create_authorized_retry_attempt(
            retry, D2RetryAuthorization(_PARENT, "UNEXPECTED_MULTI_OUTPUT")
        )


def test_replacement_capability_has_no_provider_generate_or_approval_call() -> None:
    source = Path("src/gflow_cli/v2_runtime/legacy_replacement.py").read_text(encoding="utf-8")

    assert "FlowGenerationPort" not in source
    assert "approve_confirmation" not in source
    assert "click_generate_once" not in source
    assert "create_attempt(" not in source


def test_legacy_d2_reader_accepts_schema_three_without_mutating_it(tmp_path: Path) -> None:
    from scripts.operations.reconcile_legacy_prod_attempt import _read_ledger

    path = tmp_path / "d2.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "daily_budget": 50,
                "attempts": [],
                "test_sessions": [],
                "legacy_replacement_authorizations": [],
            }
        ),
        encoding="utf-8",
    )

    rows, _, _ = _read_ledger(path, kind="D2")

    assert rows == ()


def test_frozen_test_retry_authorization_contract_remains_separate() -> None:
    from gflow_cli.v2_runtime.phase_g import D2RetryAuthorization

    assert D2RetryAuthorization("D2_test-parent-000001", "UNEXPECTED_MULTI_OUTPUT")
