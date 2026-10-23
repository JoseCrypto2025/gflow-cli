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
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlparse, urlsplit

from gflow_cli.api.transports.batchexecute import (
    TEXT_TO_VIDEO_SUBMIT_RPC,
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
    PhaseGSubmitTransportEvidence,
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
  const source = (element) => element ? (element.currentSrc || element.src || '') : '';
  const tileIdentity = (value) => {
    let url;
    try { url = new URL(value); } catch { return null; }
    if (url.origin === 'https://flow-content.google') {
      const mediaPattern = /^\/(?:video|image)\/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})$/i;
      const media = mediaPattern.exec(url.pathname);
      return media ? `media:${media[1].toLowerCase()}` : null;
    }
    if (url.hostname === 'lh3.googleusercontent.com' || url.hostname === 'flow.google.com') {
      const poster = /^\/asb\/([A-Za-z0-9_-]+)$/.exec(url.pathname);
      return poster ? `asb:${poster[1]}` : null;
    }
    return null;
  };
  const mediaId = (value) => {
    let url;
    try { url = new URL(value); } catch { return null; }
    const mediaPattern = /^\/(?:video|image)\/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})$/i;
    const match = mediaPattern.exec(url.pathname);
    return url.origin === 'https://flow-content.google' && match
      ? match[1].toLowerCase()
      : null;
  };
  const digest = async (value) => {
    if (!value) return null;
    const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
    return [...new Uint8Array(bytes)].map(part => part.toString(16).padStart(2, '0')).join('');
  };
  const tiles = [...document.querySelectorAll('flow-video-tile')];
  const finished = tiles.filter(tile => !tile.querySelector('flow-pending-tile'));
  const chat = [...document.querySelectorAll('flow-chat-bubble')];
  const videoOptions = [...document.querySelectorAll('flow-a2ui-video-option')].map(option => ({
    media_id: mediaId(source(option.querySelector('img'))),
    chat_bubble_index: chat.indexOf(option.closest('flow-chat-bubble')),
  })).filter(option => option.media_id);
  const tileRows = tiles.map(tile => {
    const video = source(tile.querySelector('video'));
    const image = source(tile.querySelector('img'));
    return { key: video || image, media_id: mediaId(video) || mediaId(image) };
  });
  const finishedRows = finished.map(tile => {
    const video = source(tile.querySelector('video'));
    const image = source(tile.querySelector('img'));
    const value = video || image;
    let location = null;
    try {
      const url = new URL(value);
      location = { origin: url.origin, pathname: url.pathname };
    } catch {}
    return {
      identity: tileIdentity(video) || tileIdentity(image),
      location,
      media_id: mediaId(video) || mediaId(image),
    };
  });
  const finishedLocations = finishedRows.map(row => row.location).filter(Boolean);
  const tileKeys = await Promise.all(finishedRows.map(row => digest(row.identity)));
  const options = [...document.querySelectorAll('flow-a2ui-video-option')];
  const optionIds = options
    .map(option => mediaId(source(option.querySelector('img'))))
    .filter(Boolean);
  return {
    pending: document.querySelectorAll('flow-video-tile flow-pending-tile').length,
    media_ids: [...new Set(
      [...tileRows, ...finishedRows].map(row => row.media_id).filter(Boolean).concat(optionIds)
    )],
    finished_media_ids: [...new Set(finishedRows.map(row => row.media_id).filter(Boolean))],
    finished_tile_key_hashes: tileKeys.filter(Boolean),
    finished_tile_locations: finishedLocations,
    finished_tile_identity_records: finishedRows.map((row, index) => ({
      identity_hash: tileKeys[index],
      media_id: row.media_id,
    })),
    chat_video_media_ids: [...new Set(videoOptions.map(option => option.media_id))],
    chat_video_options: videoOptions,
    chat_count: chat.length,
    chat_bubble_hashes: await Promise.all(chat.map(async bubble => {
      const content = bubble.cloneNode(true);
      const transientSelectors = [
        'button', '[role="button"]', '[role="radio"]', '[role="toolbar"]',
        '[role="progressbar"]', 'time', 'mat-icon', '[aria-live]', 'flow-permission-message',
      ].join(',');
      content.querySelectorAll(transientSelectors).forEach(element => element.remove());
      const text = (content.innerText || content.textContent || '')
        .replace(/\\s+/g, ' ').trim();
      const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
      return [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('');
    })),
    chat_tail_hash: chat.length
      ? [...new Uint8Array(await crypto.subtle.digest(
          'SHA-256', new TextEncoder().encode((chat[chat.length - 1].innerText || '')
            .replace(/\\s+/g, ' ').trim())))].map(
          value => value.toString(16).padStart(2, '0')).join('')
      : '',
    chat_media_ids: chat.length ? [...new Set(videoOptions
      .filter(option => option.chat_bubble_index === chat.length - 1)
      .map(option => option.media_id))] : [],
  };
}
"""
_LIVE_GATE = "flow-permission-message:has(div[role='radio'].option-row:not(.read-only))"
_APPROVE_ROW = "div[role='radio'].option-row:not(.read-only):has(mat-icon:text-is('check'))"
_RECOVERY_GATE_HYDRATION_TIMEOUT_SECONDS = 15.0
_RECOVERY_GATE_POLL_INTERVAL_SECONDS = 1.0
_SUBMIT_DISPATCH_TIMEOUT_SECONDS = 15.0
_UUID_RE = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$", re.I)
_VIDEO_QUOTE_RE = re.compile(r"\b(\d+)\s+videos?\b", re.I)
_CREDIT_QUOTE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s+credits?\b", re.I)
_AMBIGUOUS_CREDIT_RANGE_RE = re.compile(
    r"\b\d+(?:[.,]\d+)?\s*(?:or|/|to)\s*\d+(?:[.,]\d+)?\s+credits?\b", re.I
)
_VIDEO_RPCS = frozenset({TEXT_TO_VIDEO_SUBMIT_RPC, "eb1hJf", "MZZa6b", "jwpduf", "as29s"})


def _monotonic() -> float:
    """Read the monotonic clock through a module-local test seam."""
    return time.monotonic()


async def _async_sleep(seconds: float) -> None:
    """Sleep through a module-local seam so tests do not patch asyncio globally."""
    await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class _ObservedRecord:
    workflow_id: str
    project_id: str
    media_id: str
    status: int | None
    rpcid: str | None = None
    submit_request: bool = False

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


def canonical_finished_tile_identity(origin: str, pathname: str) -> str | None:
    """Return a stable media UUID or measured poster token, ignoring URL queries."""
    media = re.fullmatch(
        r"/(?:video|image)/([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})",
        pathname,
        re.IGNORECASE,
    )
    if origin == "https://flow-content.google" and media:
        return f"media:{media[1].lower()}"
    if origin in {"https://lh3.googleusercontent.com", _FLOW_ORIGIN}:
        poster = re.fullmatch(r"/asb/([A-Za-z0-9_-]+)", pathname)
        if poster:
            return f"asb:{poster[1]}"
    return None


def canonical_finished_tile_url_identity(value: str) -> str | None:
    """Canonicalize a tile URL while discarding all query and fragment data."""
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return canonical_finished_tile_identity(f"{parsed.scheme}://{parsed.netloc}", parsed.path)


def canonical_finished_tile_url_hash(value: str) -> str | None:
    """Return a digest of canonical tile identity without signed URL parameters."""
    identity = canonical_finished_tile_url_identity(value)
    return hashlib.sha256(identity.encode("utf-8")).hexdigest() if identity else None


def provider_directive_sha256(prompt: str) -> str:
    """Hash normalized visible message text so UI whitespace does not alter identity."""
    return hashlib.sha256(" ".join(prompt.split()).encode("utf-8")).hexdigest()


def _finished_tile_hashes(state: Mapping[str, object]) -> list[str]:
    """Hash stable tile identities returned by the DOM probe without query strings."""
    locations = state.get("finished_tile_locations")
    if not isinstance(locations, list):
        value = state.get("finished_tile_key_hashes")
        if not isinstance(value, list):
            return []
        return [
            item
            for item in cast(list[object], value)
            if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
        ]
    hashes: list[str] = []
    for value in cast(list[object], locations):
        if not isinstance(value, dict):
            continue
        location = cast(dict[str, object], value)
        origin = location.get("origin")
        pathname = location.get("pathname")
        if isinstance(origin, str) and isinstance(pathname, str):
            identity = canonical_finished_tile_identity(origin, pathname)
            if identity is not None:
                hashes.append(hashlib.sha256(identity.encode("utf-8")).hexdigest())
    return hashes


def _new_finished_tile_hashes(
    state: Mapping[str, object], baseline: FlowSubmissionBaseline
) -> set[str]:
    """Compare tiles by stable identity, using baseline media IDs across hash migrations."""
    raw_records = state.get("finished_tile_identity_records")
    if not isinstance(raw_records, list):
        return set(_finished_tile_hashes(state)) - set(baseline.finished_tile_key_hashes)
    result: set[str] = set()
    for value in cast(list[object], raw_records):
        if not isinstance(value, dict):
            continue
        record = cast(dict[str, object], value)
        tile_hash = record.get("identity_hash")
        media_id = record.get("media_id")
        if not isinstance(tile_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", tile_hash):
            continue
        if isinstance(media_id, str) and media_id.lower() in baseline.media_ids:
            continue
        if tile_hash not in baseline.finished_tile_key_hashes:
            result.add(tile_hash)
    return result


class GoogleFlowAgentOnlyGenerationPort:
    """One-request Flow port pinned to one explicit V2 environment identity."""

    def __init__(
        self,
        base: GoogleFlowAgentOnlyPort,
        lease: ProfileLease,
        *,
        auth_profile_key: str,
        provider_account_id: str,
        provider_project_id: str,
        project_name: str,
    ) -> None:
        self._base = base
        self._lease = lease
        self._auth_profile_key = auth_profile_key
        self._provider_account_id = provider_account_id
        self._provider_project_id = provider_project_id
        self._project_name = project_name
        self._request: PhaseGExecutionRequest | None = None
        self._baseline: FlowSubmissionBaseline | None = None
        self._records: dict[tuple[str, str, str], _ObservedRecord] = {}
        self._response_tasks: set[asyncio.Task[None]] = set()
        self._waf_rejected = False
        self._generate_clicked = False
        self._approved_gate_id: str | None = None
        self._page: Page | None = None
        self._observed_media_ids: set[str] = set()
        self._observed_finished_media_ids: set[str] = set()
        self._observed_finished_tile_key_hashes: set[str] = set()
        self._observed_peak_pending = 0
        self._observed_new_turn = False
        self._observed_new_turn_bubble_index: int | None = None
        self._submit_observer_armed = False
        self._submit_dispatch_started_at: float | None = None
        self._submit_dispatch_deadline: float | None = None
        self._submit_dispatch_window_completed = False
        self._generate_click_dispatched: bool | None = None
        self._submit_requests: list[Any] = []
        self._submit_observed_at: str | None = None
        self._submit_project_id: str | None = None
        self._submit_request_failed: bool | None = None
        self._submit_response_observed = False
        self._submit_response_status: int | None = None
        self._submit_generation_record_observed: bool | None = None
        self._submit_refusal_detected: bool | None = None
        self._submission_surface = "AGENT_ONLY"
        self._provider_directive_sha256: str | None = None

    @classmethod
    async def open(
        cls,
        *,
        expected_email: str,
        auth_profile_key: str = _TEST_AUTH_PROFILE,
        provider_account_id: str = "PVA_0001",
        provider_project_id: str = _TEST_PROJECT_ID,
        project_name: str = _TEST_PROJECT_NAME,
        model_display_names: Mapping[str, str] = _MODEL_DISPLAY_NAMES,
        block_service_workers: bool = False,
    ) -> GoogleFlowAgentOnlyGenerationPort:
        """Open a repository-owned TEST or PROD profile, never personal Chrome."""
        if not expected_email.strip() or "@" not in expected_email:
            raise FlowPresubmitError(
                "FLOW_EXPECTED_IDENTITY_REQUIRED",
                "live Phase G requires an explicitly verified Google account identity",
            )
        base = await GoogleFlowAgentOnlyPort.open(
            auth_profile_key=auth_profile_key,
            provider_account_id=provider_account_id,
            provider_project_id=provider_project_id,
            project_name=project_name,
            model_display_names=model_display_names,
            expected_email=expected_email,
            headless=False,
            block_service_workers=block_service_workers,
        )
        lease = ProfileLease(base.profile_dir)
        try:
            await lease.aacquire()
        except ProfileLockedError as exc:
            await base.close()
            raise FlowPresubmitError(
                "FLOW_LANE_BUSY", "dedicated Flow profile is already in use"
            ) from exc
        return cls(
            base,
            lease,
            auth_profile_key=auth_profile_key,
            provider_account_id=provider_account_id,
            provider_project_id=provider_project_id,
            project_name=project_name,
        )

    async def close(self) -> None:
        """Detach transport observation and close the isolated repository browser."""
        if self._page is not None:
            self.disarm_submit_request_observer()
            self._page.remove_listener("response", self._on_response)
        if self._response_tasks:
            await asyncio.gather(*tuple(self._response_tasks), return_exceptions=True)
        try:
            await self._base.close()
        finally:
            self._lease.release()

    @property
    def auth_profile_key(self) -> str:
        """Return the profile key pinned when this live port was opened."""
        return self._auth_profile_key

    async def bind_request(self, request: PhaseGExecutionRequest) -> None:
        """Pin this live adapter to exactly the approved environment job identity."""
        if self._request is not None or self._generate_clicked:
            raise PhaseGError("PHASE_G_PORT_ALREADY_BOUND", "Flow port is already bound")
        context = request.execution_context
        if context is None:
            valid_identity = (
                self._auth_profile_key == _TEST_AUTH_PROFILE
                and self._provider_account_id == "PVA_0001"
                and self._provider_project_id == _TEST_PROJECT_ID
                and self._project_name == _TEST_PROJECT_NAME
                and request.provider_account_id == "PVA_0001"
            )
        else:
            expected_profile = {
                "TEST": _TEST_AUTH_PROFILE,
                "PROD": "GFLOW_PROD_AUTH",
            }.get(context.environment)
            expected_project_name = {
                "TEST": _TEST_PROJECT_NAME,
                "PROD": "AI Video Automation V2 - PROD",
            }.get(context.environment)
            valid_identity = (
                expected_profile == self._auth_profile_key == context.auth_profile
                and self._provider_account_id == context.provider_account_id
                and self._provider_project_id == context.provider_project_id
                and self._project_name == expected_project_name
                and request.provider_account_id == context.provider_account_id
            )
        if (
            not valid_identity
            or request.provider_project_id != self._provider_project_id
            or request.model_id != "MDL_0001"
            or request.scene_id != "SCN_000001"
            or request.generation_mode != "TEXT_TO_VIDEO"
            or request.output_count != 1
        ):
            raise PhaseGError(
                "PHASE_G_EXECUTION_IDENTITY_MISMATCH", "request identity is not pinned"
            )
        session = await self._base.inspect_session()
        if (
            session.status != "FLOW_SESSION_READY"
            or session.account_id != self._provider_account_id
        ):
            raise FlowPresubmitError(
                "FLOW_AUTH_NOT_READY", "selected profile readiness is unverified"
            )
        project = await self._base.select_project(self._provider_project_id)
        if project.project_id != self._provider_project_id:
            raise PhaseGError("PHASE_G_PROJECT_MISMATCH", "dedicated V2 project is not selected")
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
        self._provider_directive_sha256 = provider_directive_sha256(prompt)

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
        chat_ids = state.get("chat_video_media_ids", [])
        chat_count = self._integer(state.get("chat_count"))
        chat_bubble_hashes = state.get("chat_bubble_hashes")
        bubble_hash_items = (
            cast(list[object], chat_bubble_hashes) if isinstance(chat_bubble_hashes, list) else []
        )
        if (
            not isinstance(chat_bubble_hashes, list)
            or len(bubble_hash_items) != chat_count
            or any(
                not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in bubble_hash_items
            )
        ):
            raise PhaseGError(
                "PHASE_G_BASELINE_UNREADABLE", "Flow chat baseline hashes are unreadable"
            )
        media_ids = set(self._valid_ids(state.get("media_ids")))
        media_ids.update(self._valid_ids(chat_ids))
        media_ids.update(record.media_id.lower() for record in self._records.values())
        execution_ids = {record.workflow_id.lower() for record in self._records.values()}
        pending = self._integer(state.get("pending"))
        finished_tile_key_hashes = tuple(_finished_tile_hashes(cast(Mapping[str, object], state)))
        baseline = FlowSubmissionBaseline(
            request.provider_project_id,
            frozenset(media_ids),
            frozenset(execution_ids),
            pending,
            frozenset(gate_ids),
            f"{chat_count}:{state.get('chat_tail_hash') or ''}",
            tuple(cast(list[str], bubble_hash_items)),
            finished_tile_key_hashes,
            frozenset(self._valid_ids(chat_ids)),
        )
        self._baseline = baseline
        return baseline

    async def arm_submit_request_observer(self) -> None:
        """Validate the pinned submit surface, then arm passive request listeners."""
        if self._submit_observer_armed:
            raise PhaseGError(
                "PHASE_G_SUBMIT_OBSERVER_ALREADY_ARMED", "submit request observer is already armed"
            )
        page = self._require_page()
        await self._validate_generate_control(page)
        page.on("request", self._on_request)
        page.on("requestfailed", self._on_request_failed)
        self._submit_observer_armed = True

    async def _validate_generate_control(self, page: Page) -> Any:
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
        submit = page.locator("flow-generate-icon-button button")
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
        return submit

    async def click_generate_once(self) -> None:
        """Click the single enabled Agent start control at most once."""
        if self._generate_clicked:
            raise PhaseGError(
                "PHASE_G_DUPLICATE_SUBMIT_FORBIDDEN", "this Flow port already clicked Generate"
            )
        if not self._submit_observer_armed:
            raise PhaseGError(
                "PHASE_G_SUBMIT_OBSERVER_REQUIRED", "submit request observer must precede Generate"
            )
        page = self._require_page()
        submit = await self._validate_generate_control(page)
        self._generate_clicked = True
        self._submit_dispatch_started_at = _monotonic()
        self._submit_dispatch_deadline = (
            self._submit_dispatch_started_at + _SUBMIT_DISPATCH_TIMEOUT_SECONDS
        )
        try:
            await submit.click(timeout=5_000)
        except Exception:
            # A click timeout is ambiguous; keep observing for the already-started request.
            self._generate_click_dispatched = None
        else:
            self._generate_click_dispatched = True

    async def await_submit_dispatch(self) -> PhaseGSubmitTransportEvidence:
        """Observe the exact Agent turn or wait for the Classic Migrated request window."""
        if self._submit_dispatch_deadline is None:
            raise PhaseGError("PHASE_G_SUBMIT_OBSERVER_REQUIRED", "Generate was not dispatched")
        if self._submission_surface == "AGENT_ONLY":
            baseline = self._baseline
            if baseline is None or self._provider_directive_sha256 is None:
                raise PhaseGError(
                    "PHASE_G_BASELINE_REQUIRED", "Agent dispatch needs the prompt and chat baseline"
                )
            while _monotonic() < self._submit_dispatch_deadline:
                await self._capture_progress(baseline)
                if self._observed_new_turn:
                    break
                await _async_sleep(min(1.0, self._submit_dispatch_deadline - _monotonic()))
        else:
            remaining = max(0.0, self._submit_dispatch_deadline - _monotonic())
            if remaining:
                await _async_sleep(remaining)
        self._submit_dispatch_window_completed = True
        self.disarm_submit_request_observer()
        if self._response_tasks:
            await asyncio.gather(*tuple(self._response_tasks), return_exceptions=True)
        if self._submit_requests and self._submit_request_failed is None:
            self._submit_request_failed = False
        return self.read_submit_transport_evidence()

    def read_submit_transport_evidence(self) -> PhaseGSubmitTransportEvidence:
        count = len(self._submit_requests)
        return PhaseGSubmitTransportEvidence(
            submission_surface=self._submission_surface,
            agent_turn_dispatched=(
                self._observed_new_turn if self._submission_surface == "AGENT_ONLY" else None
            ),
            generate_click_dispatched=self._generate_click_dispatched,
            provider_submit_rpc_observed=count > 0,
            provider_submit_rpc_id=TEXT_TO_VIDEO_SUBMIT_RPC if count else None,
            provider_submit_observed_at=self._submit_observed_at,
            provider_submit_http_method="POST" if count else None,
            provider_submit_project_id=self._submit_project_id,
            provider_submit_request_count=count,
            provider_submit_dispatch_window_completed=(self._submit_dispatch_window_completed),
            provider_submit_request_failed=self._submit_request_failed,
            provider_submit_response_observed=(self._submit_response_observed if count else False),
            provider_submit_response_status=self._submit_response_status,
            provider_submit_generation_record_observed=(self._submit_generation_record_observed),
            provider_submit_refusal_detected=self._submit_refusal_detected,
        )

    def disarm_submit_request_observer(self) -> None:
        if self._page is not None and self._submit_observer_armed:
            self._page.remove_listener("request", self._on_request)
            self._page.remove_listener("requestfailed", self._on_request_failed)
        self._submit_observer_armed = False

    async def await_confirmation(
        self, baseline: FlowSubmissionBaseline, *, observation_deadline: float
    ) -> FlowConfirmationGate | None:
        """Observe a late/optional gate for the remainder of the bounded submit window."""
        deadline = observation_deadline
        while _monotonic() < deadline:
            await self._raise_wire_refusal()
            gates = self._require_page().locator(_LIVE_GATE)
            count = await gates.count()
            new_count = count - len(baseline.confirmation_ids)
            if new_count > 1:
                raise PhaseGError(
                    "UNEXPECTED_SECOND_CONFIRMATION", "more than one new Flow gate appeared"
                )
            if new_count == 1:
                await self._capture_progress(baseline)
                gate = gates.last
                markup = await gate.evaluate("el => el.outerHTML")
                gate_id = _gate_digest(markup)
                if gate_id in baseline.confirmation_ids:
                    raise PhaseGError(
                        "PHASE_G_STALE_CONFIRMATION", "Flow gate matches the baseline"
                    )
                text = await gate.inner_text()
                video_count, cost = parse_confirmation_quote(text)
                progress = self._provider_progress_present(baseline)
                if not progress:
                    self._approved_gate_id = gate_id
                return FlowConfirmationGate(
                    gate_id, baseline.project_id, video_count, cost, None, progress
                )
            await self._capture_progress(baseline)
            if self._provider_progress_present(baseline):
                return None
            await _async_sleep(1)
        await self._raise_wire_refusal()
        return None

    async def read_current_confirmation(
        self, baseline: FlowSubmissionBaseline
    ) -> FlowConfirmationGate:
        """Read one post-baseline gate, allowing Flow up to 15s to hydrate its DOM.

        The TEST project gate appeared about 10s after project load in live
        observation; polling once per second allows for that UI hydration while
        keeping the gate check bounded and read-only.
        """
        gates = self._require_page().locator(_LIVE_GATE)
        deadline = _monotonic() + _RECOVERY_GATE_HYDRATION_TIMEOUT_SECONDS
        while True:
            count = await gates.count()
            if count > 1:
                raise PhaseGError(
                    "PHASE_G_CONFIRMATION_NOT_ATTRIBUTABLE",
                    f"observed count = {count}; exactly one live confirmation is required",
                )
            if count == 1:
                break
            if _monotonic() >= deadline:
                raise PhaseGError(
                    "PHASE_G_CONFIRMATION_NOT_ATTRIBUTABLE",
                    "observed count = 0 after 15s recovery gate hydration timeout",
                )
            await _async_sleep(_RECOVERY_GATE_POLL_INTERVAL_SECONDS)
        gate = gates.first
        gate_id = _gate_digest(await gate.evaluate("el => el.outerHTML"))
        if gate_id in baseline.confirmation_ids:
            raise PhaseGError("PHASE_G_STALE_CONFIRMATION", "Flow gate matches the baseline")
        video_count, cost = parse_confirmation_quote(await gate.inner_text())
        await self._capture_progress(baseline)
        progress = self._provider_progress_present(baseline)
        if not progress:
            self._approved_gate_id = gate_id
        return FlowConfirmationGate(gate_id, baseline.project_id, video_count, cost, None, progress)

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
        self,
        baseline: FlowSubmissionBaseline,
        confirmation_id: str | None,
        *,
        observation_deadline: float,
    ) -> PhaseGAttemptEvidence:
        """Observe only a new TEST-project media ID tied to the new Agent turn."""
        request = self._require_request()
        if self._approved_gate_id is not None:
            raise PhaseGError("PHASE_G_GATE_NOT_APPROVED", "new gate was not approved")
        deadline = observation_deadline
        peak_pending = self._observed_peak_pending
        first_observation = True
        while first_observation or _monotonic() < deadline:
            first_observation = False
            await self._raise_wire_refusal()
            page = self._require_page()
            state = await page.evaluate(_VIDEO_STATE_JS)
            if not isinstance(state, dict):
                await _async_sleep(1)
                continue
            state = cast(dict[str, object], state)
            if self._observed_new_turn_bubble_index is None:
                self._observed_new_turn_bubble_index = self._new_agent_turn_bubble_index(
                    state, baseline
                )
            self._observed_new_turn = self._observed_new_turn_bubble_index is not None
            pending = self._integer(state.get("pending"))
            peak_pending = max(peak_pending, pending)
            wire_new = [
                record
                for record in self._records.values()
                if record.rpcid != "as29s" and self._submission_surface == "CLASSIC_MIGRATED"
                if record.media_id.lower() not in baseline.media_ids
                and record.workflow_id.lower() not in baseline.execution_ids
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
            finished_dom_ids = set(self._valid_ids(state.get("finished_media_ids")))
            current_tile_hashes = _finished_tile_hashes(state)
            new_tile_hashes = _new_finished_tile_hashes(state, baseline)
            tile_records = state.get("finished_tile_identity_records")
            first_tile_hash = None
            if (
                isinstance(tile_records, list)
                and tile_records
                and isinstance(tile_records[0], dict)
            ):
                first_tile_hash = cast(dict[str, object], tile_records[0]).get("identity_hash")
            newest_tile_changed = (
                first_tile_hash in new_tile_hashes
                if tile_records is not None
                else bool(
                    current_tile_hashes
                    and (
                        not baseline.finished_tile_key_hashes
                        or current_tile_hashes[0] != baseline.finished_tile_key_hashes[0]
                    )
                )
            )
            dom_correlated_media_ids: set[str]
            media_ids: set[str]
            if self._submission_surface == "AGENT_ONLY":
                new_turn_index = self._observed_new_turn_bubble_index
                agent_chat_ids = self._chat_video_ids_for_turn(state, new_turn_index)
                agent_chat_ids -= baseline.chat_video_media_ids | baseline.media_ids
                agent_finished_ids = finished_dom_ids - baseline.media_ids
                dom_correlated_media_ids = set()
                if (
                    len(agent_chat_ids) == 1
                    and len(new_tile_hashes) == 1
                    and newest_tile_changed
                    and (not agent_finished_ids or agent_finished_ids == agent_chat_ids)
                ):
                    dom_correlated_media_ids = set(agent_chat_ids)
                elif (
                    len(agent_chat_ids) > 1
                    and len(new_tile_hashes) == len(agent_chat_ids)
                    and newest_tile_changed
                    and (not agent_finished_ids or agent_finished_ids == agent_chat_ids)
                ):
                    dom_correlated_media_ids = set(agent_chat_ids)
                media_ids = dom_correlated_media_ids
            else:
                chat_video_ids = set(self._valid_ids(state.get("chat_video_media_ids")))
                dom_correlated_media_ids = (finished_dom_ids & chat_video_ids) - baseline.media_ids
                media_ids = {
                    record.media_id.lower() for record in wire_new
                } | dom_correlated_media_ids
            if len(media_ids) > 1 and (
                self._submission_surface != "AGENT_ONLY" or len(new_tile_hashes) == len(media_ids)
            ):
                ids = tuple(sorted(media_ids))
                return PhaseGAttemptEvidence(
                    request.d2_run_id,
                    "UNEXPECTED_MULTI_OUTPUT",
                    provider_execution_id=wire_new[0].workflow_id if wire_new else None,
                    provider_project_id=request.provider_project_id,
                    terminal_state="UNEXPECTED_MULTI_OUTPUT",
                    attributed_confirmation_id=confirmation_id,
                    new_provider_media_ids=ids,
                )
            baseline_chat_count, _, baseline_chat_hash = (baseline.agent_turn_id or "0:").partition(
                ":"
            )
            classic_new_turn = (
                self._integer(state.get("chat_count")) > int(baseline_chat_count or "0")
                and str(state.get("chat_tail_hash") or "") != baseline_chat_hash
            )
            new_turn = (
                self._observed_new_turn_bubble_index is not None
                if self._submission_surface == "AGENT_ONLY"
                else classic_new_turn
            )
            completed_media_ids = {record.media_id.lower() for record in wire_done}
            completed_media_ids.update(dom_correlated_media_ids)
            if (
                len(media_ids) == 1
                and not pending
                and new_turn
                and len(completed_media_ids) == 1
                and (self._submission_surface != "AGENT_ONLY" or newest_tile_changed)
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
            if _monotonic() >= deadline and peak_pending:
                return PhaseGAttemptEvidence(
                    request.d2_run_id,
                    "GENERATION_PENDING",
                    provider_execution_id=wire_new[0].workflow_id if wire_new else None,
                    provider_media_id=wire_new[0].media_id if wire_new else None,
                    provider_project_id=request.provider_project_id,
                    attributed_confirmation_id=confirmation_id,
                    new_provider_media_ids=tuple(sorted(media_ids)),
                )
            if _monotonic() >= deadline:
                break
            await _async_sleep(1)
        wire_pending = [
            record
            for record in self._records.values()
            if record.rpcid != "as29s" and self._submission_surface == "CLASSIC_MIGRATED"
            if record.media_id.lower() not in baseline.media_ids
            and record.workflow_id.lower() not in baseline.execution_ids
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

    def _on_request(self, request: Any) -> None:
        """Observe only the exact T2V submit RPC during the one-click window."""
        if (
            not self._submit_observer_armed
            or self._submit_dispatch_started_at is None
            or self._submit_dispatch_deadline is None
            or not self._submit_dispatch_started_at
            <= _monotonic()
            <= self._submit_dispatch_deadline
        ):
            return
        parsed = urlparse(str(request.url))
        if (
            parsed.netloc != "flow.google.com"
            or not parsed.path.endswith("/data/batchexecute")
            or str(request.method).upper() != "POST"
        ):
            return
        rpcids = parse_qs(parsed.query).get("rpcids", [])
        if TEXT_TO_VIDEO_SUBMIT_RPC not in rpcids:
            return
        attempt = self._request
        page = self._page
        if attempt is None or page is None:
            return
        page_url = urlparse(page.url)
        if (
            page_url.netloc != "flow.google.com"
            or page_url.path != f"/project/{attempt.provider_project_id}"
        ):
            return
        self._submit_requests.append(request)
        self._submit_project_id = attempt.provider_project_id
        if self._submit_observed_at is None:
            self._submit_observed_at = (
                datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )

    def _on_request_failed(self, request: Any) -> None:
        if self._is_submit_request(request):
            self._submit_request_failed = True

    def _is_submit_request(self, request: Any) -> bool:
        candidate_impl = getattr(request, "_impl_obj", None)
        return any(
            request is tracked
            or request == tracked
            or (
                candidate_impl is not None and candidate_impl is getattr(tracked, "_impl_obj", None)
            )
            for tracked in self._submit_requests
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
            is_submit_response = self._is_submit_request(response.request)
        except Exception:
            is_submit_response = False
        if is_submit_response:
            self._submit_response_observed = True
            self._submit_response_status = int(response.status)
            self._submit_generation_record_observed = False
            self._submit_refusal_detected = False
        try:
            body = await response.text()
        except Exception:
            return
        for error in rpc_errors(body):
            if "PUBLIC_ERROR_UNUSUAL_ACTIVITY" in error.reasons:
                self._waf_rejected = True
                if is_submit_response:
                    self._submit_refusal_detected = True
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
            if is_submit_response and rpcid == TEXT_TO_VIDEO_SUBMIT_RPC:
                self._submit_generation_record_observed = True
            key = (record.workflow_id.lower(), record.project_id.lower(), record.media_id.lower())
            self._records[key] = _ObservedRecord(
                record.workflow_id,
                record.project_id,
                record.media_id,
                record.status,
                rpcid,
                is_submit_response,
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

    async def _capture_progress(self, baseline: FlowSubmissionBaseline) -> None:
        """Retain sanitized provider progress while a late/optional gate is observed."""
        state_value = await self._require_page().evaluate(_VIDEO_STATE_JS)
        if not isinstance(state_value, dict):
            return
        state = cast(dict[str, object], state_value)
        self._observed_peak_pending = max(
            self._observed_peak_pending, self._integer(state.get("pending"))
        )
        self._observed_media_ids.update(
            record.media_id.lower()
            for record in self._records.values()
            if record.rpcid != "as29s"
            and (self._submission_surface != "AGENT_ONLY" or record.submit_request)
            if record.media_id.lower() not in baseline.media_ids
            and record.workflow_id.lower() not in baseline.execution_ids
        )
        self._observed_finished_media_ids.update(
            set(self._valid_ids(state.get("finished_media_ids"))) - baseline.media_ids
        )
        self._observed_finished_tile_key_hashes.update(_new_finished_tile_hashes(state, baseline))
        if self._observed_new_turn_bubble_index is None:
            self._observed_new_turn_bubble_index = self._new_agent_turn_bubble_index(
                state, baseline
            )
        self._observed_new_turn = self._observed_new_turn_bubble_index is not None

    def _new_agent_turn_bubble_index(
        self, state: dict[str, object], baseline: FlowSubmissionBaseline
    ) -> int | None:
        if self._provider_directive_sha256 is None:
            return None
        baseline_count, _, _ = (baseline.agent_turn_id or "0:").partition(":")
        hashes = self._valid_hashes(state.get("chat_bubble_hashes"))
        current_count = self._integer(state.get("chat_count"))
        baseline_index = int(baseline_count or "0")
        if current_count <= baseline_index or len(hashes) != current_count:
            return None
        matching = [
            index
            for index in range(baseline_index, current_count)
            if hashes[index] == self._provider_directive_sha256
        ]
        return matching[0] if len(matching) == 1 else None

    def _chat_video_ids_for_turn(
        self, state: dict[str, object], turn_index: int | None
    ) -> set[str]:
        if turn_index is None:
            return set()
        value = state.get("chat_video_options")
        if not isinstance(value, list):
            return set()
        ids: set[str] = set()
        for option in cast(list[object], value):
            if not isinstance(option, dict):
                continue
            row = cast(dict[str, object], option)
            media_id = row.get("media_id")
            bubble_index = row.get("chat_bubble_index")
            if (
                isinstance(media_id, str)
                and _UUID_RE.fullmatch(media_id)
                and isinstance(bubble_index, int)
                and not isinstance(bubble_index, bool)
                and bubble_index >= turn_index
            ):
                ids.add(media_id.lower())
        return ids

    def _provider_progress_present(self, baseline: FlowSubmissionBaseline) -> bool:
        return bool(
            self._observed_peak_pending
            or self._observed_media_ids
            or self._observed_finished_media_ids
            or self._observed_finished_tile_key_hashes
            or any(
                record.rpcid != "as29s"
                and (self._submission_surface != "AGENT_ONLY" or record.submit_request)
                and (
                    record.media_id.lower() not in baseline.media_ids
                    or record.workflow_id.lower() not in baseline.execution_ids
                )
                for record in self._records.values()
            )
        )

    @staticmethod
    def _valid_hashes(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            item
            for item in cast(list[object], value)
            if isinstance(item, str) and re.fullmatch(r"[0-9a-f]{64}", item)
        ]

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
    def _valid_ids(values: object) -> set[str]:
        if not isinstance(values, list):
            return set()
        return {
            value.lower()
            for value in cast(list[object], values)
            if isinstance(value, str) and _UUID_RE.fullmatch(value)
        }
