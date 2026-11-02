"""Durable, canary-only bridge to the audited upstream Flow CLI adapter.

This module has no default live dependencies. A caller must inject its canonical
readers, identity verifier, adapter and storage, making the offline boundary
explicit and replay behavior testable.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, cast

from .phase_g_artifacts import (
    PersistedArtifact,
    PhaseGStoragePort,
    ValidatedVideo,
    persist_and_verify,
    validate_video,
)
from .production_execution import (
    CreditBudgetContext,
    ProductionCreditGuard,
    ProductionExecutionContext,
    ProductionExecutionError,
)
from .production_jobs import ProductionJobCoordinator, ProductionJobStore, ProductionRequest
from .upstream_gflow_adapter import (
    UPSTREAM_COMMIT,
    UPSTREAM_VERSION,
    AdapterRequest,
    AdapterResult,
    AdapterStatus,
    UpstreamGflowVideoAdapter,
)

UPSTREAM_CANARY_REQUEST_PREFIX = "upstream-canary-v1:"
_PROMPT_ID_RE = re.compile(r"^SPV_[0-9]{6}$")
_SCRIPT_ID_RE = re.compile(r"^SCR_[0-9]{6}$")
_SCENE_ID_RE = re.compile(r"^SCN_[0-9]{6}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROFILE_ID_RE = re.compile(r"^GPR_[0-9]{4}$")
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CANARY_KEY_RE = re.compile(r"^upstream-canary-v1:[A-Za-z0-9_.:-]{1,96}$")
_PROD_ACCOUNT_EMAIL = "singleava508@gmail.com"
_PROD_PROJECT_ID = "aafe9282-071f-4333-baa4-92b9f76a9c27"


class CanaryPolicyError(ValueError):
    """Fresh Master Data or authorization did not satisfy canary policy."""


@dataclass(frozen=True, slots=True)
class CanaryRequest:
    """Explicit, caller-owned authorization and stable lineage inputs."""

    script_id: str
    scene_id: str
    prompt_id: str
    generation_profile_id: str
    profile_name: str
    account_email: str
    project_id: str
    request_idempotency_key: str
    authorization_id: str
    authorized: bool
    ceo_credit_ceiling: Decimal
    staging_root: Path
    verification_temp_dir: Path


@dataclass(frozen=True, slots=True)
class FreshCanaryEvidence:
    """One fresh canonical read plus verified account/project and quote evidence."""

    prompt_status: str
    prompt_text: str
    prompt_sha256: str
    scene_id: str
    prompt_id: str
    generation_profile_id: str
    profile_name: str
    auth_profile: str
    account_email: str
    provider_account_id: str
    project_id: str
    model: str
    aspect_ratio: str
    duration_seconds: int
    output_count: int
    quote_credits: Decimal
    quote_input_digest: str
    quote_source_version: str
    quote_read_at: str
    credit_budget: CreditBudgetContext
    profile_home: Path
    identity_verified: bool
    scene_locked: bool
    project_policy_verified: bool
    profile_lease_released: bool
    adapter_commit: str
    upstream_version: str = UPSTREAM_VERSION
    upstream_commit: str = UPSTREAM_COMMIT


class FreshCanaryReader(Protocol):
    """Read the current approved Prompt, Profile, Account, Project and quote."""

    def read_fresh(self, request: CanaryRequest) -> FreshCanaryEvidence: ...


class CanaryAdapter(Protocol):
    """The one-shot adapter port; production callers inject the audited adapter."""

    def generate_once(self, request: AdapterRequest) -> AdapterResult: ...


class CanaryArtifactRecovery(Protocol):
    """Recover bytes for a known upstream media ID; it has no generation method."""

    def download_existing(self, media_id: str, destination: Path) -> Path: ...


@dataclass(frozen=True, slots=True)
class CanaryResult:
    state: str
    canary_invocation_id: str
    production_job_id: str
    scene_job_id: str
    reservation_credits: Decimal
    adapter_invocations: int
    validated_video: ValidatedVideo | None = None
    persisted_artifact: PersistedArtifact | None = None
    normalized_status: str | None = None


class UpstreamCanaryProductionCaller:
    """Execute the upstream adapter once, with durable reservation and replay guards."""

    def __init__(
        self,
        *,
        reader: FreshCanaryReader,
        jobs: ProductionJobStore,
        credit_guard: ProductionCreditGuard,
        adapter: CanaryAdapter | UpstreamGflowVideoAdapter,
        storage: PhaseGStoragePort,
        artifact_recovery: CanaryArtifactRecovery | None = None,
        validator: Callable[..., ValidatedVideo] = validate_video,
        persister: Callable[..., PersistedArtifact] = persist_and_verify,
    ) -> None:
        self._reader = reader
        self._jobs = jobs
        self._credit_guard = credit_guard
        self._adapter = adapter
        self._storage = storage
        self._artifact_recovery = artifact_recovery
        self._validator = validator
        self._persister = persister

    def run(self, request: CanaryRequest) -> CanaryResult:
        """Fresh-read, reserve and invoke once; replays after start are HOLD-only."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise CanaryPolicyError("upstream canary caller must run outside an asyncio event loop")
        if request.authorized is not True or not request.authorization_id.strip():
            raise CanaryPolicyError("explicit canary authorization is required")
        if (
            not _SCRIPT_ID_RE.fullmatch(request.script_id)
            or not _CANARY_KEY_RE.fullmatch(request.request_idempotency_key)
            or not _SCENE_ID_RE.fullmatch(request.scene_id)
            or not _PROMPT_ID_RE.fullmatch(request.prompt_id)
            or not _PROFILE_ID_RE.fullmatch(request.generation_profile_id)
            or not _PROFILE_NAME_RE.fullmatch(request.profile_name)
            or request.account_email != _PROD_ACCOUNT_EMAIL
            or request.project_id != _PROD_PROJECT_ID
            or not request.staging_root.is_absolute()
            or request.staging_root == Path(request.staging_root.anchor)
            or request.staging_root.is_symlink()
            or not request.verification_temp_dir.is_absolute()
        ):
            raise CanaryPolicyError("canary request does not match the exact PROD policy")

        evidence = self._reader.read_fresh(request)
        self._validate_fresh_evidence(request, evidence)
        invocation_id = (
            "UPC_"
            + hashlib.sha256(request.request_idempotency_key.encode("utf-8")).hexdigest()[:32]
        )
        production_request = ProductionRequest(
            request.script_id,
            "PROD",
            request.request_idempotency_key,
            (request.prompt_id,),
        )
        claim = ProductionJobCoordinator(self._jobs).submit_request(production_request)
        if (
            len(claim.scene_jobs) != 1
            or claim.scene_jobs[0].scene_prompt_id != request.prompt_id
            or claim.production_job.execution_environment != "PROD"
        ):
            raise CanaryPolicyError("durable job claim differs from the canary lineage")
        scene_job = claim.scene_jobs[0]
        output_path = request.staging_root / invocation_id / "output.mp4"
        context = ProductionExecutionContext(
            environment="PROD",
            provider_id="PRV_0001",
            provider_account_id=evidence.provider_account_id,
            provider_project_id=evidence.project_id,
            auth_profile=evidence.auth_profile,
            scene_id=evidence.scene_id,
            generation_profile_id=evidence.generation_profile_id,
            scene_prompt_id=evidence.prompt_id,
            attempt_id=invocation_id,
            credit_budget_context=evidence.credit_budget,
            production_job_id=claim.production_job.production_job_id,
            scene_job_id=scene_job.scene_job_id,
            max_reservation_credits=evidence.quote_credits,
            reservation_quote_input_digest=evidence.quote_input_digest,
            reservation_quote_source_version=evidence.quote_source_version,
            production_run_no=claim.production_job.production_run_no,
            request_idempotency_key=request.request_idempotency_key,
        )
        budget = evidence.credit_budget
        if budget.authorized_run_ceiling != request.ceo_credit_ceiling:
            raise CanaryPolicyError("fresh credit context does not bind the CEO ceiling")
        if evidence.quote_credits > request.ceo_credit_ceiling:
            raise CanaryPolicyError("fresh canonical quote exceeds the CEO ceiling")
        binding: dict[str, str | int] = {
            "canary_invocation_id": invocation_id,
            "authorization_id": request.authorization_id,
            "scene_id": evidence.scene_id,
            "prompt_id": evidence.prompt_id,
            "prompt_sha256": evidence.prompt_sha256,
            "generation_profile_id": evidence.generation_profile_id,
            "profile_name": evidence.profile_name,
            "auth_profile": evidence.auth_profile,
            "account_email": evidence.account_email,
            "project_id": evidence.project_id,
            "model": evidence.model,
            "aspect_ratio": evidence.aspect_ratio,
            "duration_seconds": evidence.duration_seconds,
            "output_count": evidence.output_count,
            "adapter_commit": evidence.adapter_commit,
            "upstream_version": evidence.upstream_version,
            "upstream_commit": evidence.upstream_commit,
            "output_path": str(output_path),
            "verification_temp_dir": str(request.verification_temp_dir),
            "quote_read_at": evidence.quote_read_at,
        }
        row = self._credit_guard.prepare_upstream_canary(
            context,
            request_idempotency_key=request.request_idempotency_key,
            quoted_credits=evidence.quote_credits,
            budget=budget,
            binding=binding,
        )
        state = str(row.get("state", ""))
        if state == "UPSTREAM_ARTIFACT_PENDING":
            validated = self._validate_and_persist(request, evidence, invocation_id, output_path)
            self._credit_guard.record_upstream_artifact(
                invocation_id,
                remote_file_id=validated.persisted_artifact.reference.asset_id,
                local_sha256=validated.persisted_artifact.verification.local_sha256,
                remote_sha256=validated.persisted_artifact.verification.remote_sha256,
            )
            return CanaryResult(
                "UPSTREAM_ARTIFACT_PERSISTED",
                invocation_id,
                claim.production_job.production_job_id,
                scene_job.scene_job_id,
                evidence.quote_credits,
                _int_or_zero(row.get("upstream_invocation_count")),
                validated.validated_video,
                validated.persisted_artifact,
                str(row.get("normalized_status")),
            )
        if state != "UPSTREAM_READY":
            return self._replay_result(
                row, claim.production_job.production_job_id, scene_job.scene_job_id
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint = self._credit_guard.start_upstream_invocation(invocation_id)
        adapter_request = AdapterRequest(
            approved_prompt=evidence.prompt_text,
            profile_id=evidence.generation_profile_id,
            profile_name=evidence.profile_name,
            expected_account=evidence.account_email,
            profile_home=evidence.profile_home,
            project_id=evidence.project_id,
            model=evidence.model,
            aspect_ratio=evidence.aspect_ratio,
            duration_seconds=evidence.duration_seconds,
            output_count=evidence.output_count,
            staging_root=output_path.parent,
            staging_output_path=output_path,
            production_job_id=claim.production_job.production_job_id,
            scene_job_id=scene_job.scene_job_id,
            request_idempotency_key=request.request_idempotency_key,
            generation_authorized=True,
        )
        try:
            result = self._adapter.generate_once(adapter_request)
        except BaseException:
            # The durable started checkpoint remains unresolved; never invoke again.
            raise
        if checkpoint.get("upstream_invocation_count") != 1:
            raise ProductionExecutionError("durable canary invocation count is invalid")
        status = self._persist_adapter_result(
            invocation_id,
            result,
            production_job_id=claim.production_job.production_job_id,
            scene_job_id=scene_job.scene_job_id,
            request_idempotency_key=request.request_idempotency_key,
            project_id=evidence.project_id,
            profile_id=evidence.generation_profile_id,
            output_path=output_path,
        )
        saved = self._credit_guard.upstream_canary_row(invocation_id)
        state = str(saved.get("state", "UPSTREAM_UNKNOWN_HOLD"))
        if state != "UPSTREAM_ARTIFACT_PENDING":
            return self._replay_result(
                saved, claim.production_job.production_job_id, scene_job.scene_job_id
            )
        validated = self._validate_and_persist(request, evidence, invocation_id, output_path)
        self._credit_guard.record_upstream_artifact(
            invocation_id,
            remote_file_id=validated.persisted_artifact.reference.asset_id,
            local_sha256=validated.persisted_artifact.verification.local_sha256,
            remote_sha256=validated.persisted_artifact.verification.remote_sha256,
        )
        return CanaryResult(
            "UPSTREAM_ARTIFACT_PERSISTED",
            invocation_id,
            claim.production_job.production_job_id,
            scene_job.scene_job_id,
            evidence.quote_credits,
            1,
            validated.validated_video,
            validated.persisted_artifact,
            status,
        )

    def recover_download(self, invocation_id: str) -> CanaryResult:
        """Download a known media ID only; this path has no adapter invocation."""
        if self._artifact_recovery is None:
            raise CanaryPolicyError("existing-asset download recovery is not configured")
        row = self._credit_guard.upstream_canary_row(invocation_id)
        if row.get("state") != "UPSTREAM_DOWNLOAD_RECOVERY_REQUIRED":
            raise CanaryPolicyError("canary has no generated asset awaiting download recovery")
        result_fields = row.get("normalized_result")
        media_id = (
            cast(dict[str, object], result_fields).get("media_id")
            if isinstance(result_fields, dict)
            else None
        )
        output_path = Path(str(row.get("upstream_output_path", "")))
        if not isinstance(media_id, str) or not media_id or not output_path.is_absolute():
            raise CanaryPolicyError("persisted generated media identity is incomplete")
        if output_path.is_symlink():
            raise CanaryPolicyError("existing recovered asset path is unsafe")
        if not output_path.exists():
            recovered = self._artifact_recovery.download_existing(media_id, output_path)
            if recovered != output_path:
                raise CanaryPolicyError("asset recovery wrote to an unexpected path")
        elif not output_path.is_file():
            raise CanaryPolicyError("existing recovered asset path is unsafe")
        evidence = _evidence_from_row(row)
        validated = self._validate_and_persist(
            CanaryRequest(
                script_id="RECOVERY",
                scene_id=str(row["upstream_scene_id"]),
                prompt_id=str(row["upstream_prompt_id"]),
                generation_profile_id=str(row["upstream_generation_profile_id"]),
                profile_name=str(row["upstream_profile_name"]),
                account_email=str(row["upstream_account_email"]),
                project_id=str(row["provider_project_id"]),
                request_idempotency_key=str(row["request_idempotency_key"]),
                authorization_id="EXISTING_ASSET_RECOVERY",
                authorized=True,
                ceo_credit_ceiling=Decimal(str(row["authorized_run_ceiling"])),
                staging_root=output_path.parent,
                verification_temp_dir=Path(str(row["upstream_verification_temp_dir"])),
            ),
            evidence,
            invocation_id,
            output_path,
        )
        persisted = self._credit_guard.record_upstream_artifact(
            invocation_id,
            remote_file_id=validated.persisted_artifact.reference.asset_id,
            local_sha256=validated.persisted_artifact.verification.local_sha256,
            remote_sha256=validated.persisted_artifact.verification.remote_sha256,
        )
        return CanaryResult(
            str(persisted["state"]),
            invocation_id,
            str(row["production_job_id"]),
            str(row["scene_job_id"]),
            Decimal(str(row["reserved_credits"])),
            _int_or_zero(row.get("upstream_invocation_count")),
            validated.validated_video,
            validated.persisted_artifact,
            str(row.get("normalized_status")),
        )

    def _persist_adapter_result(
        self,
        invocation_id: str,
        result: object,
        *,
        production_job_id: str,
        scene_job_id: str,
        request_idempotency_key: str,
        project_id: str,
        profile_id: str,
        output_path: Path,
    ) -> str:
        typed_result = result if isinstance(result, AdapterResult) else None
        valid_result = typed_result is not None and (
            typed_result.schema_version == "UPSTREAM_GFLOW_ADAPTER_RESULT_V1"
            and typed_result.production_job_id == production_job_id
            and typed_result.scene_job_id == scene_job_id
            and typed_result.request_idempotency_key == request_idempotency_key
            and typed_result.requested_project_id == project_id
            and typed_result.profile_id == profile_id
            and typed_result.auto_retry is False
            and (
                typed_result.status is not AdapterStatus.UPSTREAM_GENERATION_COMPLETED
                or (
                    typed_result.local_output_path is not None
                    and typed_result.local_output_path.resolve() == output_path.resolve()
                    and bool(typed_result.media_id)
                )
            )
            and (
                typed_result.status is not AdapterStatus.DOWNLOAD_FAILURE_AFTER_GENERATION
                or bool(typed_result.media_id)
            )
        )
        normalized_status = AdapterStatus.POST_SUBMIT_UNKNOWN
        if valid_result and typed_result is not None:
            try:
                normalized_status = AdapterStatus(typed_result.status)
            except (TypeError, ValueError):
                valid_result = False
        if not valid_result or typed_result is None:
            status = normalized_status
            fields: dict[str, str | int | bool | None] = {"submit_may_have_occurred": True}
        else:
            status = normalized_status
            fields = {
                "upstream_operation_id": typed_result.upstream_operation_id,
                "media_id": typed_result.media_id,
                "stdout_digest": typed_result.stdout_digest,
                "stderr_digest": typed_result.stderr_digest,
                "normalized_error_code": typed_result.normalized_error_code,
                "upstream_error_type": typed_result.upstream_error_type,
                "submit_may_have_occurred": typed_result.submit_may_have_occurred,
                "exit_code": typed_result.exit_code,
            }
        self._credit_guard.record_upstream_result(
            invocation_id, status=status.value, result_fields=fields
        )
        return status.value

    def _validate_and_persist(
        self,
        request: CanaryRequest,
        evidence: FreshCanaryEvidence,
        invocation_id: str,
        output_path: Path,
    ) -> _ValidatedAndPersisted:
        validated_video = self._validator(
            output_path,
            expected_duration_sec=float(evidence.duration_seconds),
            expected_aspect_ratio=evidence.aspect_ratio,
        )
        persisted = self._persister(
            self._storage,
            output_path,
            f"upstream-canary/{invocation_id}/output.mp4",
            verification_temp_dir=request.verification_temp_dir,
            cleanup_local=False,
        )
        return _ValidatedAndPersisted(validated_video, persisted)

    def _validate_fresh_evidence(
        self, request: CanaryRequest, evidence: FreshCanaryEvidence
    ) -> None:
        try:
            quote = Decimal(str(evidence.quote_credits))
        except InvalidOperation as exc:
            raise CanaryPolicyError("fresh canonical quote is invalid") from exc
        try:
            quote_read_at = datetime.fromisoformat(evidence.quote_read_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise CanaryPolicyError("fresh canonical quote timestamp is invalid") from exc
        now = datetime.now(UTC)
        if (
            quote_read_at.tzinfo is None
            or quote_read_at.utcoffset() is None
            or quote_read_at > now + timedelta(seconds=30)
            or now - quote_read_at.astimezone(UTC) > timedelta(minutes=5)
        ):
            raise CanaryPolicyError("canonical quote is not fresh")
        if (
            evidence.prompt_status != "APPROVED"
            or not evidence.prompt_text.strip()
            or not _SHA256_RE.fullmatch(evidence.prompt_sha256)
            or hashlib.sha256(evidence.prompt_text.encode("utf-8")).hexdigest()
            != evidence.prompt_sha256
            or evidence.scene_id != request.scene_id
            or evidence.prompt_id != request.prompt_id
            or evidence.generation_profile_id != request.generation_profile_id
            or evidence.profile_name != request.profile_name
            or evidence.account_email != request.account_email
            or evidence.project_id != request.project_id
            or evidence.provider_account_id != "PVA_0002"
            or evidence.auth_profile != "GFLOW_PROD_AUTH"
            or evidence.identity_verified is not True
            or evidence.scene_locked is not True
            or evidence.project_policy_verified is not True
            or evidence.profile_lease_released is not True
            or quote <= 0
            or not quote.is_finite()
            or not _SHA256_RE.fullmatch(evidence.quote_input_digest)
            or not evidence.quote_source_version.strip()
            or not evidence.quote_read_at.strip()
            or not evidence.profile_home.is_absolute()
            or evidence.model != "veo-lite"
            or evidence.aspect_ratio != "9:16"
            or evidence.duration_seconds != 8
            or evidence.output_count != 1
            or not request.ceo_credit_ceiling.is_finite()
            or request.ceo_credit_ceiling <= 0
            or evidence.upstream_version != UPSTREAM_VERSION
            or evidence.upstream_commit != UPSTREAM_COMMIT
            or not re.fullmatch(r"[0-9a-f]{40}", evidence.adapter_commit)
            or quote > request.ceo_credit_ceiling
        ):
            raise CanaryPolicyError("fresh canonical or PROD account/project evidence failed")

    @staticmethod
    def _replay_result(
        row: dict[str, object], production_job_id: str, scene_job_id: str
    ) -> CanaryResult:
        state = str(row.get("state", "UPSTREAM_UNKNOWN_HOLD"))
        try:
            amount = Decimal(str(row.get("reserved_credits", "0")))
        except InvalidOperation:
            amount = Decimal(0)
        status = row.get("normalized_status")
        return CanaryResult(
            state,
            str(row.get("attempt_id", "")),
            production_job_id,
            scene_job_id,
            amount,
            _int_or_zero(row.get("upstream_invocation_count")),
            normalized_status=str(status) if isinstance(status, str) else None,
        )


@dataclass(frozen=True, slots=True)
class _ValidatedAndPersisted:
    validated_video: ValidatedVideo
    persisted_artifact: PersistedArtifact


def _evidence_from_row(row: dict[str, object]) -> FreshCanaryEvidence:
    """Rebuild only the artifact-validation fields; never re-read or regenerate."""
    return FreshCanaryEvidence(
        prompt_status="APPROVED",
        prompt_text="",
        prompt_sha256=str(row["upstream_prompt_sha256"]),
        scene_id=str(row["upstream_scene_id"]),
        prompt_id=str(row["upstream_prompt_id"]),
        generation_profile_id=str(row["upstream_generation_profile_id"]),
        profile_name=str(row["upstream_profile_name"]),
        auth_profile=str(row["upstream_auth_profile"]),
        account_email=str(row["upstream_account_email"]),
        provider_account_id=str(row["provider_account_id"]),
        project_id=str(row["provider_project_id"]),
        model=str(row["upstream_model"]),
        aspect_ratio=str(row["upstream_aspect_ratio"]),
        duration_seconds=int(str(row["upstream_duration_seconds"])),
        output_count=int(str(row["upstream_output_count"])),
        quote_credits=Decimal(str(row["quoted_credits"])),
        quote_input_digest=str(row["reservation_quote_input_digest"]),
        quote_source_version=str(row["reservation_quote_source_version"]),
        quote_read_at=str(row["upstream_quote_read_at"]),
        credit_budget=CreditBudgetContext(
            authorized_run_ceiling=Decimal(str(row["authorized_run_ceiling"])),
            provider_remaining_free=Decimal("50"),
        ),
        profile_home=Path.cwd(),
        identity_verified=True,
        scene_locked=True,
        project_policy_verified=True,
        profile_lease_released=True,
        adapter_commit=str(row["upstream_adapter_commit"]),
        upstream_version=str(row["upstream_upstream_version"]),
        upstream_commit=str(row["upstream_upstream_commit"]),
    )


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return 0
    try:
        return int(value)
    except ValueError:
        return 0
