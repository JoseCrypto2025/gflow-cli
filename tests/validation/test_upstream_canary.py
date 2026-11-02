from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.assets import AssetReference
from gflow_cli.v2_runtime.phase_e import sha256_json
from gflow_cli.v2_runtime.phase_g_artifacts import (
    PersistedArtifact,
    ValidatedVideo,
    VerifiedRemoteArtifact,
)
from gflow_cli.v2_runtime.production_execution import (
    CreditBudgetContext,
    ProductionCreditGuard,
    ProductionExecutionError,
)
from gflow_cli.v2_runtime.production_jobs import InMemoryProductionJobStore
from gflow_cli.v2_runtime.production_orchestrator import (
    ProductionOrchestrationError,
    ProductionOrchestrator,
)
from gflow_cli.v2_runtime.upstream_canary import (
    CanaryPolicyError,
    CanaryRequest,
    FreshCanaryEvidence,
    UpstreamCanaryProductionCaller,
)
from gflow_cli.v2_runtime.upstream_gflow_adapter import (
    RESULT_SCHEMA_VERSION,
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
)


class FreshReader:
    def __init__(self, evidence: FreshCanaryEvidence) -> None:
        self.evidence = evidence
        self.reads = 0

    def read_fresh(self, request: CanaryRequest) -> FreshCanaryEvidence:
        self.reads += 1
        return self.evidence


class FakeAdapter:
    def __init__(self, status: AdapterStatus = AdapterStatus.UPSTREAM_GENERATION_COMPLETED) -> None:
        self.status = status
        self.calls = 0
        self.checkpoint: dict[str, object] | None = None
        self.guard: ProductionCreditGuard | None = None

    def generate_once(self, request: AdapterRequest) -> AdapterResult:
        self.calls += 1
        assert self.guard is not None
        invocation_id = (
            "UPC_"
            + hashlib.sha256(request.request_idempotency_key.encode("utf-8")).hexdigest()[:32]
        )
        row = self.guard.upstream_canary_row(invocation_id)
        self.checkpoint = row
        if self.status is AdapterStatus.UPSTREAM_GENERATION_COMPLETED:
            request.staging_output_path.parent.mkdir(parents=True, exist_ok=True)
            request.staging_output_path.write_bytes(b"offline fake mp4")
            path = request.staging_output_path
        else:
            path = None
        return _adapter_result(request, self.status, path)


class FakeRecovery:
    def __init__(self) -> None:
        self.media_ids: list[str] = []

    def download_existing(self, media_id: str, destination: Path) -> Path:
        self.media_ids.append(media_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"recovered existing media")
        return destination


def _request(tmp_path: Path) -> CanaryRequest:
    return CanaryRequest(
        script_id="SCR_000001",
        scene_id="SCN_000001",
        prompt_id="SPV_000007",
        generation_profile_id="GPR_0002",
        profile_name="gflow-prod",
        account_email="singleava508@gmail.com",
        project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
        request_idempotency_key="upstream-canary-v1:SPV_000007:run-1",
        authorization_id="CANARY_TEST_AUTH",
        authorized=True,
        ceo_credit_ceiling=Decimal("30"),
        staging_root=tmp_path / "stage",
        verification_temp_dir=tmp_path / "verify",
    )


def _evidence(tmp_path: Path) -> FreshCanaryEvidence:
    return FreshCanaryEvidence(
        prompt_status="APPROVED",
        prompt_text="A quiet cinematic scene.",
        prompt_sha256=sha256_json({"prompt": "A quiet cinematic scene.", "negative_prompt": ""}),
        scene_id="SCN_000001",
        prompt_id="SPV_000007",
        generation_profile_id="GPR_0002",
        profile_name="gflow-prod",
        auth_profile="GFLOW_PROD_AUTH",
        account_email="singleava508@gmail.com",
        provider_account_id="PVA_0002",
        project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
        model="veo-lite",
        aspect_ratio="9:16",
        duration_seconds=8,
        output_count=1,
        quote_credits=Decimal("10"),
        quote_input_digest="c" * 64,
        quote_source_version="PCT_000001:verified:fixture",
        quote_read_at=datetime.now(UTC).isoformat(),
        credit_budget=CreditBudgetContext(
            authorized_run_ceiling=Decimal("30"), provider_remaining_free=Decimal("50")
        ),
        profile_home=tmp_path / "profile",
        identity_verified=True,
        scene_locked=True,
        project_policy_verified=True,
        profile_lease_released=True,
        adapter_commit="d" * 40,
    )


def _adapter_result(
    request: AdapterRequest, status: AdapterStatus, path: Path | None
) -> AdapterResult:
    return AdapterResult(
        schema_version=RESULT_SCHEMA_VERSION,
        status=status,
        exit_code=0 if status is AdapterStatus.UPSTREAM_GENERATION_COMPLETED else 23,
        upstream_operation_id="operation-1",
        requested_project_id=request.project_id,
        project_binding_source="REQUEST_ARGUMENT_ONLY",
        profile_id=request.profile_id,
        production_job_id=request.production_job_id,
        scene_job_id=request.scene_job_id,
        request_idempotency_key=request.request_idempotency_key,
        media_id="media-1" if status is not AdapterStatus.PRE_SUBMIT_FAILURE else None,
        local_output_path=path,
        stdout_digest="e" * 64,
        stderr_digest="f" * 64,
        elapsed_ms=10,
        normalized_error_code=None,
        upstream_error_type=None,
        submit_may_have_occurred=status
        not in {
            AdapterStatus.PRE_SUBMIT_FAILURE,
            AdapterStatus.AUTH_FAILURE,
            AdapterStatus.UNSUPPORTED_FLOW_STATE,
            AdapterStatus.INSUFFICIENT_CREDITS,
        },
        auto_retry=False,
        safe_message="offline result",
        remediation="none",
    )


def _make_caller(
    tmp_path: Path,
    *,
    status: AdapterStatus = AdapterStatus.UPSTREAM_GENERATION_COMPLETED,
    evidence: FreshCanaryEvidence | None = None,
    recovery: FakeRecovery | None = None,
) -> tuple[UpstreamCanaryProductionCaller, FakeAdapter, ProductionCreditGuard]:
    guard = ProductionCreditGuard(tmp_path / "runtime")
    adapter = FakeAdapter(status)
    adapter.guard = guard

    def validate(path: Path, *, expected_duration_sec: float, expected_aspect_ratio: str):
        assert path.is_file()
        assert expected_duration_sec == 8.0
        assert expected_aspect_ratio == "9:16"
        return ValidatedVideo(path, path.stat().st_size, "a" * 64, 8.0, 720, 1280, "9:16", False, 1)

    def persist(
        storage, path: Path, logical_key: str, *, verification_temp_dir: Path, cleanup_local: bool
    ):
        assert logical_key.startswith("upstream-canary/UPC_")
        assert cleanup_local is False
        return PersistedArtifact(
            AssetReference("google-drive", "drive-file-1", logical_key),
            VerifiedRemoteArtifact(
                "drive-file-1", logical_key, path.stat().st_size, "a" * 64, "a" * 64
            ),
        )

    caller = UpstreamCanaryProductionCaller(
        reader=FreshReader(evidence or _evidence(tmp_path)),
        jobs=InMemoryProductionJobStore(),
        credit_guard=guard,
        adapter=adapter,
        storage=object(),
        artifact_recovery=recovery,
        validator=validate,
        persister=persist,
    )
    return caller, adapter, guard


def test_success_persists_checkpoint_before_one_shot_and_artifact(tmp_path: Path) -> None:
    caller, adapter, guard = _make_caller(tmp_path)
    result = caller.run(_request(tmp_path))

    assert adapter.calls == 1
    assert adapter.checkpoint is not None
    assert adapter.checkpoint["state"] == "UPSTREAM_INVOCATION_STARTED"
    assert adapter.checkpoint["reserved_credits"] == "10"
    assert adapter.checkpoint["checkpoint_state"] == "UPSTREAM_INVOCATION_STARTED"
    assert result.state == "UPSTREAM_ARTIFACT_PERSISTED"
    assert result.reservation_credits == Decimal("10")
    assert result.persisted_artifact is not None
    assert (
        result.persisted_artifact.verification.local_sha256
        == result.persisted_artifact.verification.remote_sha256
    )
    assert guard.upstream_canary_row(result.canary_invocation_id)["generate_click_count"] == 0


def test_success_replay_reuses_job_and_invocation_without_adapter_call(tmp_path: Path) -> None:
    caller, adapter, guard = _make_caller(tmp_path)
    first = caller.run(_request(tmp_path))
    second = caller.run(_request(tmp_path))

    assert first.canary_invocation_id == second.canary_invocation_id
    assert first.production_job_id == second.production_job_id
    assert first.scene_job_id == second.scene_job_id
    assert adapter.calls == 1
    assert guard.upstream_canary_row(first.canary_invocation_id)["upstream_invocation_count"] == 1


def test_crash_after_success_result_recovers_artifact_without_invoking_again(
    tmp_path: Path,
) -> None:
    caller, adapter, guard = _make_caller(tmp_path)
    original_validator = caller._validator  # noqa: SLF001 - simulate a crash after result persistence
    fail_once = True

    def interrupt_once(*args, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("simulated artifact validation interruption")
        return original_validator(*args, **kwargs)

    caller._validator = interrupt_once  # noqa: SLF001 - deterministic recovery injection
    with pytest.raises(RuntimeError, match="artifact validation interruption"):
        caller.run(_request(tmp_path))
    invocation_id = (
        "UPC_"
        + hashlib.sha256(_request(tmp_path).request_idempotency_key.encode("utf-8")).hexdigest()[
            :32
        ]
    )
    assert guard.upstream_canary_row(invocation_id)["state"] == "UPSTREAM_ARTIFACT_PENDING"

    recovered = caller.run(_request(tmp_path))
    assert recovered.state == "UPSTREAM_ARTIFACT_PERSISTED"
    assert adapter.calls == 1


def test_malformed_adapter_result_is_held_without_replay(tmp_path: Path) -> None:
    caller, adapter, guard = _make_caller(tmp_path)

    def malformed(_request: AdapterRequest) -> AdapterResult:
        adapter.calls += 1
        return None  # type: ignore[return-value]

    adapter.generate_once = malformed  # type: ignore[method-assign]
    result = caller.run(_request(tmp_path))
    replay = caller.run(_request(tmp_path))

    assert result.state == replay.state == "UPSTREAM_UNKNOWN_HOLD"
    assert adapter.calls == 1
    assert guard.upstream_canary_row(result.canary_invocation_id)["reserved_credits"] == "10"


def test_concurrent_same_key_canary_invokes_adapter_at_most_once(tmp_path: Path) -> None:
    caller, adapter, guard = _make_caller(tmp_path)
    request = _request(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(caller.run, request) for _ in range(2)]
        results = []
        for future in futures:
            try:
                results.append(future.result())
            except Exception:
                pass

    assert adapter.calls == 1
    assert len(results) >= 1
    assert (
        guard.upstream_canary_row(results[0].canary_invocation_id)["upstream_invocation_count"] == 1
    )


def test_canary_reservation_cannot_overrun_shared_daily_ceiling(tmp_path: Path) -> None:
    caller, adapter, guard = _make_caller(tmp_path)
    bangkok_day = datetime.now(UTC).astimezone(timezone(timedelta(hours=7))).date().isoformat()
    guard.root.mkdir(parents=True, exist_ok=True)
    guard.path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "attempts": [
                    {
                        "attempt_id": "prior-success",
                        "provider_id": "PRV_0001",
                        "date": bangkok_day,
                        "state": "SUCCESS",
                        "reserved_credits": "45",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProductionExecutionError, match="shared daily ceiling"):
        caller.run(_request(tmp_path))
    assert adapter.calls == 0


def test_restart_after_durable_start_never_launches_adapter_again(tmp_path: Path) -> None:
    caller, adapter, guard = _make_caller(tmp_path)
    adapter_error = RuntimeError("simulated process interruption after invocation-start")

    def interrupted(_request: AdapterRequest) -> AdapterResult:
        adapter.calls += 1
        raise adapter_error

    adapter.generate_once = interrupted  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="simulated process interruption"):
        caller.run(_request(tmp_path))
    invocation_id = (
        "UPC_"
        + hashlib.sha256(_request(tmp_path).request_idempotency_key.encode("utf-8")).hexdigest()[
            :32
        ]
    )
    assert guard.upstream_canary_row(invocation_id)["state"] == "UPSTREAM_INVOCATION_STARTED"

    replay = caller.run(_request(tmp_path))
    assert replay.state == "UPSTREAM_INVOCATION_STARTED"
    assert adapter.calls == 1


def test_crash_before_start_checkpoint_can_resume_safely(tmp_path: Path) -> None:
    caller, adapter, guard = _make_caller(tmp_path)
    original_start = guard.start_upstream_invocation
    fail_once = True

    def interrupted_before_start(attempt_id: str) -> dict[str, object]:
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise RuntimeError("simulated crash before invocation-start")
        return original_start(attempt_id)

    guard.start_upstream_invocation = interrupted_before_start  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="before invocation-start"):
        caller.run(_request(tmp_path))
    invocation_id = (
        "UPC_"
        + hashlib.sha256(_request(tmp_path).request_idempotency_key.encode("utf-8")).hexdigest()[
            :32
        ]
    )
    assert guard.upstream_canary_row(invocation_id)["state"] == "UPSTREAM_READY"

    caller.run(_request(tmp_path))
    assert adapter.calls == 1
    assert guard.upstream_canary_row(invocation_id)["upstream_invocation_count"] == 1


@pytest.mark.parametrize(
    "status,expected_state",
    [
        (AdapterStatus.POST_SUBMIT_UNKNOWN, "UPSTREAM_UNKNOWN_HOLD"),
        (AdapterStatus.AUTH_FAILURE, "UPSTREAM_PRE_SUBMIT_FAILED"),
        (AdapterStatus.PROJECT_NAVIGATION_FAILURE, "UPSTREAM_UNKNOWN_HOLD"),
        (AdapterStatus.INSUFFICIENT_CREDITS, "UPSTREAM_PRE_SUBMIT_FAILED"),
        (AdapterStatus.UNSUPPORTED_FLOW_STATE, "UPSTREAM_PRE_SUBMIT_FAILED"),
        (AdapterStatus.PRE_SUBMIT_FAILURE, "UPSTREAM_PRE_SUBMIT_FAILED"),
    ],
)
def test_non_success_outcomes_never_retry(
    tmp_path: Path, status: AdapterStatus, expected_state: str
) -> None:
    caller, adapter, guard = _make_caller(tmp_path, status=status)
    first = caller.run(_request(tmp_path))
    replay = caller.run(_request(tmp_path))
    row = guard.upstream_canary_row(first.canary_invocation_id)

    assert first.state == expected_state
    assert replay.state == expected_state
    assert adapter.calls == 1
    assert row["upstream_invocation_count"] == 1
    assert row["reserved_credits"] == (
        "0" if expected_state == "UPSTREAM_PRE_SUBMIT_FAILED" else "10"
    )


def test_download_failure_recovers_existing_media_without_invocation(tmp_path: Path) -> None:
    recovery = FakeRecovery()
    caller, adapter, guard = _make_caller(
        tmp_path,
        status=AdapterStatus.DOWNLOAD_FAILURE_AFTER_GENERATION,
        recovery=recovery,
    )
    first = caller.run(_request(tmp_path))
    recovered = caller.recover_download(first.canary_invocation_id)

    assert first.state == "UPSTREAM_DOWNLOAD_RECOVERY_REQUIRED"
    assert recovered.state == "UPSTREAM_ARTIFACT_PERSISTED"
    assert recovery.media_ids == ["media-1"]
    assert adapter.calls == 1
    assert guard.upstream_canary_row(first.canary_invocation_id)["upstream_invocation_count"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"prompt_status": "DRAFT"},
        {"prompt_text": "different prompt"},
        {"prompt_sha256": "d" * 64},
        {"account_email": "wrong@example.com"},
        {"project_id": "wrong-project"},
        {"identity_verified": False},
        {"scene_locked": False},
        {"project_policy_verified": False},
        {"profile_lease_released": False},
        {"quote_credits": Decimal("31")},
    ],
)
def test_bad_fresh_evidence_stops_before_adapter(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    evidence = replace(_evidence(tmp_path), **changes)
    caller, adapter, _guard = _make_caller(tmp_path, evidence=evidence)

    with pytest.raises(CanaryPolicyError):
        caller.run(_request(tmp_path))
    assert adapter.calls == 0


@pytest.mark.asyncio
async def test_caller_rejects_invocation_inside_asyncio_event_loop(tmp_path: Path) -> None:
    caller, adapter, _guard = _make_caller(tmp_path)

    with pytest.raises(CanaryPolicyError, match="outside an asyncio event loop"):
        caller.run(_request(tmp_path))
    assert adapter.calls == 0


def test_phase_g_orchestrator_rejects_canary_job_key(tmp_path: Path) -> None:
    orchestrator = ProductionOrchestrator(
        InMemoryProductionJobStore(),
        object(),
        object(),
        temp_root=tmp_path,
    )
    from gflow_cli.v2_runtime.phase_e import PreflightResult
    from gflow_cli.v2_runtime.production_jobs import ProductionRequest

    with pytest.raises(ProductionOrchestrationError, match="isolated from the Phase G"):
        orchestrator.run_scene_once(
            ProductionRequest("SCR_000001", "PROD", "upstream-canary-v1:blocked", ("SPV_000007",)),
            scene={},
            approved_prompt={},
            preflight=PreflightResult("NO", False, "", ""),
            dialogue_rows=(),
            expected_duration_sec=8,
            expected_aspect_ratio="9:16",
            workspace=object(),
            workspace_created_at=datetime.now(UTC),
            verification_temp_dir=tmp_path,
            occurred_at=datetime.now(UTC).isoformat(),
        )
