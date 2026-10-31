from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from test_phase_g import (
    FakeGenerationPort,
    _capture_ledger_baseline,
    _observe_ledger_confirmation,
    phase_g_request,
)

from gflow_cli.v2_runtime.agent_recovery import AgentSettingsRecoveryJournal
from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    D6PrerequisiteProof,
    GoogleFlowAgentOnlyGenerationExecutor,
    PhaseGError,
    PhaseGExecutionRequest,
    _d6_semantic_fingerprint,
    evaluate_d6_release,
)
from gflow_cli.v2_runtime.phase_g_artifacts import sha256_file

_D2_ID = "D2_test-run-0001"
_D6_ID = "D6_test-run-0001"


def _d6_request(root: Path, *, prerequisite_id: str | None = _D2_ID) -> PhaseGExecutionRequest:
    return PhaseGExecutionRequest.from_presubmit(
        phase_g_request(root).presubmit_request,
        _D6_ID,
        prerequisite_d2_run_id=prerequisite_id,
    )


def _set_attempt_state(
    ledger: D2DailyCreditLedger,
    run_id: str,
    *,
    state: str,
    terminal_state: str | None = None,
) -> None:
    def update(payload: dict[str, object]) -> None:
        attempts = payload["attempts"]
        assert isinstance(attempts, list)
        attempt = next(row for row in attempts if row["d2_run_id"] == run_id)
        attempt["submission_state"] = state
        attempt["terminal_state"] = terminal_state

    ledger._mutate(update)  # noqa: SLF001 - deterministic persisted-state fixture


def _seed_d2_state(
    root: Path, *, state: str, terminal_state: str | None = None
) -> D2DailyCreditLedger:
    ledger = D2DailyCreditLedger(root / "state")
    ledger.create_attempt(phase_g_request(root))
    _set_attempt_state(ledger, _D2_ID, state=state, terminal_state=terminal_state)
    return ledger


def _seed_success(root: Path) -> D2DailyCreditLedger:
    request = phase_g_request(root)
    ledger = D2DailyCreditLedger(request.presubmit_request.runtime_state_root)
    ledger.create_attempt(request)
    _capture_ledger_baseline(ledger)
    ledger.mark_generate_action(_D2_ID)
    _observe_ledger_confirmation(ledger, confirmation_id="new-confirmation")
    ledger.approve_gate(
        _D2_ID,
        video_count=1,
        credit_cost=Decimal("10"),
        confirmation_id="new-confirmation",
    )
    ledger.record_state(
        _D2_ID,
        submission_state="GENERATION_SUCCEEDED",
        terminal_state="GENERATION_SUCCEEDED",
        provider_media_id="media-verified",
        provider_project_id=request.provider_project_id,
    )
    local_file = request.presubmit_request.temp_root / _D2_ID / "clip.mp4"
    local_file.parent.mkdir(parents=True)
    local_file.write_bytes(b"validated-and-persisted-clip")
    digest = sha256_file(local_file)
    ledger.record_local_artifact_validated(
        _D2_ID,
        path=local_file,
        size_bytes=local_file.stat().st_size,
        sha256=digest,
    )
    remote_file = root / "remote-redownload.mp4"
    remote_file.write_bytes(local_file.read_bytes())
    ledger.mark_artifact_verified(
        _D2_ID,
        remote_download=remote_file,
        remote_file_id="drive-file-verified",
        remote_filename="verified.mp4",
    )
    assert ledger.attempts()[0]["submission_state"] == "D2_SUCCEEDED"
    return ledger


@pytest.mark.parametrize(
    ("state", "terminal_state"),
    [
        ("UNKNOWN_SUBMISSION_STATE", None),
        ("FORENSICALLY_CLOSED_UNKNOWN", "FORENSICALLY_CLOSED_UNKNOWN"),
        ("RECONCILED_NO_CHARGE", "RECONCILED_NO_CHARGE"),
        ("GENERATION_FAILED", "GENERATION_FAILED"),
        ("GENERATION_SUCCEEDED", "GENERATION_SUCCEEDED"),
        ("MEDIA_VALIDATION_FAILED", "MEDIA_VALIDATION_FAILED"),
        ("PERSISTENCE_FAILED", "PERSISTENCE_FAILED"),
        ("GENERATION_PENDING", None),
    ],
)
def test_d6_rejects_every_non_d2_succeeded_state(
    tmp_path: Path, state: str, terminal_state: str | None
) -> None:
    ledger = _seed_d2_state(tmp_path, state=state, terminal_state=terminal_state)
    result = evaluate_d6_release(
        prerequisite_d2_run_id=_D2_ID,
        d6_request=_d6_request(tmp_path),
        ledger=ledger,
    )
    assert result.eligible is False
    assert result.failure_code == "D6_PREREQUISITE_NOT_SUCCEEDED"


def test_d6_requires_an_explicit_exact_d2_prerequisite(tmp_path: Path) -> None:
    with pytest.raises(PhaseGError) as missing:
        _d6_request(tmp_path, prerequisite_id=None)
    assert missing.value.code == "D6_PREREQUISITE_REQUIRED"

    request = _d6_request(tmp_path)
    ledger = D2DailyCreditLedger(tmp_path / "state")
    result = evaluate_d6_release(prerequisite_d2_run_id=None, d6_request=request, ledger=ledger)
    assert result.eligible is False
    assert result.failure_code == "D6_PREREQUISITE_REQUIRED"


def test_d6_requires_phase_e_model_compatibility_fingerprint(tmp_path: Path) -> None:
    presubmit = phase_g_request(tmp_path).presubmit_request
    preflight = replace(
        presubmit.preflight,
        resolved={
            key: value
            for key, value in presubmit.preflight.resolved.items()
            if key != "model_compatibility_sha256"
        },
    )
    with pytest.raises(PhaseGError) as missing:
        PhaseGExecutionRequest.from_presubmit(
            replace(presubmit, preflight=preflight),
            _D6_ID,
            prerequisite_d2_run_id=_D2_ID,
        )
    assert missing.value.code == "D6_MODEL_COMPATIBILITY_REQUIRED"


def test_production_run_number_does_not_change_d2_d6_workload_fingerprint() -> None:
    workload = {
        "scene_id": "SCN_000001",
        "provider_id": "PRV_0001",
        "generation_mode": "TEXT_TO_VIDEO",
        "model_id": "MDL_0001",
        "model_compatibility_sha256": "a" * 64,
        "requested_duration_sec": 8,
        "requested_aspect_ratio": "9:16",
        "requested_output_count": 1,
        "production_run_no": 2,
    }

    first_run = _d6_semantic_fingerprint(workload)
    next_run = _d6_semantic_fingerprint({**workload, "production_run_no": 3})

    assert first_run == next_run


def test_d6_prerequisite_must_exist_in_authoritative_ledger(tmp_path: Path) -> None:
    request = _d6_request(tmp_path)
    ledger = D2DailyCreditLedger(tmp_path / "state")
    result = evaluate_d6_release(prerequisite_d2_run_id=_D2_ID, d6_request=request, ledger=ledger)
    assert result.eligible is False
    assert result.failure_code == "D6_PREREQUISITE_NOT_FOUND"


def test_d6_does_not_accept_success_from_a_non_authoritative_ledger(
    tmp_path: Path,
) -> None:
    unrelated_ledger = _seed_success(tmp_path / "unrelated")
    result = evaluate_d6_release(
        prerequisite_d2_run_id=_D2_ID,
        d6_request=_d6_request(tmp_path),
        ledger=unrelated_ledger,
    )
    assert result.eligible is False
    assert result.failure_code == "D6_PREREQUISITE_NOT_FOUND"


def test_d6_requires_remote_verified_artifact_evidence(tmp_path: Path) -> None:
    ledger = _seed_d2_state(tmp_path, state="D2_SUCCEEDED", terminal_state="D2_SUCCEEDED")
    result = evaluate_d6_release(
        prerequisite_d2_run_id=_D2_ID,
        d6_request=_d6_request(tmp_path),
        ledger=ledger,
    )
    assert result.eligible is False
    assert result.failure_code == "D6_PREREQUISITE_NOT_SUCCEEDED"


def test_compatible_d2_success_produces_immutable_proof_and_persists_relation(
    tmp_path: Path,
) -> None:
    d2_ledger = _seed_success(tmp_path)
    request = _d6_request(tmp_path)
    result = evaluate_d6_release(
        prerequisite_d2_run_id=_D2_ID, d6_request=request, ledger=d2_ledger
    )
    assert result.eligible is True
    assert result.binding_match is True
    assert result.prerequisite_state == "D2_SUCCEEDED"
    assert result.proof is not None

    d6_ledger = D2DailyCreditLedger(tmp_path / "production")
    created = d6_ledger.create_d6_attempt(request, proof=result.proof)
    assert created["prerequisite_d2_run_id"] == _D2_ID
    assert created["d6_prerequisite_proof"]["prerequisite_state"] == "D2_SUCCEEDED"
    with pytest.raises(TypeError):
        d6_ledger.record_state(
            _D6_ID,
            submission_state="GENERATION_PENDING",
            **{"prerequisite_d2_run_id": "D2_other-run-0001"},
        )
    assert d6_ledger.attempts()[0]["prerequisite_d2_run_id"] == _D2_ID


def test_d2_d6_binding_rejects_incompatible_success(tmp_path: Path) -> None:
    ledger = _seed_success(tmp_path)
    ledger._mutate(  # noqa: SLF001 - model a valid success for another workload
        lambda payload: payload["attempts"][0].update(requested_duration_sec=4)
    )
    result = evaluate_d6_release(
        prerequisite_d2_run_id=_D2_ID,
        d6_request=_d6_request(tmp_path),
        ledger=ledger,
    )
    assert result.eligible is False
    assert result.failure_code == "D6_PREREQUISITE_BINDING_MISMATCH"


def test_d6_binding_allows_lane_specific_account_project_and_profile(
    tmp_path: Path,
) -> None:
    ledger = _seed_success(tmp_path)
    d6_request = replace(
        _d6_request(tmp_path),
        provider_account_id="PVA_0002",
        provider_project_id="prod-project",
        generation_profile_id="GPR_0002",
    )
    result = evaluate_d6_release(
        prerequisite_d2_run_id=_D2_ID,
        d6_request=d6_request,
        ledger=ledger,
    )
    assert result.eligible is True
    assert result.binding_match is True


def test_legacy_success_without_release_contract_marker_is_ineligible(
    tmp_path: Path,
) -> None:
    ledger = _seed_success(tmp_path)
    ledger._mutate(  # noqa: SLF001 - represent a pre-contract historical row
        lambda payload: payload["attempts"][0].pop("d6_release_contract_version")
    )
    result = evaluate_d6_release(
        prerequisite_d2_run_id=_D2_ID,
        d6_request=_d6_request(tmp_path),
        ledger=ledger,
    )
    assert result.eligible is False
    assert result.failure_code == "D6_PREREQUISITE_NOT_SUCCEEDED"


def test_generic_d6_creation_is_blocked_and_d2_creation_is_unchanged(tmp_path: Path) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "state")
    d6_request = _d6_request(tmp_path)
    with pytest.raises(PhaseGError) as blocked:
        ledger.create_attempt(replace(d6_request, prerequisite_d2_run_id=None))
    assert blocked.value.code == "D6_PREREQUISITE_REQUIRED"

    with pytest.raises(PhaseGError) as dedicated:
        ledger.create_attempt(d6_request)
    assert dedicated.value.code == "D6_RELEASE_PATH_REQUIRED"

    d2_request = phase_g_request(tmp_path)
    assert ledger.create_attempt(d2_request)["d2_run_id"] == d2_request.d2_run_id


def test_forged_success_claim_cannot_bypass_authoritative_lookup(tmp_path: Path) -> None:
    ledger = _seed_d2_state(tmp_path, state="UNKNOWN_SUBMISSION_STATE")
    request = _d6_request(tmp_path)
    forged = D6PrerequisiteProof(
        prerequisite_d2_run_id=_D2_ID,
        prerequisite_state="D2_SUCCEEDED",
        d2_fingerprint_sha256="a" * 64,
        d6_fingerprint_sha256="a" * 64,
        prerequisite_ledger_sha256=sha256_file(ledger.path),
        contract_version=1,
        verified_at="2026-10-19T00:00:00Z",
    )
    with pytest.raises(PhaseGError) as denied:
        D2DailyCreditLedger(tmp_path / "production").create_d6_attempt(request, proof=forged)
    assert denied.value.code == "D6_PREREQUISITE_NOT_SUCCEEDED"


def test_prerequisite_change_after_read_only_evaluation_blocks_creation(tmp_path: Path) -> None:
    d2_ledger = _seed_success(tmp_path)
    request = _d6_request(tmp_path)
    result = evaluate_d6_release(
        prerequisite_d2_run_id=_D2_ID, d6_request=request, ledger=d2_ledger
    )
    assert result.proof is not None
    d2_ledger._mutate(  # noqa: SLF001 - simulate concurrent ledger update
        lambda payload: payload["attempts"][0].update(updated_at="2026-10-19T00:00:00Z")
    )
    with pytest.raises(PhaseGError) as changed:
        D2DailyCreditLedger(tmp_path / "production").create_d6_attempt(request, proof=result.proof)
    assert changed.value.code == "D6_PREREQUISITE_CHANGED"
    assert D2DailyCreditLedger(tmp_path / "production").attempts() == ()


def test_successful_d2_from_another_scene_does_not_authorize_d6(tmp_path: Path) -> None:
    ledger = _seed_success(tmp_path)
    ledger._mutate(  # noqa: SLF001 - another successful workload identity
        lambda payload: payload["attempts"][0].update(scene_id="SCN_000099")
    )
    result = evaluate_d6_release(
        prerequisite_d2_run_id=_D2_ID,
        d6_request=_d6_request(tmp_path),
        ledger=ledger,
    )
    assert result.failure_code == "D6_PREREQUISITE_BINDING_MISMATCH"


@pytest.mark.asyncio
async def test_failed_d6_release_stops_before_browser_or_ledger_mutation(
    tmp_path: Path,
) -> None:
    _seed_d2_state(tmp_path, state="UNKNOWN_SUBMISSION_STATE")
    request = _d6_request(tmp_path)
    port = FakeGenerationPort()
    d6_ledger = D2DailyCreditLedger(tmp_path / "production")
    journal = AgentSettingsRecoveryJournal(tmp_path / "browser-state", "PVA_0002", "prod-project")
    executor = GoogleFlowAgentOnlyGenerationExecutor(port, d6_ledger, journal)

    with pytest.raises(PhaseGError) as denied:
        await executor.execute(request)

    assert denied.value.code == "D6_PREREQUISITE_NOT_SUCCEEDED"
    assert port.calls == []
    assert d6_ledger.attempts() == ()
    assert journal.read() is None


@pytest.mark.asyncio
async def test_d6_requires_prod_context_and_shared_credit_guard(tmp_path: Path) -> None:
    _seed_success(tmp_path)
    request = _d6_request(tmp_path)
    port = FakeGenerationPort()
    d6_ledger = D2DailyCreditLedger(tmp_path / "production")
    journal = AgentSettingsRecoveryJournal(tmp_path / "browser-state", "PVA_0002", "prod-project")
    executor = GoogleFlowAgentOnlyGenerationExecutor(port, d6_ledger, journal)

    with pytest.raises(PhaseGError) as denied:
        await executor.execute(request)

    assert denied.value.code == "D6_PRODUCTION_CONTEXT_REQUIRED"
    assert port.calls == []
    assert d6_ledger.attempts() == ()
