"""Concrete, isolated Agent-only Flow adapter for the Phase G boundary.

This module composes the repository-owned profile/browser port. It captures
provider evidence without retaining response bodies, signed URLs, or session data.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlparse

from gflow_cli.api.transports.batchexecute import (
    generation_record,
    parse_frames,
    rpc_errors,
)
from gflow_cli.api.transports.migrated_recover import RecoveredClip, recover_clip
from gflow_cli.errors import ProfileLockedError, WafRejectionError
from gflow_cli.profile_lease import ProfileLease

from .errors import FlowPresubmitError
from .live_flow import GoogleFlowAgentOnlyPort
from .phase_g import (
    FlowConfirmationGate,
    FlowSubmissionBaseline,
    PhaseGAttemptEvidence,
    PhaseGError,
    PhaseGExecutionRequest,
)

if TYPE_CHECKING:
    from playwright.async_api import Page, Response


_TEST_PROJECT_ID = "34e60953-e534-46e9-ac65-993893e61065"
_TEST_PROJECT_NAME = "AI Video Automation V2 - TEST"
_TEST_AUTH_PROFILE = "GFLOW_V2_TEST_AUTH"
_MODEL_DISPLAY_NAMES = {"MDL_0001": "Veo 3.1 - Lite"}
_FLOW_ORIGIN = "https://flow.google.com"
_VIDEO_STATE_JS = r"""
async () => {
  const read = (root) => {
    const values = [];
    const uuid = /[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/ig;
    for (const el of [root, ...root.querySelectorAll('*')]) {
      for (const attr of ['src', 'href', 'data-media-id', 'data-id']) {
        const value = el.getAttribute?.(attr) || '';
        for (const match of value.matchAll(uuid)) values.push(match[0].toLowerCase());
      }
      for (const match of (el.textContent || '').matchAll(uuid)) {
        values.push(match[0].toLowerCase());
      }
    }
    return values;
  };
  const tiles = [...document.querySelectorAll('flow-video-tile')];
  const finished = tiles.filter(tile => !tile.querySelector('flow-pending-tile'));
  const chat = [...document.querySelectorAll('flow-chat-bubble')];
  return {
    pending: document.querySelectorAll('flow-video-tile flow-pending-tile').length,
    media_ids: [...new Set([...tiles, ...document.querySelectorAll('flow-a2ui-video-option')]
      .flatMap(read))],
    finished_media_ids: [...new Set(finished.flatMap(read))],
    finished_keys: finished.map(tile => {
      const video = tile.querySelector('video');
      const image = tile.querySelector('img');
      return video ? (video.currentSrc || video.src || '')
        : image ? (image.currentSrc || image.src || '') : '';
    }),
    chat_count: chat.length,
    chat_tail_hash: chat.length
      ? [...new Uint8Array(await crypto.subtle.digest(
          'SHA-256', new TextEncoder().encode(chat[chat.length - 1].outerHTML)))].map(
          value => value.toString(16).padStart(2, '0')).join('')
      : '',
    chat_media_ids: chat.length ? [...new Set(read(chat[chat.length - 1]))] : [],
  };
}
"""
_LIVE_GATE = "flow-permission-message:has(div[role='radio'].option-row:not(.read-only))"
_APPROVE_ROW = "div[role='radio'].option-row:not(.read-only):has(mat-icon:text-is('check'))"
_UUID_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
_VIDEO_QUOTE_RE = re.compile(r"\b(\d+)\s+videos?\b", re.I)
_CREDIT_QUOTE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s+credits?\b", re.I)
_AMBIGUOUS_CREDIT_RANGE_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:or|/|to)\s*\d+(?:[.,]\d+)?\s+credits?\b", re.I
)
_VIDEO_RPCS = frozenset({"YhhmEf", "eb1hJf", "MZZa6b", "jwpduf", "as29s"})


@dataclass(frozen=True, slots=True)
class _ObservedRecord:
    workflow_id: str
    project_id: str
    media_id: str
    status: int | None

    @property
    def is_done(self) -> bool:
        return self.status == 3

    @property
    def is_failed(self) -> bool:
        return self.status is not None and self.status not in {2, 3, 6}


def parse_confirmation_quote(text: str) -> tuple[int | None, Decimal | None]:
    """Read one explicit English video-count and credit quote, or return unknowns."""
    if _AMBIGUOUS_CREDIT_RANGE_RE.search(text):
        return None, None
    video_counts = _VIDEO_QUOTE_RE.findall(text)
    costs = _CREDIT_QUOTE_RE.findall(text)
    if len(video_counts) != 1 or len(costs) != 1:
        return None, None
    try:
        count = int(video_counts[0])
        cost = Decimal(costs[0].replace(",", "."))
    except (ValueError, ArithmeticError):
        return None, None
    if count <= 0 or not cost.is_finite() or cost <= 0:
        return None, None
    return count, cost


def _gate_digest(markup: str) -> str:
    """Fingerprint a gate without persisting its prompt or raw markup."""
    return hashlib.sha256(markup.encode("utf-8")).hexdigest()


class GoogleFlowAgentOnlyGenerationPort:
    """One-request Flow port pinned to the dedicated V2 TEST account and project."""

    def __init__(self, base: GoogleFlowAgentOnlyPort, lease: ProfileLease) -> None:
        self._base = base
        self._lease = lease
        self._request: PhaseGExecutionRequest | None = None
        self._baseline: FlowSubmissionBaseline | None = None
        self._records: dict[tuple[str, str, str], _ObservedRecord] = {}
        self._baseline_head_key = ""
        self._response_tasks: set[asyncio.Task[None]] = set()
        self._waf_rejected = False
        self._generate_clicked = False
        self._approved_gate_id: str | None = None
        self._page: Page | None = None

    @classmethod
    async def open(cls, *, expected_email: str | None = None) -> GoogleFlowAgentOnlyGenerationPort:
        """Open only the repository-owned TEST profile; never launch personal Chrome."""
        base = await GoogleFlowAgentOnlyPort.open(
            auth_profile_key=_TEST_AUTH_PROFILE,
            provider_account_id="PVA_0001",
            provider_project_id=_TEST_PROJECT_ID,
            project_name=_TEST_PROJECT_NAME,
            model_display_names=_MODEL_DISPLAY_NAMES,
            expected_email=expected_email,
            headless=False,
        )
        lease = ProfileLease(base.profile_dir)
        try:
            await lease.aacquire()
        except ProfileLockedError as exc:
            await base.close()
            raise FlowPresubmitError(
                "FLOW_LANE_BUSY", "dedicated Flow profile is already in use"
            ) from exc
        return cls(base, lease)

    async def close(self) -> None:
        """Detach response observation and close the isolated repository browser."""
        if self._page is not None:
            self._page.remove_listener("response", self._on_response)
        if self._response_tasks:
            await asyncio.gather(*tuple(self._response_tasks), return_exceptions=True)
        try:
            await self._base.close()
        finally:
            self._lease.release()

    async def bind_request(self, request: PhaseGExecutionRequest) -> None:
        """Pin this live adapter to exactly the approved TEST job identity."""
        if self._request is not None or self._generate_clicked:
            raise PhaseGError("PHASE_G_PORT_ALREADY_BOUND", "Flow port is already bound")
        if (
            request.provider_account_id != "PVA_0001"
            or request.provider_project_id != _TEST_PROJECT_ID
            or request.model_id != "MDL_0001"
            or request.generation_profile_id != "GPR_0001"
            or request.scene_id != "SCN_000001"
            or request.generation_mode != "TEXT_TO_VIDEO"
            or request.output_count != 1
        ):
            raise PhaseGError("PHASE_G_TEST_IDENTITY_MISMATCH", "request is outside D2 TEST scope")
        session = await self._base.inspect_session()
        if session.status != "FLOW_SESSION_READY" or session.account_id != "PVA_0001":
            raise FlowPresubmitError("FLOW_AUTH_NOT_READY", "TEST profile readiness is unverified")
        project = await self._base.select_project(_TEST_PROJECT_ID)
        if project.project_id != _TEST_PROJECT_ID:
            raise PhaseGError("PHASE_G_PROJECT_MISMATCH", "dedicated TEST project is not selected")
        if await self._base.inspect_surface() != "AGENT_ONLY":
            raise PhaseGError("PHASE_G_SURFACE_MISMATCH", "Flow surface is not Agent-only")
        self._request = request
        self._page = await self._base.active_page()
        self._page.on("response", self._on_response)

    async def snapshot_settings(self) -> Mapping[str, object]:
        return await self._base.snapshot_settings()

    async def apply_settings(self, settings: Mapping[str, object]) -> Mapping[str, object]:
        return await self._base.apply_settings(settings)

    async def read_settings(self) -> Mapping[str, object]:
        return await self._base.snapshot_settings()

    async def restore_settings(self, settings: Mapping[str, object]) -> None:
        await self._base.restore_settings(settings)

    async def fill_prompt(self, prompt: str, negative_prompt: str) -> None:
        if negative_prompt:
            raise PhaseGError(
                "PHASE_G_NEGATIVE_PROMPT_UNSUPPORTED", "D2 requires an empty negative prompt"
            )
        await self._base.fill_prompt(prompt)

    async def read_prompt(self) -> str | None:
        return (await self._base.readback()).prompt

    async def capture_submission_baseline(self) -> FlowSubmissionBaseline:
        page = self._require_page()
        request = self._require_request()
        selected = await self._base.select_project(request.provider_project_id)
        if selected.project_id != request.provider_project_id:
            raise PhaseGError("PHASE_G_PROJECT_MISMATCH", "TEST project changed before submission")
        state = await page.evaluate(_VIDEO_STATE_JS)
        if not isinstance(state, dict):
            raise PhaseGError("PHASE_G_BASELINE_UNREADABLE", "Flow project baseline is unreadable")
        state = cast(dict[str, object], state)
        gate_ids = await self._confirmation_ids()
        chat_ids = state.get("chat_media_ids", [])
        media_ids = set(self._valid_ids(state.get("media_ids")))
        media_ids.update(self._valid_ids(chat_ids))
        media_ids.update(record.media_id.lower() for record in self._records.values())
        execution_ids = {record.workflow_id.lower() for record in self._records.values()}
        pending = self._integer(state.get("pending"))
        baseline = FlowSubmissionBaseline(
            request.provider_project_id,
            frozenset(media_ids),
            frozenset(execution_ids),
            pending,
            frozenset(gate_ids),
            f"{self._integer(state.get('chat_count'))}:{state.get('chat_tail_hash') or ''}",
        )
        self._baseline = baseline
        self._baseline_head_key = self._head_key(state.get("finished_keys"))
        return baseline

    async def click_generate_once(self) -> None:
        """Click the single enabled Agent start control at most once."""
        if self._generate_clicked:
            raise PhaseGError(
                "PHASE_G_DUPLICATE_SUBMIT_FORBIDDEN", "this Flow port already clicked Generate"
            )
        page = self._require_page()
        request = self._require_request()
        parsed = urlparse(page.url)
        if (
            parsed.netloc != "flow.google.com"
            or parsed.path != f"/project/{request.provider_project_id}"
        ):
            raise PhaseGError(
                "PHASE_G_PROJECT_MISMATCH", "Flow navigated away from the TEST project"
            )
        if await self._base.inspect_surface() != "AGENT_ONLY":
            raise PhaseGError(
                "PHASE_G_SURFACE_MISMATCH", "Agent-only surface changed before Generate"
            )
        submit = page.locator('button[aria-label="Start generation"]')
        if (
            await submit.count() != 1
            or not await submit.is_visible()
            or not await submit.is_enabled()
        ):
            raise PhaseGError(
                "PHASE_G_GENERATE_CONTROL_UNPROVEN", "one enabled Generate control is required"
            )
        if self._baseline is None:
            raise PhaseGError(
                "PHASE_G_BASELINE_REQUIRED", "submission baseline must precede Generate"
            )
        self._generate_clicked = True
        await submit.click(timeout=5_000)

    async def await_confirmation(self, baseline: FlowSubmissionBaseline) -> FlowConfirmationGate:
        """Wait for exactly one new gate and parse its count/cost without approving it."""
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            await self._raise_wire_refusal()
            gates = self._require_page().locator(_LIVE_GATE)
            count = await gates.count()
            new_count = count - len(baseline.confirmation_ids)
            if new_count > 1:
                raise PhaseGError(
                    "UNEXPECTED_SECOND_CONFIRMATION", "more than one new Flow gate appeared"
                )
            if new_count == 1:
                gate = gates.last
                markup = await gate.evaluate("el => el.outerHTML")
                gate_id = _gate_digest(markup)
                if gate_id in baseline.confirmation_ids:
                    raise PhaseGError(
                        "PHASE_G_STALE_CONFIRMATION", "Flow gate matches the baseline"
                    )
                text = await gate.inner_text()
                video_count, cost = parse_confirmation_quote(text)
                self._approved_gate_id = gate_id
                return FlowConfirmationGate(gate_id, baseline.project_id, video_count, cost, None)
            await asyncio.sleep(0.25)
        await self._raise_wire_refusal()
        raise PhaseGError("PHASE_G_CONFIRMATION_TIMEOUT", "no new Flow confirmation gate appeared")

    async def approve_confirmation(self, confirmation_id: str) -> None:
        """Approve only the one-time choice after revalidating gate identity and quote."""
        if self._approved_gate_id != confirmation_id:
            raise PhaseGError("PHASE_G_STALE_CONFIRMATION", "gate identity changed before approval")
        page = self._require_page()
        gates = page.locator(_LIVE_GATE)
        if await gates.count() != 1:
            raise PhaseGError(
                "UNEXPECTED_SECOND_CONFIRMATION", "Flow gate count changed before approval"
            )
        gate = gates.first
        markup = await gate.evaluate("el => el.outerHTML")
        if _gate_digest(markup) != confirmation_id:
            raise PhaseGError("PHASE_G_STALE_CONFIRMATION", "Flow gate changed before approval")
        count, cost = parse_confirmation_quote(await gate.inner_text())
        if count != 1 or cost is None:
            raise PhaseGError(
                "PHASE_G_CREDIT_GATE_UNPROVEN", "gate no longer proves one video and cost"
            )
        choices = gate.locator(_APPROVE_ROW)
        if await choices.count() != 2:
            raise PhaseGError(
                "PHASE_G_APPROVAL_CONTROL_UNPROVEN", "one-time approval choice is ambiguous"
            )
        self._approved_gate_id = None
        await choices.first.click(timeout=5_000)

    async def observe_attempt(
        self, baseline: FlowSubmissionBaseline, confirmation_id: str
    ) -> PhaseGAttemptEvidence:
        """Observe only a new TEST-project media ID tied to the new Agent turn."""
        request = self._require_request()
        if self._approved_gate_id is not None:
            raise PhaseGError("PHASE_G_GATE_NOT_APPROVED", "new gate was not approved")
        deadline = time.monotonic() + 240
        peak_pending = 0
        while time.monotonic() < deadline:
            await self._raise_wire_refusal()
            page = self._require_page()
            state = await page.evaluate(_VIDEO_STATE_JS)
            if not isinstance(state, dict):
                await asyncio.sleep(1)
                continue
            state = cast(dict[str, object], state)
            pending = self._integer(state.get("pending"))
            peak_pending = max(peak_pending, pending)
            wire_new = [
                record
                for record in self._records.values()
                if record.media_id.lower() not in baseline.media_ids
                or record.workflow_id.lower() not in baseline.execution_ids
            ]
            for record in wire_new:
                if record.project_id != request.provider_project_id:
                    raise PhaseGError(
                        "PHASE_G_PROJECT_MISMATCH", "new wire record belongs to another project"
                    )
            wire_failed = [record for record in wire_new if record.is_failed]
            if wire_failed:
                return PhaseGAttemptEvidence(
                    request.d2_run_id,
                    "GENERATION_FAILED",
                    provider_execution_id=wire_failed[0].workflow_id,
                    provider_media_id=wire_failed[0].media_id,
                    provider_project_id=request.provider_project_id,
                    terminal_state="GENERATION_FAILED",
                    attributed_confirmation_id=confirmation_id,
                )
            wire_done = [record for record in wire_new if record.is_done]
            if len({record.media_id.lower() for record in wire_done}) > 1:
                ids = tuple(sorted({record.media_id.lower() for record in wire_done}))
                return PhaseGAttemptEvidence(
                    request.d2_run_id,
                    "UNEXPECTED_MULTI_OUTPUT",
                    provider_execution_id=wire_done[0].workflow_id,
                    provider_project_id=request.provider_project_id,
                    terminal_state="UNEXPECTED_MULTI_OUTPUT",
                    attributed_confirmation_id=confirmation_id,
                    new_provider_media_ids=ids,
                )
            media_ids = set(self._valid_ids(state.get("media_ids")))
            media_ids.update(self._valid_ids(state.get("chat_media_ids")))
            media_ids.difference_update(baseline.media_ids)
            if wire_done:
                media_ids.add(wire_done[0].media_id.lower())
            baseline_chat_count, _, baseline_chat_hash = (baseline.agent_turn_id or "0:").partition(
                ":"
            )
            new_turn = (
                self._integer(state.get("chat_count")) > int(baseline_chat_count or "0")
                and str(state.get("chat_tail_hash") or "") != baseline_chat_hash
            )
            finished_ids = set(self._valid_ids(state.get("finished_media_ids")))
            head_changed = self._head_key(state.get("finished_keys")) != self._baseline_head_key
            if len(media_ids) > 1:
                ids = tuple(sorted(media_ids))
                return PhaseGAttemptEvidence(
                    request.d2_run_id,
                    "UNEXPECTED_MULTI_OUTPUT",
                    provider_execution_id=wire_done[0].workflow_id if wire_done else None,
                    provider_project_id=request.provider_project_id,
                    terminal_state="UNEXPECTED_MULTI_OUTPUT",
                    attributed_confirmation_id=confirmation_id,
                    new_provider_media_ids=ids,
                )
            if (
                len(media_ids) == 1
                and not pending
                and new_turn
                and (wire_done or peak_pending > 0)
                and (next(iter(media_ids)) in finished_ids or head_changed)
            ):
                media_id = next(iter(media_ids))
                workflow_id = next(
                    (
                        record.workflow_id
                        for record in wire_done
                        if record.media_id.lower() == media_id
                    ),
                    None,
                )
                return PhaseGAttemptEvidence(
                    request.d2_run_id,
                    "GENERATION_SUCCEEDED",
                    provider_execution_id=workflow_id,
                    provider_media_id=media_id,
                    provider_project_id=request.provider_project_id,
                    terminal_state="GENERATION_SUCCEEDED",
                    attributed_confirmation_id=confirmation_id,
                    new_provider_media_ids=(media_id,),
                )
            await asyncio.sleep(1)
        wire_pending = [
            record
            for record in self._records.values()
            if (
                record.media_id.lower() not in baseline.media_ids
                or record.workflow_id.lower() not in baseline.execution_ids
            )
        ]
        if wire_pending:
            record = wire_pending[0]
            return PhaseGAttemptEvidence(
                request.d2_run_id,
                "GENERATION_PENDING" if record.status in {2, 6} else "SUBMISSION_ACKNOWLEDGED",
                provider_execution_id=record.workflow_id,
                provider_media_id=record.media_id,
                provider_project_id=record.project_id,
                attributed_confirmation_id=confirmation_id,
            )
        return PhaseGAttemptEvidence(
            request.d2_run_id,
            "UNKNOWN_SUBMISSION_STATE",
            provider_project_id=request.provider_project_id,
            attributed_confirmation_id=confirmation_id,
        )

    async def download_exact_media(self, temp_root: Path, media_id: str) -> RecoveredClip:
        """Retrieve only the ID proven by the current request into its job temp dir."""
        request = self._require_request()
        if not _UUID_RE.fullmatch(media_id) or not temp_root.is_absolute():
            raise PhaseGError("PHASE_G_MEDIA_ID_INVALID", "exact media ID or temp root is invalid")
        destination = temp_root / request.d2_run_id
        return await recover_clip(
            self._require_page(),
            project_id=request.provider_project_id,
            media_id=media_id,
            out_dir=destination,
        )

    def _on_response(self, response: Response) -> None:
        """Parse only sanitized record fields; do not retain response bodies or signed URLs."""
        parsed = urlparse(response.url)
        if parsed.netloc != "flow.google.com" or "batchexecute" not in parsed.path:
            return
        task = asyncio.create_task(self._capture_response(response))
        self._response_tasks.add(task)
        task.add_done_callback(self._response_tasks.discard)

    async def _capture_response(self, response: Response) -> None:
        try:
            body = await response.text()
        except Exception:
            return
        for error in rpc_errors(body):
            if "PUBLIC_ERROR_UNUSUAL_ACTIVITY" in error.reasons:
                self._waf_rejected = True
        request = self._request
        if request is None:
            return
        requested_rpcs = set(parse_qs(urlparse(response.url).query).get("rpcids", []))
        for rpcid, payload in parse_frames(body):
            if rpcid not in _VIDEO_RPCS and requested_rpcs.isdisjoint(_VIDEO_RPCS):
                continue
            try:
                record = generation_record(rpcid, payload)
            except Exception:
                continue
            key = (record.workflow_id.lower(), record.project_id.lower(), record.media_id.lower())
            self._records[key] = _ObservedRecord(
                record.workflow_id, record.project_id, record.media_id, record.status
            )

    async def _raise_wire_refusal(self) -> None:
        tasks = tuple(self._response_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._waf_rejected:
            raise WafRejectionError(
                detail="Flow returned PUBLIC_ERROR_UNUSUAL_ACTIVITY for this submission",
                status=403,
                route="batchexecute:FlowAgent",
                retryable=False,
                remediation_hint=(
                    "Stop Phase G live testing for this task/day; do not retry or rotate profiles."
                ),
            )

    async def _confirmation_ids(self) -> set[str]:
        gates = self._require_page().locator(_LIVE_GATE)
        result: set[str] = set()
        for index in range(await gates.count()):
            markup = await gates.nth(index).evaluate("el => el.outerHTML")
            result.add(_gate_digest(markup))
        return result

    def _require_page(self) -> Page:
        if self._page is None:
            raise PhaseGError("PHASE_G_PORT_NOT_READY", "TEST browser context is not open")
        return self._page

    def _require_request(self) -> PhaseGExecutionRequest:
        if self._request is None:
            raise PhaseGError("PHASE_G_REQUEST_NOT_BOUND", "pinned request has not been bound")
        return self._request

    @staticmethod
    def _integer(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    @staticmethod
    def _head_key(values: object) -> str:
        if not isinstance(values, list) or not values:
            return ""
        head = cast(list[object], values)[0]
        return hashlib.sha256(head.encode("utf-8")).hexdigest() if isinstance(head, str) else ""

    @staticmethod
    def _valid_ids(values: object) -> set[str]:
        if not isinstance(values, list):
            return set()
        return {
            value.lower()
            for value in cast(list[object], values)
            if isinstance(value, str) and _UUID_RE.fullmatch(value)
        }
