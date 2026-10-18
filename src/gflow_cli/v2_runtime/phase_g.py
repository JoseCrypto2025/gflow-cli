"""Fail-closed Phase G execution records and the local D2 credit ledger.

Phase G is deliberately separate from the Phase F preparation port. This module
contains no browser or provider calls; the concrete executor owns those edges.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol, cast

from gflow_cli.errors import WafRejectionError

from .agent_recovery import AgentSettingsRecoveryJournal, RecoveryJournalError
from .errors import AssetVerificationError, V2RuntimeError
from .google_flow import FlowPresubmitRequest, build_agent_only_directive
from .phase_g_artifacts import sha256_file, verify_remote_redownload
from .timestamps import format_utc_timestamp

_RUN_ID_RE = re.compile(r"^D2_[A-Za-z0-9_-]{8,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_LEDGER_SCHEMA = 1
_BANGKOK = timezone(timedelta(hours=7), name="Asia/Bangkok")
_SUBMISSION_STATES = frozenset(
    {
        "PREPARED",
        "GENERATE_ACTION_RECORDED",
        "COST_RESERVED_AWAITING_APPROVAL",
        "SUBMISSION_ACKNOWLEDGED",
        "GENERATION_PENDING",
        "GENERATION_SUCCEEDED",
        "D2_SUCCEEDED",
        "PROVIDER_REFUSED",
        "WAF_REJECTED",
        "GENERATION_FAILED",
        "UNEXPECTED_MULTI_OUTPUT",
        "MEDIA_VALIDATION_FAILED",
        "PERSISTENCE_FAILED",
        "UNKNOWN_SUBMISSION_STATE",
        "RECONCILED_NO_CHARGE",
    }
)
_TERMINAL_STATES = frozenset(
    {
        "D2_SUCCEEDED",
        "PROVIDER_REFUSED",
        "WAF_REJECTED",
        "GENERATION_FAILED",
        "UNEXPECTED_MULTI_OUTPUT",
        "MEDIA_VALIDATION_FAILED",
        "PERSISTENCE_FAILED",
        "RECONCILED_NO_CHARGE",
    }
)


class PhaseGError(V2RuntimeError):
    """Stable, non-secret failure from Phase G preconditions or accounting."""

    code = "PHASE_G_HOLD"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class PhaseGExecutionRequest:
    """Immutable generation contract pinned to one approved Scene prompt."""

    scene_id: str
    scene_prompt_id: str
    generation_profile_id: str
    provider_id: str
    provider_account_id: str
    provider_project_id: str
    model_id: str
    generation_mode: str
    compiled_prompt_text: str
    compiled_negative_prompt: str
    source_context_sha256: str
    prompt_sha256: str
    duration_sec: float
    aspect_ratio: str
    output_count: int
    d2_run_id: str
    presubmit_request: FlowPresubmitRequest

    @classmethod
    def from_presubmit(
        cls, request: FlowPresubmitRequest, d2_run_id: str
    ) -> PhaseGExecutionRequest:
        """Construct a pinned Phase G request from a Phase F input."""
        result = request.preflight
        prompt = request.prompt_version
        config = result.technical_configuration
        duration = config.get("duration_sec")
        aspect = config.get("aspect_ratio")
        count = config.get("output_count")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise PhaseGError("PHASE_G_REQUEST_INVALID", "Phase E duration is unresolved")
        if not isinstance(aspect, str) or not aspect:
            raise PhaseGError("PHASE_G_REQUEST_INVALID", "Phase E aspect ratio is unresolved")
        if isinstance(count, bool) or not isinstance(count, int):
            raise PhaseGError("PHASE_G_REQUEST_INVALID", "Phase E output count is unresolved")
        value = cls(
            result.scene_id,
            prompt.scene_prompt_id,
            prompt.generation_profile_id,
            request.provider_id,
            request.provider_account_id,
            request.provider_project_id,
            request.model_id,
            request.generation_mode,
            prompt.compiled_prompt_text,
            prompt.compiled_negative_prompt,
            prompt.source_context_sha256,
            prompt.prompt_sha256,
            float(duration),
            aspect,
            count,
            d2_run_id,
            request,
        )
        validate_phase_g_request(value)
        return value


@dataclass(frozen=True, slots=True)
class PhaseGAttemptEvidence:
    """Sanitized evidence recorded for one D2 attempt."""

    d2_run_id: str
    submission_state: str
    provider_execution_id: str | None = None
    provider_media_id: str | None = None
    provider_project_id: str | None = None
    terminal_state: str | None = None
    accounted_credit_cost: Decimal | None = None
    attributed_confirmation_id: str | None = None
    new_provider_media_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FlowSubmissionBaseline:
    """Attempt-specific provider state captured immediately before Generate."""

    project_id: str
    media_ids: frozenset[str]
    execution_ids: frozenset[str]
    pending_count: int
    confirmation_ids: frozenset[str]
    agent_turn_id: str | None


@dataclass(frozen=True, slots=True)
class FlowConfirmationGate:
    """One live confirmation gate with its explicit quote."""

    confirmation_id: str
    project_id: str
    video_count: int | None
    credit_cost: Decimal | None
    visible_balance: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PhaseGExecutionResult:
    """Sanitized result of the single Phase G execution attempt."""

    d2_run_id: str
    submission_state: str
    confirmation_id: str
    provider_execution_id: str | None = None
    provider_media_id: str | None = None
    provider_project_id: str | None = None


class FlowGenerationPort(Protocol):
    """Generation-only interface kept separate from the F5 preparation port."""

    async def bind_request(self, request: PhaseGExecutionRequest) -> None: ...
    async def snapshot_settings(self) -> Mapping[str, object]: ...
    async def apply_settings(self, settings: Mapping[str, object]) -> Mapping[str, object]: ...
    async def read_settings(self) -> Mapping[str, object]: ...
    async def restore_settings(self, settings: Mapping[str, object]) -> None: ...
    async def fill_prompt(self, prompt: str, negative_prompt: str) -> None: ...
    async def read_prompt(self) -> str | None: ...
    async def capture_submission_baseline(self) -> FlowSubmissionBaseline: ...
    async def click_generate_once(self) -> None: ...
    async def await_confirmation(
        self, baseline: FlowSubmissionBaseline
    ) -> FlowConfirmationGate: ...
    async def approve_confirmation(self, confirmation_id: str) -> None: ...
    async def observe_attempt(
        self, baseline: FlowSubmissionBaseline, confirmation_id: str
    ) -> PhaseGAttemptEvidence: ...


class GoogleFlowAgentOnlyGenerationExecutor:
    """One-attempt controller; concrete browser interactions are injected separately."""

    def __init__(
        self,
        port: FlowGenerationPort,
        ledger: D2DailyCreditLedger,
        recovery_journal: AgentSettingsRecoveryJournal,
    ) -> None:
        self.port = port
        self.ledger = ledger
        self.recovery_journal = recovery_journal

    async def execute(self, request: PhaseGExecutionRequest) -> PhaseGExecutionResult:
        """Apply settings, click once, validate one new gate and restore defaults."""
        validate_phase_g_request(request)
        try:
            self.recovery_journal.require_clean_or_missing()
        except RecoveryJournalError as exc:
            raise PhaseGError(
                "PHASE_G_RECOVERY_REQUIRED", "settings recovery must be clean before Phase G"
            ) from exc
        await self.port.bind_request(request)

        # Snapshot all relevant defaults before the journal or first mutation.
        original = dict(await self.port.snapshot_settings())
        if not original:
            raise PhaseGError(
                "PHASE_G_SETTINGS_SNAPSHOT_INVALID", "original settings snapshot is empty"
            )
        target: dict[str, object] = {
            "model_id": request.model_id,
            "generation_mode": request.generation_mode,
            "aspect_ratio": request.aspect_ratio,
            "output_count": request.output_count,
            "confirm_before_generating": "Always",
        }
        if not set(target).issubset(original):
            raise PhaseGError(
                "PHASE_G_SETTINGS_SNAPSHOT_INVALID",
                "original settings snapshot omits a setting Phase G will change",
            )
        self.recovery_journal.begin(
            correlation_id=request.d2_run_id,
            original_settings=original,
            intended_settings=target,
        )
        started = False
        generate_recorded = False
        gate_approved = False
        try:
            self.ledger.create_attempt(request)
            applied = dict(await self.port.apply_settings(target))
            if any(applied.get(key) != value for key, value in target.items()):
                raise PhaseGError(
                    "PHASE_G_SETTINGS_READBACK_MISMATCH", "Flow settings differ from request"
                )
            directive = build_agent_only_directive(
                request.compiled_prompt_text,
                request.presubmit_request.preflight.technical_configuration,
            )
            await self.port.fill_prompt(directive, request.compiled_negative_prompt)
            if await self.port.read_prompt() != directive:
                raise PhaseGError(
                    "PHASE_G_PROMPT_READBACK_MISMATCH",
                    "Flow composer prompt differs from the pinned Agent directive",
                )
            # This baseline is deliberately captured immediately before submit.
            baseline = await self.port.capture_submission_baseline()
            if baseline.project_id != request.provider_project_id:
                raise PhaseGError("PHASE_G_PROJECT_MISMATCH", "baseline belongs to another project")
            if baseline.pending_count:
                raise PhaseGError(
                    "PHASE_G_UNRELATED_GENERATION_ACTIVE", "project already has pending work"
                )
            self.ledger.capture_baseline(request.d2_run_id, baseline)
            self.ledger.mark_generate_action(request.d2_run_id)
            generate_recorded = True
            started = True
            await self.port.click_generate_once()
            gate = await self.port.await_confirmation(baseline)
            if (
                not gate.confirmation_id
                or gate.confirmation_id in baseline.confirmation_ids
                or gate.project_id != request.provider_project_id
            ):
                self._record_no_charge(request)
                raise PhaseGError(
                    "PHASE_G_CONFIRMATION_NOT_ATTRIBUTABLE", "confirmation is stale or mismatched"
                )
            self.ledger.observe_confirmation(
                request.d2_run_id,
                confirmation_id=gate.confirmation_id,
                video_count=gate.video_count,
                credit_cost=gate.credit_cost,
                visible_balance=gate.visible_balance,
            )
            if gate.video_count != 1 or gate.credit_cost is None or gate.credit_cost <= 0:
                self._record_no_charge(request)
                code = (
                    "PHASE_G_VIDEO_COUNT_UNPROVEN"
                    if gate.video_count != 1
                    else "PHASE_G_CREDIT_COST_UNKNOWN"
                )
                raise PhaseGError(code, "confirmation must prove one video and known positive cost")
            try:
                self.ledger.approve_gate(
                    request.d2_run_id,
                    video_count=gate.video_count,
                    credit_cost=gate.credit_cost,
                    visible_balance=gate.visible_balance,
                    confirmation_id=gate.confirmation_id,
                )
            except PhaseGError as exc:
                if exc.code in {
                    "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED",
                    "PHASE_G_VISIBLE_BALANCE_INSUFFICIENT",
                }:
                    self._record_no_charge(request)
                raise
            gate_approved = True
            await self.port.approve_confirmation(gate.confirmation_id)
            evidence = await self.port.observe_attempt(baseline, gate.confirmation_id)
            if evidence.d2_run_id != request.d2_run_id:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED", "provider evidence belongs to another run"
                )
            if evidence.provider_project_id != request.provider_project_id:
                raise PhaseGError(
                    "PHASE_G_PROJECT_MISMATCH", "provider result belongs to another project"
                )
            if evidence.attributed_confirmation_id != gate.confirmation_id:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED",
                    "provider evidence is not tied to this confirmation",
                )
            media_ids = evidence.new_provider_media_ids
            if not media_ids and evidence.provider_media_id:
                media_ids = (evidence.provider_media_id,)
            if len(media_ids) > 1:
                self.ledger.record_state(
                    request.d2_run_id,
                    submission_state="UNEXPECTED_MULTI_OUTPUT",
                    terminal_state="UNEXPECTED_MULTI_OUTPUT",
                    provider_execution_id=evidence.provider_execution_id,
                    provider_media_ids=media_ids,
                    provider_project_id=evidence.provider_project_id,
                    accounted_credit_cost=evidence.accounted_credit_cost,
                )
                started = False
                raise PhaseGError(
                    "UNEXPECTED_MULTI_OUTPUT", "more than one new output is attributable"
                )
            if any(media_id in baseline.media_ids for media_id in media_ids):
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED",
                    "provider media identity existed in the pre-submit baseline",
                )
            if evidence.provider_execution_id in baseline.execution_ids:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED",
                    "provider execution identity existed in the pre-submit baseline",
                )
            if evidence.submission_state == "GENERATION_SUCCEEDED" and len(media_ids) != 1:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED",
                    "success does not identify exactly one new provider media asset",
                )
            if (
                evidence.submission_state in {"SUBMISSION_ACKNOWLEDGED", "GENERATION_PENDING"}
                and evidence.provider_execution_id is None
                and len(media_ids) != 1
            ):
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_ATTRIBUTION_FAILED",
                    "pending attempt has no new provider execution or media identity",
                )
            if evidence.submission_state == "WAF_REJECTED":
                self.ledger.record_state(
                    request.d2_run_id,
                    submission_state="WAF_REJECTED",
                    terminal_state="WAF_REJECTED",
                    provider_execution_id=evidence.provider_execution_id,
                    provider_media_id=evidence.provider_media_id,
                    provider_media_ids=media_ids,
                    provider_project_id=evidence.provider_project_id,
                    accounted_credit_cost=evidence.accounted_credit_cost,
                )
                started = False
                raise PhaseGError(
                    "WAF_REJECTED", "Flow refused unusual activity; no retry is permitted"
                )
            self.ledger.record_state(
                request.d2_run_id,
                submission_state=evidence.submission_state,
                terminal_state=evidence.terminal_state,
                provider_execution_id=evidence.provider_execution_id,
                provider_media_id=evidence.provider_media_id,
                provider_media_ids=media_ids,
                provider_project_id=evidence.provider_project_id,
                accounted_credit_cost=evidence.accounted_credit_cost,
            )
            started = False
            return PhaseGExecutionResult(
                request.d2_run_id,
                evidence.submission_state,
                gate.confirmation_id,
                evidence.provider_execution_id,
                evidence.provider_media_id,
                evidence.provider_project_id,
            )
        except WafRejectionError:
            if generate_recorded:
                self.ledger.record_state(
                    request.d2_run_id,
                    submission_state="WAF_REJECTED",
                    terminal_state="WAF_REJECTED",
                    provider_project_id=request.provider_project_id,
                    accounted_credit_cost=None if gate_approved else 0,
                )
            started = False
            raise
        except BaseException:
            if started and generate_recorded:
                self._record_unknown(request)
            raise
        finally:
            # Failed restoration leaves the journal DIRTY and blocks new work.
            restoring_error: Exception | None = None
            try:
                self.recovery_journal.set_state("RESTORING")
            except Exception as exc:
                restoring_error = exc
            try:
                await self.port.restore_settings(original)
                if dict(await self.port.read_settings()) != original:
                    raise PhaseGError(
                        "PHASE_G_SETTINGS_RESTORE_FAILED", "original settings readback differs"
                    )
                if restoring_error is None:
                    self.recovery_journal.set_state("CLEAN")
            except Exception:
                # Keep DIRTY/RESTORING as durable evidence and block the next run.
                raise
            if restoring_error is not None:
                raise PhaseGError(
                    "PHASE_G_SETTINGS_RESTORE_FAILED",
                    "recovery journal could not record restoration",
                ) from restoring_error

    def _record_no_charge(self, request: PhaseGExecutionRequest) -> None:
        self.ledger.record_state(
            request.d2_run_id,
            submission_state="RECONCILED_NO_CHARGE",
            terminal_state="RECONCILED_NO_CHARGE",
            accounted_credit_cost=0,
            provider_project_id=request.provider_project_id,
        )

    def _record_unknown(self, request: PhaseGExecutionRequest) -> None:
        """Reserve the quoted amount or all remaining budget on ambiguous submit."""
        try:
            rows = {str(row["d2_run_id"]): row for row in self.ledger.attempts()}
            current = rows.get(request.d2_run_id, {})
            if current.get("submission_state") in _TERMINAL_STATES:
                return
            self.ledger.record_state(
                request.d2_run_id,
                submission_state="UNKNOWN_SUBMISSION_STATE",
                provider_project_id=request.provider_project_id,
            )
        except PhaseGError:
            # Do not mask the triggering exception; the recovery journal still
            # records settings as DIRTY if the browser restore cannot be proven.
            return


def validate_phase_g_request(request: PhaseGExecutionRequest) -> None:
    """Require a fresh, exact Phase E match and a persisted APPROVED prompt."""
    presubmit = request.presubmit_request
    phase_e = presubmit.preflight
    prompt = presubmit.prompt_version
    if not _RUN_ID_RE.fullmatch(request.d2_run_id):
        raise PhaseGError("PHASE_G_RUN_ID_INVALID", "D2 run ID is invalid")
    if prompt.prompt_status != "APPROVED":
        raise PhaseGError("PHASE_G_PROMPT_NOT_APPROVED", "prompt version is not APPROVED")
    if not phase_e.ready_for_provider_adapter or phase_e.status != "PREFLIGHT_PASS":
        raise PhaseGError("PHASE_E_PREFLIGHT_REQUIRED", "fresh Phase E preflight did not pass")
    if phase_e.errors:
        raise PhaseGError("PHASE_E_PREFLIGHT_REQUIRED", "fresh Phase E has validation errors")
    identities = (
        (request.scene_id, phase_e.scene_id, prompt.scene_id),
        (
            request.generation_profile_id,
            phase_e.generation_profile_id,
            prompt.generation_profile_id,
        ),
        (request.provider_id, str(phase_e.resolved.get("provider_id", "")), presubmit.provider_id),
        (
            request.provider_account_id,
            str(phase_e.resolved.get("provider_account_id", "")),
            presubmit.provider_account_id,
        ),
        (request.model_id, str(phase_e.resolved.get("model_id", "")), presubmit.model_id),
        (
            request.generation_mode,
            str(phase_e.technical_configuration.get("generation_mode", "")),
            presubmit.generation_mode,
        ),
    )
    if any(len(set(values)) != 1 for values in identities):
        raise PhaseGError("PHASE_G_IDENTITY_MISMATCH", "pinned Phase G identities differ")
    if request.provider_project_id != presubmit.provider_project_id:
        raise PhaseGError("PHASE_G_PROJECT_MISMATCH", "provider project identity differs")
    if request.scene_prompt_id != prompt.scene_prompt_id:
        raise PhaseGError("PHASE_G_PROMPT_MISMATCH", "Scene prompt identity differs")
    if (
        request.compiled_prompt_text != phase_e.compiled_prompt
        or prompt.compiled_prompt_text != phase_e.compiled_prompt
    ):
        raise PhaseGError(
            "PHASE_G_PROMPT_MISMATCH", "compiled prompt text differs from fresh Phase E"
        )
    if (
        request.compiled_negative_prompt != (phase_e.compiled_negative_prompt or "")
        or prompt.compiled_negative_prompt != request.compiled_negative_prompt
    ):
        raise PhaseGError(
            "PHASE_G_PROMPT_MISMATCH", "compiled negative prompt differs from fresh Phase E"
        )
    if (
        request.source_context_sha256 != phase_e.source_context_sha256
        or prompt.source_context_sha256 != request.source_context_sha256
    ):
        raise PhaseGError("PHASE_G_HASH_MISMATCH", "source context hash differs from fresh Phase E")
    if (
        request.prompt_sha256 != phase_e.prompt_sha256
        or prompt.prompt_sha256 != request.prompt_sha256
    ):
        raise PhaseGError("PHASE_G_HASH_MISMATCH", "prompt hash differs from fresh Phase E")
    if not _SHA256_RE.fullmatch(request.source_context_sha256) or not _SHA256_RE.fullmatch(
        request.prompt_sha256
    ):
        raise PhaseGError("PHASE_G_HASH_INVALID", "prompt hash is malformed")
    config = phase_e.technical_configuration
    if (
        config.get("duration_sec") != request.duration_sec
        or config.get("aspect_ratio") != request.aspect_ratio
        or config.get("output_count") != request.output_count
    ):
        raise PhaseGError("PHASE_G_SETTINGS_MISMATCH", "generation settings differ from Phase E")
    if request.generation_mode != "TEXT_TO_VIDEO" or request.output_count != 1:
        raise PhaseGError("PHASE_G_REQUEST_UNSUPPORTED", "D2 requires one text-to-video output")
    if presubmit.references or request.compiled_negative_prompt:
        raise PhaseGError(
            "PHASE_G_REQUEST_UNSUPPORTED",
            "controlled Agent-only D2 requires zero references and an empty negative prompt",
        )
    if not _PROJECT_RE.fullmatch(request.provider_project_id):
        raise PhaseGError("PHASE_G_PROJECT_MISMATCH", "provider project ID is invalid")


class D2DailyCreditLedger:
    """Atomic task-local ledger enforcing a 50-credit Bangkok-day ceiling."""

    def __init__(self, runtime_state_root: Path, *, daily_budget: int = 50) -> None:
        if daily_budget != 50:
            raise PhaseGError("PHASE_G_BUDGET_INVALID", "D2 daily budget is fixed at 50 credits")
        if not runtime_state_root.is_absolute() or runtime_state_root == Path(
            runtime_state_root.anchor
        ):
            raise PhaseGError(
                "PHASE_G_LEDGER_PATH_INVALID", "runtime state root must be absolute and non-root"
            )
        self.root = runtime_state_root / "phase-g"
        self.path = self.root / "d2-credit-ledger.json"
        self.lock_path = self.root / ".d2-credit-ledger.lock"
        self.daily_budget = daily_budget

    def create_attempt(self, request: PhaseGExecutionRequest) -> dict[str, object]:
        """Persist a zero-action attempt before any UI mutation."""
        validate_phase_g_request(request)
        return self._mutate(lambda payload: self._create(payload, request))

    def mark_generate_action(self, d2_run_id: str) -> dict[str, object]:
        """Durably consume the attempt's sole Generate action before clicking."""

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if _counter(attempt.get("generate_action_count")) != 0:
                raise PhaseGError(
                    "PHASE_G_DUPLICATE_SUBMIT_FORBIDDEN",
                    "this attempt already consumed its Generate action",
                )
            if attempt["submission_state"] != "PREPARED":
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_STATE_INVALID",
                    "attempt is not prepared for its one Generate action",
                )
            if attempt.get("baseline_captured") is not True:
                raise PhaseGError(
                    "PHASE_G_BASELINE_REQUIRED", "attempt has no durable pre-submit baseline"
                )
            attempt["generate_action_count"] = 1
            attempt["submission_state"] = "GENERATE_ACTION_RECORDED"
            attempt["updated_at"] = _utc_now()
            return dict(attempt)

        return self._mutate(update)

    def capture_baseline(
        self, d2_run_id: str, baseline: FlowSubmissionBaseline
    ) -> dict[str, object]:
        """Persist the exact project baseline needed to reconcile without resubmitting."""

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if attempt["submission_state"] != "PREPARED" or attempt.get("baseline_captured"):
                raise PhaseGError(
                    "PHASE_G_BASELINE_INVALID", "baseline is stale or already captured"
                )
            if baseline.project_id != attempt["provider_project_id"] or baseline.pending_count:
                raise PhaseGError(
                    "PHASE_G_BASELINE_INVALID", "baseline project or pending state is invalid"
                )
            attempt.update(
                baseline_captured=True,
                baseline_media_ids=sorted(baseline.media_ids),
                baseline_execution_ids=sorted(baseline.execution_ids),
                baseline_pending_count=baseline.pending_count,
                baseline_confirmation_ids=sorted(baseline.confirmation_ids),
                baseline_agent_turn_id=baseline.agent_turn_id,
                updated_at=_utc_now(),
            )
            return dict(attempt)

        return self._mutate(update)

    def approve_gate(
        self,
        d2_run_id: str,
        *,
        video_count: int | None,
        credit_cost: Decimal | int | float | str | None,
        confirmation_id: str,
        visible_balance: Decimal | int | float | str | None = None,
    ) -> dict[str, object]:
        """Validate the new gate and reserve its quote before one approval."""
        quoted = _decimal(credit_cost)
        balance = _decimal(visible_balance, allow_none=True)
        if video_count != 1:
            raise PhaseGError(
                "PHASE_G_VIDEO_COUNT_UNPROVEN", "confirmation must explicitly quote one video"
            )
        if quoted is None or quoted <= 0:
            raise PhaseGError("PHASE_G_CREDIT_COST_UNKNOWN", "confirmation credit cost is unknown")

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if _counter(attempt.get("generate_action_count")) != 1:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_STATE_INVALID", "new gate has no recorded Generate action"
                )
            if _counter(attempt.get("approval_action_count")) != 0:
                raise PhaseGError(
                    "UNEXPECTED_SECOND_CONFIRMATION", "attempt already approved one confirmation"
                )
            if attempt["submission_state"] != "GENERATE_ACTION_RECORDED":
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_STATE_INVALID",
                    "attempt is not awaiting its first confirmation",
                )
            baseline_confirmation_ids = attempt.get("baseline_confirmation_ids")
            if not isinstance(baseline_confirmation_ids, list):
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_STATE_INVALID", "stored baseline confirmations are invalid"
                )
            if (
                not confirmation_id
                or confirmation_id in cast(list[object], baseline_confirmation_ids)
                or attempt.get("observed_confirmation_id") != confirmation_id
                or attempt.get("confirmation_id") is not None
                or attempt.get("observed_video_count") != video_count
                or _decimal(attempt.get("observed_credit_cost"), allow_none=True) != quoted
                or _decimal(attempt.get("observed_visible_balance"), allow_none=True) != balance
            ):
                raise PhaseGError(
                    "PHASE_G_CONFIRMATION_NOT_ATTRIBUTABLE",
                    "confirmation is stale or this attempt already has a gate",
                )
            date = datetime.now(_BANGKOK).date().isoformat()
            cumulative = self._accounted(payload, date)
            if cumulative + quoted > self.daily_budget:
                raise PhaseGError(
                    "DAILY_FREE_CREDIT_BUDGET_EXHAUSTED",
                    "quoted cost exceeds remaining D2 daily budget",
                )
            if balance is not None and quoted > balance:
                raise PhaseGError(
                    "PHASE_G_VISIBLE_BALANCE_INSUFFICIENT",
                    "quoted cost exceeds visible Flow balance",
                )
            attempt.update(
                approval_action_count=1,
                confirmation_id=confirmation_id,
                quoted_video_count=video_count,
                quoted_credit_cost=str(quoted),
                accounted_credit_cost=str(quoted),
                accounting_date=date,
                submission_state="COST_RESERVED_AWAITING_APPROVAL",
                updated_at=_utc_now(),
            )
            return dict(attempt)

        return self._mutate(update)

    def observe_confirmation(
        self,
        d2_run_id: str,
        *,
        confirmation_id: str,
        video_count: int | None,
        credit_cost: Decimal | int | float | str | None,
        visible_balance: Decimal | int | float | str | None,
    ) -> dict[str, object]:
        """Persist a fresh gate quote even when policy later refuses approval."""
        quoted = _decimal(credit_cost, allow_none=True)
        balance = _decimal(visible_balance, allow_none=True)

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            baseline_ids = attempt.get("baseline_confirmation_ids")
            if not isinstance(baseline_ids, list):
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_STATE_INVALID", "stored baseline confirmations are invalid"
                )
            if (
                attempt.get("submission_state") != "GENERATE_ACTION_RECORDED"
                or not confirmation_id
                or confirmation_id in cast(list[object], baseline_ids)
                or attempt.get("observed_confirmation_id") is not None
            ):
                raise PhaseGError(
                    "PHASE_G_CONFIRMATION_NOT_ATTRIBUTABLE",
                    "confirmation is stale or a second gate was observed",
                )
            attempt.update(
                observed_confirmation_id=confirmation_id,
                observed_video_count=video_count,
                observed_credit_cost=str(quoted) if quoted is not None else None,
                observed_visible_balance=str(balance) if balance is not None else None,
                updated_at=_utc_now(),
            )
            return dict(attempt)

        return self._mutate(update)

    def record_state(
        self,
        d2_run_id: str,
        *,
        submission_state: str,
        terminal_state: str | None = None,
        provider_execution_id: str | None = None,
        provider_media_id: str | None = None,
        provider_media_ids: Sequence[str] | None = None,
        provider_project_id: str | None = None,
        accounted_credit_cost: Decimal | int | float | str | None = None,
    ) -> dict[str, object]:
        """Persist sanitized provider evidence and conservatively reserve charges."""
        if submission_state not in _SUBMISSION_STATES:
            raise PhaseGError("PHASE_G_STATE_INVALID", "submission state is not recognized")
        if terminal_state is not None and terminal_state not in _SUBMISSION_STATES:
            raise PhaseGError("PHASE_G_STATE_INVALID", "terminal state is not recognized")
        cost = _decimal(accounted_credit_cost, allow_none=True)

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            current_state = str(attempt["submission_state"])
            if current_state in _TERMINAL_STATES:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_TERMINAL", "terminal D2 attempt cannot be changed"
                )
            allowed_next = {
                "PREPARED": {"PREPARED"},
                "GENERATE_ACTION_RECORDED": {
                    "GENERATE_ACTION_RECORDED",
                    "SUBMISSION_ACKNOWLEDGED",
                    "GENERATION_PENDING",
                    "PROVIDER_REFUSED",
                    "WAF_REJECTED",
                    "UNKNOWN_SUBMISSION_STATE",
                    "RECONCILED_NO_CHARGE",
                },
                "COST_RESERVED_AWAITING_APPROVAL": {
                    "COST_RESERVED_AWAITING_APPROVAL",
                    "SUBMISSION_ACKNOWLEDGED",
                    "GENERATION_PENDING",
                    "GENERATION_SUCCEEDED",
                    "PROVIDER_REFUSED",
                    "WAF_REJECTED",
                    "GENERATION_FAILED",
                    "UNEXPECTED_MULTI_OUTPUT",
                    "MEDIA_VALIDATION_FAILED",
                    "PERSISTENCE_FAILED",
                    "UNKNOWN_SUBMISSION_STATE",
                },
                "SUBMISSION_ACKNOWLEDGED": {
                    "SUBMISSION_ACKNOWLEDGED",
                    "GENERATION_PENDING",
                    "GENERATION_SUCCEEDED",
                    "GENERATION_FAILED",
                    "UNEXPECTED_MULTI_OUTPUT",
                    "MEDIA_VALIDATION_FAILED",
                    "PERSISTENCE_FAILED",
                    "UNKNOWN_SUBMISSION_STATE",
                },
                "GENERATION_PENDING": {
                    "GENERATION_PENDING",
                    "GENERATION_SUCCEEDED",
                    "GENERATION_FAILED",
                    "UNEXPECTED_MULTI_OUTPUT",
                    "MEDIA_VALIDATION_FAILED",
                    "PERSISTENCE_FAILED",
                    "UNKNOWN_SUBMISSION_STATE",
                },
                "UNKNOWN_SUBMISSION_STATE": {
                    "UNKNOWN_SUBMISSION_STATE",
                    "SUBMISSION_ACKNOWLEDGED",
                    "GENERATION_PENDING",
                    "GENERATION_SUCCEEDED",
                    "PROVIDER_REFUSED",
                    "WAF_REJECTED",
                    "GENERATION_FAILED",
                    "UNEXPECTED_MULTI_OUTPUT",
                    "MEDIA_VALIDATION_FAILED",
                    "PERSISTENCE_FAILED",
                    "RECONCILED_NO_CHARGE",
                },
                "GENERATION_SUCCEEDED": {
                    "GENERATION_SUCCEEDED",
                    "D2_SUCCEEDED",
                    "MEDIA_VALIDATION_FAILED",
                    "PERSISTENCE_FAILED",
                },
            }
            if submission_state not in allowed_next.get(current_state, set()):
                raise PhaseGError(
                    "PHASE_G_STATE_TRANSITION_INVALID", "D2 attempt state transition is not allowed"
                )
            if provider_project_id and provider_project_id != attempt["provider_project_id"]:
                raise PhaseGError(
                    "PHASE_G_PROJECT_MISMATCH", "provider evidence names a different project"
                )
            for key, value in (
                ("provider_execution_id", provider_execution_id),
                ("provider_media_id", provider_media_id),
                ("provider_project_id", provider_project_id),
            ):
                if value is not None:
                    attempt[key] = value
            if provider_media_ids is not None:
                if any(not media_id for media_id in provider_media_ids):
                    raise PhaseGError(
                        "PHASE_G_MEDIA_ID_INVALID", "provider media identity is invalid"
                    )
                attempt["provider_media_ids"] = list(provider_media_ids)
            if cost is not None:
                if attempt.get("accounting_date") is None and cost > 0:
                    attempt["accounting_date"] = datetime.now(_BANGKOK).date().isoformat()
                attempt["accounted_credit_cost"] = str(cost)
            if submission_state == "UNKNOWN_SUBMISSION_STATE":
                # Once a submit may have reached Flow, reserve the entire
                # remaining task budget when no trustworthy quote was captured.
                # This blocks accidental same-day paid retries until reconciled.
                date = attempt.get("accounting_date") or datetime.now(_BANGKOK).date().isoformat()
                attempt["accounting_date"] = date
                current = _decimal(attempt.get("accounted_credit_cost")) or Decimal(0)
                if current == 0:
                    already = self._accounted(payload, str(date))
                    attempt["accounted_credit_cost"] = str(
                        max(Decimal(0), Decimal(self.daily_budget) - already)
                    )
            attempt["submission_state"] = submission_state
            attempt["terminal_state"] = terminal_state
            attempt["updated_at"] = _utc_now()
            return dict(attempt)

        return self._mutate(update)

    def attempts(self) -> tuple[Mapping[str, object], ...]:
        """Return a defensive snapshot of the sanitized attempt records."""
        with self._locked_file():
            payload = self._read()
            rows = cast(list[dict[str, object]], payload["attempts"])
            return tuple({key: value for key, value in row.items()} for row in rows)

    def record_artifact_recovered(self, d2_run_id: str) -> Mapping[str, object]:
        """Resume the same run after its exact stored media ID is revalidated."""

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if attempt.get("submission_state") not in {
                "PERSISTENCE_FAILED",
                "MEDIA_VALIDATION_FAILED",
            } or not attempt.get("provider_media_id"):
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "only an already generated media identity can be recovered",
                )
            attempt["submission_state"] = "GENERATION_SUCCEEDED"
            attempt["terminal_state"] = None
            attempt["updated_at"] = _utc_now()
            return dict(attempt)

        return self._mutate(update)

    def record_local_artifact_validated(
        self, d2_run_id: str, *, path: Path, size_bytes: int, sha256: str
    ) -> Mapping[str, object]:
        """Persist the validated local clip identity for later Drive finalization."""
        if (
            not path.is_absolute()
            or not path.is_file()
            or path.is_symlink()
            or isinstance(size_bytes, bool)
            or size_bytes <= 0
            or not _SHA256_RE.fullmatch(sha256)
            or path.stat().st_size != size_bytes
        ):
            raise PhaseGError(
                "PHASE_G_LOCAL_ARTIFACT_INVALID", "validated local artifact identity is invalid"
            )
        actual_sha256 = sha256_file(path)
        if actual_sha256 != sha256:
            raise PhaseGError(
                "PHASE_G_LOCAL_ARTIFACT_INVALID", "validated artifact SHA-256 does not match file"
            )

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if attempt.get("submission_state") != "GENERATION_SUCCEEDED":
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "attributed generation must succeed before recording local media",
                )
            temp_root = Path(str(attempt.get("temp_root", ""))).resolve()
            resolved_path = path.resolve()
            if not resolved_path.is_relative_to(temp_root):
                raise PhaseGError(
                    "PHASE_G_LOCAL_ARTIFACT_INVALID",
                    "validated artifact is outside its D2 temporary root",
                )
            attempt.update(
                local_artifact_path=str(resolved_path),
                local_artifact_size_bytes=size_bytes,
                local_artifact_sha256=sha256,
                updated_at=_utc_now(),
            )
            return dict(attempt)

        return self._mutate(update)

    def accounted_today(self) -> Decimal:
        """Sum reserved or definitively spent D2 credits for the Bangkok date."""
        with self._locked_file():
            return self._accounted(self._read(), datetime.now(_BANGKOK).date().isoformat())

    def mark_artifact_verified(
        self,
        d2_run_id: str,
        *,
        remote_download: Path,
        remote_file_id: str,
        remote_filename: str,
    ) -> Mapping[str, object]:
        """Verify the downloaded remote copy itself before completing D2."""

        def update(payload: dict[str, object]) -> dict[str, object]:
            attempt = self._attempt(payload, d2_run_id)
            if attempt.get("submission_state") != "GENERATION_SUCCEEDED":
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "exact provider output must be generated before artifact completion",
                )
            if not attempt.get("provider_media_id"):
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "exact provider media identity is required before artifact completion",
                )
            local_path_value = attempt.get("local_artifact_path")
            temp_root_value = attempt.get("temp_root")
            if not isinstance(local_path_value, str) or not isinstance(temp_root_value, str):
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "validated local artifact path is required before remote verification",
                )
            local_candidate = Path(local_path_value)
            temp_root = Path(temp_root_value).resolve()
            if local_candidate.is_symlink() or not local_candidate.is_file():
                raise PhaseGError(
                    "PHASE_G_LOCAL_ARTIFACT_INVALID",
                    "validated local artifact is missing or unsafe",
                )
            local_path = local_candidate.resolve()
            if not local_path.is_relative_to(temp_root):
                raise PhaseGError(
                    "PHASE_G_LOCAL_ARTIFACT_INVALID",
                    "validated local artifact escaped its D2 temp root",
                )
            try:
                evidence = verify_remote_redownload(
                    local_path,
                    remote_download,
                    remote_file_id=remote_file_id,
                    remote_filename=remote_filename,
                )
            except AssetVerificationError as exc:
                raise PhaseGError(
                    "PHASE_G_REMOTE_VERIFICATION_FAILED",
                    "remote re-download could not be verified against the local artifact",
                ) from exc
            if evidence.size_bytes != attempt.get(
                "local_artifact_size_bytes"
            ) or evidence.local_sha256 != attempt.get("local_artifact_sha256"):
                raise PhaseGError(
                    "PHASE_G_REMOTE_VERIFICATION_FAILED",
                    "remote verification differs from the recorded validated local artifact",
                )
            attempt.update(
                remote_file_id=evidence.remote_file_id,
                remote_filename=evidence.remote_filename,
                remote_size_bytes=evidence.size_bytes,
                local_sha256=evidence.local_sha256,
                remote_redownload_sha256=evidence.remote_sha256,
                remote_verified=True,
                submission_state="D2_SUCCEEDED",
                terminal_state="D2_SUCCEEDED",
                updated_at=_utc_now(),
            )
            return dict(attempt)

        return self._mutate(update)

    def _create(
        self, payload: dict[str, object], request: PhaseGExecutionRequest
    ) -> dict[str, object]:
        attempts = cast(list[dict[str, object]], payload["attempts"])
        if any(row["d2_run_id"] == request.d2_run_id for row in attempts):
            raise PhaseGError("PHASE_G_DUPLICATE_RUN_ID", "D2 run ID already exists in the ledger")
        for previous in attempts:
            state = previous.get("submission_state")
            if state == "WAF_REJECTED":
                raise PhaseGError(
                    "WAF_REJECTED", "D2 live testing stopped after provider WAF refusal"
                )
            if state == "D2_SUCCEEDED":
                raise PhaseGError(
                    "PHASE_G_SUCCESS_ALREADY_PROVEN",
                    "D2 stops after its first verified artifact",
                )
            if state in {
                "UNKNOWN_SUBMISSION_STATE",
                "GENERATION_PENDING",
                "SUBMISSION_ACKNOWLEDGED",
            }:
                raise PhaseGError(
                    "PHASE_G_ATTEMPT_UNRESOLVED", "prior D2 attempt requires reconciliation"
                )
            if state == "PERSISTENCE_FAILED":
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "recover the already generated artifact before another submit",
                )
            if state == "GENERATION_SUCCEEDED":
                raise PhaseGError(
                    "PHASE_G_ARTIFACT_RECOVERY_REQUIRED",
                    "validate and persist the generated artifact before another submit",
                )
            generate_count = previous.get("generate_action_count", 0)
            if isinstance(generate_count, bool) or not isinstance(generate_count, int):
                raise PhaseGError(
                    "PHASE_G_LEDGER_INVALID", "stored Generate action count is invalid"
                )
            if generate_count > 0:
                raise PhaseGError(
                    "PHASE_G_PAID_ATTEMPT_ALREADY_STARTED",
                    "the authorized D2 live attempt has already used its Generate action",
                )
            if state not in _TERMINAL_STATES:
                raise PhaseGError("PHASE_G_ATTEMPT_UNRESOLVED", "prior D2 attempt is not terminal")
        now = _utc_now()
        attempt: dict[str, object] = {
            "d2_run_id": request.d2_run_id,
            "scene_id": request.scene_id,
            "scene_prompt_id": request.scene_prompt_id,
            "generation_profile_id": request.generation_profile_id,
            "provider_id": request.provider_id,
            "provider_account_id": request.provider_account_id,
            "provider_project_id": request.provider_project_id,
            "model_id": request.model_id,
            "generation_mode": request.generation_mode,
            "source_context_sha256": request.source_context_sha256,
            "prompt_sha256": request.prompt_sha256,
            "requested_duration_sec": request.duration_sec,
            "requested_aspect_ratio": request.aspect_ratio,
            "requested_output_count": request.output_count,
            "temp_root": str(request.presubmit_request.temp_root.resolve()),
            "started_at": now,
            "started_bangkok_date": datetime.now(_BANGKOK).date().isoformat(),
            "generate_action_count": 0,
            "quoted_video_count": None,
            "quoted_credit_cost": None,
            "approval_action_count": 0,
            "observed_confirmation_id": None,
            "observed_video_count": None,
            "observed_credit_cost": None,
            "observed_visible_balance": None,
            "baseline_captured": False,
            "baseline_media_ids": [],
            "baseline_execution_ids": [],
            "baseline_pending_count": None,
            "baseline_confirmation_ids": [],
            "baseline_agent_turn_id": None,
            "confirmation_id": None,
            "accounting_date": None,
            "accounted_credit_cost": "0",
            "submission_state": "PREPARED",
            "provider_execution_id": None,
            "provider_media_id": None,
            "provider_media_ids": [],
            "terminal_state": None,
            "updated_at": now,
        }
        attempts.append(attempt)
        return dict(attempt)

    def _accounted(self, payload: Mapping[str, object], date: str) -> Decimal:
        total = Decimal(0)
        for attempt in cast(list[dict[str, object]], payload["attempts"]):
            if attempt.get("accounting_date") == date:
                total += _decimal(attempt.get("accounted_credit_cost")) or Decimal(0)
        return total

    @staticmethod
    def _attempt(payload: dict[str, object], d2_run_id: str) -> dict[str, object]:
        for row in cast(list[dict[str, object]], payload["attempts"]):
            if row.get("d2_run_id") == d2_run_id:
                return row
        raise PhaseGError(
            "PHASE_G_ATTEMPT_NOT_FOUND", "D2 attempt does not exist in the local ledger"
        )

    def _mutate(
        self, operation: Callable[[dict[str, object]], dict[str, object]]
    ) -> dict[str, object]:
        with self._locked_file():
            payload = self._read()
            result = operation(payload)
            self._write(payload)
            return result

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {
                "schema_version": _LEDGER_SCHEMA,
                "daily_budget": self.daily_budget,
                "attempts": [],
            }
        if self.path.is_symlink():
            raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "ledger may not be a symlink")
        try:
            payload_value: object = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PhaseGError("PHASE_G_LEDGER_CORRUPT", "D2 credit ledger is unreadable") from exc
        if not isinstance(payload_value, dict):
            raise PhaseGError("PHASE_G_LEDGER_CORRUPT", "D2 credit ledger has an invalid shape")
        payload = cast(dict[str, object], payload_value)
        if set(payload) != {
            "schema_version",
            "daily_budget",
            "attempts",
        }:
            raise PhaseGError("PHASE_G_LEDGER_CORRUPT", "D2 credit ledger has an invalid shape")
        if (
            payload["schema_version"] != _LEDGER_SCHEMA
            or payload["daily_budget"] != self.daily_budget
            or not isinstance(payload["attempts"], list)
        ):
            raise PhaseGError(
                "PHASE_G_LEDGER_CORRUPT", "D2 credit ledger version or budget is invalid"
            )
        return payload

    def _write(self, payload: dict[str, object]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            root = self.root.resolve(strict=True)
            if root == Path(root.anchor) or self.path.resolve(strict=False).parent != root:
                raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "D2 ledger path escaped its root")
            fd, temp_name = tempfile.mkstemp(prefix=".d2-ledger-", suffix=".tmp", dir=root)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                    json.dump(
                        payload,
                        stream,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    )
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_name, self.path)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        except PhaseGError:
            raise
        except (OSError, TypeError, ValueError) as exc:
            raise PhaseGError(
                "PHASE_G_LEDGER_WRITE_FAILED", "D2 credit ledger could not be saved"
            ) from exc

    def _locked_file(self):
        return _LedgerFileLock(self)


class _LedgerFileLock:
    """Cross-process lock for the single append-only D2 evidence file."""

    def __init__(self, ledger: D2DailyCreditLedger) -> None:
        self.ledger = ledger
        self.fd: int | None = None

    def __enter__(self):
        self.ledger.root.mkdir(parents=True, exist_ok=True)
        if self.ledger.root.is_symlink() or self.ledger.lock_path.is_symlink():
            raise PhaseGError(
                "PHASE_G_LEDGER_PATH_INVALID", "D2 ledger lock path may not be a symlink"
            )
        self.fd = os.open(self.ledger.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if os.name == "nt":
            import msvcrt

            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b"0")
            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_args: object) -> None:
        if self.fd is not None:
            if os.name == "nt":
                import msvcrt

                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


def _decimal(value: object, *, allow_none: bool = False) -> Decimal | None:
    """Parse a finite non-negative credit count without rounding."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() and result >= 0 else None


def _counter(value: object) -> int:
    """Read a non-negative integer action count from persisted JSON."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PhaseGError("PHASE_G_LEDGER_CORRUPT", "D2 action count is invalid")
    return value


def _utc_now() -> str:
    return format_utc_timestamp(datetime.now(UTC), timespec="seconds")
