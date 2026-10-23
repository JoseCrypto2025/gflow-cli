"""Collect sanitized, read-only evidence for the exact provider-silent D2 V3 contract."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import parse_qs, urlsplit

from gflow_cli.api.transports.batchexecute import generation_records, parse_frames
from gflow_cli.v2_runtime.google_flow import FlowPresubmitRequest
from gflow_cli.v2_runtime.phase_g import D2DailyCreditLedger, PhaseGError, PhaseGExecutionRequest
from gflow_cli.v2_runtime.phase_g_artifacts import sha256_file
from gflow_cli.v2_runtime.phase_g_forensic_provider_silent_v3 import (
    validate_provider_silent_evidence_v3,
)
from gflow_cli.v2_runtime.phase_g_live_flow import (
    _VIDEO_STATE_JS,
    GoogleFlowAgentOnlyGenerationPort,
)

_RUN_ID = "D2_8d892c38cc8f4c99"
_PROJECT_ID = "34e60953-e534-46e9-ac65-993893e61065"
_PROJECT_NAME = "AI Video Automation V2 - TEST"
_ACCOUNT_ID = "PVA_0001"
_HISTORICAL_WORKFLOW_ID = "9dc0380b-0a38-44f7-9c6c-8fe5262fdc2a"
_HISTORICAL_MEDIA_ID = "d6e85926-120f-40ab-bfc0-6a788c521339"
_PROJECT_LISTING_RPC = "Zzl0ze"
_CHAT_SCAN_JS = r"""
() => {
  const bubbles = [...document.querySelectorAll('flow-chat-bubble')];
  const ids = new Set();
  const uuid = /[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}/ig;
  const run = /D2_[A-Za-z0-9_-]{8,64}/g;
  for (const bubble of bubbles) {
    const text = bubble.innerText || bubble.textContent || '';
    for (const match of text.matchAll(uuid)) ids.add(match[0].toLowerCase());
    for (const match of text.matchAll(run)) ids.add(match[0]);
  }
  return {
    count: bubbles.length,
    provider_identifiers: [...ids].sort(),
    stable_ids_available: bubbles.some(el => Boolean(el.getAttribute('data-id') || el.id)),
    timestamps_available: bubbles.some(el => Boolean(el.querySelector('time[datetime]'))),
  };
}
"""


def _required_attempt(ledger: D2DailyCreditLedger) -> dict[str, object]:
    rows = [row for row in ledger.attempts() if row.get("d2_run_id") == _RUN_ID]
    if len(rows) != 1:
        raise PhaseGError("PHASE_G_ATTEMPT_NOT_UNIQUE", "target D2 must exist exactly once")
    row = dict(rows[0])
    if (
        row.get("submission_state") != "UNKNOWN_SUBMISSION_STATE"
        or row.get("terminal_state") is not None
        or row.get("generate_action_count") != 1
        or row.get("approval_action_count") != 0
        or row.get("accounting_date") != "2026-10-23"
        or row.get("accounted_credit_cost") != "50"
        or row.get("provider_execution_id") is not None
        or row.get("provider_media_id") is not None
        or row.get("provider_media_ids") != []
    ):
        raise PhaseGError("PHASE_G_FORENSIC_STATE_MISMATCH", "target D2 no longer matches V3 shape")
    return row


def _request_from_attempt(row: dict[str, object]) -> PhaseGExecutionRequest:
    return PhaseGExecutionRequest(
        scene_id=str(row["scene_id"]),
        scene_prompt_id=str(row["scene_prompt_id"]),
        generation_profile_id=str(row["generation_profile_id"]),
        provider_id=str(row["provider_id"]),
        provider_account_id=str(row["provider_account_id"]),
        provider_project_id=str(row["provider_project_id"]),
        model_id=str(row["model_id"]),
        generation_mode=str(row["generation_mode"]),
        compiled_prompt_text="",
        compiled_negative_prompt="",
        source_context_sha256=str(row["source_context_sha256"]),
        prompt_sha256=str(row["prompt_sha256"]),
        duration_sec=float(cast(int | float, row["requested_duration_sec"])),
        aspect_ratio=str(row["requested_aspect_ratio"]),
        output_count=int(cast(int, row["requested_output_count"])),
        d2_run_id=_RUN_ID,
        presubmit_request=cast(FlowPresubmitRequest, None),
        model_compatibility_sha256=cast(str | None, row.get("model_compatibility_sha256")),
    )


def _historical_ids(
    ledger: D2DailyCreditLedger, row: dict[str, object]
) -> tuple[list[str], list[str]]:
    rows = [dict(item) for item in ledger.attempts()]
    target_index = next(index for index, item in enumerate(rows) if item == row)
    prior_rows = [
        item for item in rows[:target_index] if item.get("provider_project_id") == _PROJECT_ID
    ]
    execution_ids = {
        value
        for item in prior_rows
        if isinstance((value := item.get("provider_execution_id")), str) and value
    }
    media_ids = {
        value.lower()
        for item in prior_rows
        if isinstance((value := item.get("provider_media_id")), str) and value
    }
    for item in prior_rows:
        provider_media_ids = item.get("provider_media_ids", [])
        if isinstance(provider_media_ids, list):
            media_ids.update(
                value.lower()
                for value in cast(list[object], provider_media_ids)
                if isinstance(value, str) and value
            )
    baseline_execution = row.get("baseline_execution_ids", [])
    baseline_media = row.get("baseline_media_ids", [])
    if isinstance(baseline_execution, list):
        execution_ids.update(cast(list[str], baseline_execution))
    if isinstance(baseline_media, list):
        media_ids.update(value.lower() for value in cast(list[str], baseline_media))
    return sorted(execution_ids), sorted(media_ids)


def _uuid_ids(value: object, *, field: str) -> list[str]:
    if not isinstance(value, list):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", f"{field} are unreadable")
    values = cast(list[object], value)
    if any(
        not isinstance(item, str)
        or not re.fullmatch(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", item, re.I)
        for item in values
    ):
        raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", f"{field} are malformed")
    return sorted({cast(str, item).lower() for item in values})


@dataclass(frozen=True)
class _SanitizedGenerationRecord:
    workflow_id: str
    project_id: str
    media_id: str
    status: int | None


class _ProviderRecord(Protocol):
    workflow_id: str
    project_id: str
    media_id: str
    status: int | None


def _is_project_listing_response(response_url: str) -> bool:
    parsed = urlsplit(response_url)
    if parsed.hostname != "flow.google.com" or "batchexecute" not in parsed.path:
        return False
    return any(
        _PROJECT_LISTING_RPC in value.split(",")
        for value in parse_qs(parsed.query).get("rpcids", [])
    )


class _ProjectListingObserver:
    """Collect sanitized records from one explicitly armed project reload."""

    def __init__(self, page: object) -> None:
        self._page = page
        self._tasks: set[asyncio.Task[None]] = set()
        self._armed = False
        self._event: asyncio.Event | None = None
        self._response_count = 0
        self._records: list[_SanitizedGenerationRecord] = []
        self._error: str | None = None
        self._page.on("response", self._on_response)  # type: ignore[attr-defined]

    def begin_snapshot(self) -> None:
        self._event = asyncio.Event()
        self._response_count = 0
        self._records = []
        self._error = None
        self._armed = True

    def _on_response(self, response: object) -> None:
        if not self._armed or not _is_project_listing_response(str(getattr(response, "url", ""))):
            return
        self._response_count += 1
        task = asyncio.create_task(self._capture_response(response))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _capture_response(self, response: object) -> None:
        try:
            body = await response.text()  # type: ignore[attr-defined]
            frames = parse_frames(body)
            payloads = [payload for rpcid, payload in frames if rpcid == _PROJECT_LISTING_RPC]
            if not payloads:
                self._error = "project listing response did not contain its requested RPC frame"
                return
            decoded = [
                record
                for payload in payloads
                for record in generation_records(_PROJECT_LISTING_RPC, payload)
            ]
            if not decoded:
                self._error = "project listing RPC was observed but no video record shape decoded"
                return
            self._records.extend(
                _SanitizedGenerationRecord(
                    record.workflow_id.lower(),
                    record.project_id.lower(),
                    record.media_id.lower(),
                    record.status,
                )
                for record in decoded
            )
        except Exception:  # noqa: BLE001 - response/parser details may contain sensitive data
            self._error = "project listing response could not be read or decoded"
        finally:
            event = self._event
            if event is not None:
                event.set()

    async def wait_snapshot(self, *, timeout_s: float = 30.0) -> list[_SanitizedGenerationRecord]:
        event = self._event
        if not self._armed or event is None:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "project listing observer is not armed"
            )
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout_s)
            tasks = tuple(self._tasks)
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except TimeoutError as exc:
            self._armed = False
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                "project listing RPC was not observed for this reload",
            ) from exc
        finally:
            self._armed = False
        if self._response_count != 1:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                "project listing reload did not yield exactly one RPC response",
            )
        if self._error is not None:
            raise PhaseGError("PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", self._error)
        if not self._records:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                "project listing RPC was observed with zero video records",
            )
        return list(self._records)

    async def close(self) -> None:
        self._armed = False
        self._page.remove_listener("response", self._on_response)  # type: ignore[attr-defined]
        pending = tuple(self._tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def _reload_project_listing(
    page: object,
    observer: _ProjectListingObserver,
    *,
    timeout_s: float = 30.0,
    deadline: float | None = None,
) -> list[_SanitizedGenerationRecord]:
    if deadline is None:
        deadline = time.monotonic() + timeout_s

    def timeout_ms() -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                "project listing observation exceeded its bounded deadline",
            )
        return max(1, min(30_000, int(remaining * 1000)))

    observer.begin_snapshot()
    try:
        await page.reload(  # type: ignore[attr-defined]
            wait_until="domcontentloaded", timeout=timeout_ms()
        )
        await page.wait_for_load_state(  # type: ignore[attr-defined]
            "domcontentloaded", timeout=timeout_ms()
        )
        if getattr(page, "url", None) != f"https://flow.google.com/project/{_PROJECT_ID}":
            raise PhaseGError(
                "PHASE_G_PROJECT_MISMATCH", "project reload left the pinned TEST project"
            )
        return await observer.wait_snapshot(timeout_s=min(30.0, (deadline - time.monotonic())))
    except PhaseGError:
        raise
    except Exception as exc:  # noqa: BLE001 - do not expose response/navigation details
        raise PhaseGError(
            "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "project listing reload did not complete"
        ) from exc


def _merge_project_records(
    listing_records: Iterable[_ProviderRecord], live_records: Iterable[_ProviderRecord]
) -> list[_SanitizedGenerationRecord]:
    records: dict[tuple[str, str, str], _SanitizedGenerationRecord] = {}
    for record in [*listing_records, *live_records]:
        item = _SanitizedGenerationRecord(
            record.workflow_id.lower(),
            record.project_id.lower(),
            record.media_id.lower(),
            record.status,
        )
        if item.project_id != _PROJECT_ID.lower():
            continue
        key = (item.workflow_id, item.project_id, item.media_id)
        previous = records.get(key)
        if previous is not None and previous.status != item.status:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                "project listing and live status records disagree",
            )
        records[key] = item
    return sorted(
        records.values(), key=lambda item: (item.workflow_id, item.project_id, item.media_id)
    )


def _require_historical_records(
    records: list[_SanitizedGenerationRecord],
    historical_execution_ids: list[str],
    historical_media_ids: list[str],
) -> None:
    record_exec = {item.workflow_id for item in records}
    record_media = {item.media_id for item in records}
    if not record_exec.issubset(historical_execution_ids) or not record_media.issubset(
        historical_media_ids
    ):
        raise PhaseGError(
            "PHASE_G_PROVIDER_OUTCOME_PRESENT", "new structured provider output found"
        )


async def _collect(args: argparse.Namespace) -> dict[str, object]:
    ledger = D2DailyCreditLedger(args.runtime_state_root)
    if ledger.path.is_symlink() or not ledger.path.is_file():
        raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "canonical D2 ledger is missing or unsafe")
    ledger_sha = sha256_file(ledger.path)
    attempt = _required_attempt(ledger)
    if (
        attempt.get("provider_account_id") != _ACCOUNT_ID
        or attempt.get("provider_project_id") != _PROJECT_ID
    ):
        raise PhaseGError(
            "PHASE_G_FORENSIC_IDENTITY_MISMATCH", "target is not the pinned TEST lane"
        )
    historical_execution_ids, historical_media_ids = _historical_ids(ledger, attempt)
    port = await GoogleFlowAgentOnlyGenerationPort.open(expected_email=args.expected_email)
    listing_observer: _ProjectListingObserver | None = None
    try:
        await port.bind_request(_request_from_attempt(attempt))
        page = port._require_page()
        if page.url != f"https://flow.google.com/project/{_PROJECT_ID}":
            raise PhaseGError("PHASE_G_PROJECT_MISMATCH", "TEST project URL is not selected")
        listing_observer = _ProjectListingObserver(page)
        snapshots: list[dict[str, object]] = []
        chat_metadata: dict[str, object] | None = None
        chat_hashes: list[str] = []
        chat_provider_identifiers: set[str] = set()
        previous_record_signature: tuple[tuple[str, str, str, int | None], ...] | None = None
        listing_records: list[_SanitizedGenerationRecord] = []
        start = time.monotonic()
        deadline = start + 120.0
        for elapsed in (0, 30, 60):
            if elapsed:
                await asyncio.sleep(max(0.0, elapsed - (time.monotonic() - start)))
            if time.monotonic() >= deadline:
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                    "project listing observation exceeded its bounded deadline",
                )
            await port._raise_wire_refusal()
            listing_records = await _reload_project_listing(
                page, listing_observer, deadline=deadline
            )
            if not any(
                item.project_id == _PROJECT_ID.lower()
                and item.workflow_id == _HISTORICAL_WORKFLOW_ID
                and item.media_id == _HISTORICAL_MEDIA_ID
                for item in listing_records
            ):
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                    "project listing did not contain the known historical record",
                )
            state_value = await page.evaluate(_VIDEO_STATE_JS)
            chat_value = await page.evaluate(_CHAT_SCAN_JS)
            if not isinstance(state_value, dict) or not isinstance(chat_value, dict):
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "Flow state is unreadable"
                )
            state = cast(dict[str, object], state_value)
            chat_metadata = cast(dict[str, object], chat_value)
            chat_count = state.get("chat_count")
            bubble_hashes = state.get("chat_bubble_hashes")
            if (
                not isinstance(chat_count, int)
                or isinstance(chat_count, bool)
                or not isinstance(bubble_hashes, list)
                or len(cast(list[object], bubble_hashes)) != chat_count
                or chat_value.get("count") != chat_count
                or any(
                    not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
                    for item in bubble_hashes
                )
            ):
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "Flow chat snapshot is not hydrated"
                )
            chat_hashes = cast(list[str], bubble_hashes)
            scanned_ids = chat_value.get("provider_identifiers")
            if not isinstance(scanned_ids, list) or any(
                not isinstance(item, str) for item in scanned_ids
            ):
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "chat identifier scan is unavailable"
                )
            chat_provider_identifiers.update(cast(list[str], scanned_ids))
            dom_media_ids = _uuid_ids(state.get("media_ids"), field="project media IDs")
            dom_finished_media_ids = _uuid_ids(
                state.get("finished_media_ids"), field="finished media IDs"
            )
            if not set(dom_media_ids).issubset(historical_media_ids) or not set(
                dom_finished_media_ids
            ).issubset(historical_media_ids):
                raise PhaseGError(
                    "PHASE_G_PROVIDER_OUTCOME_PRESENT", "new DOM media identity found"
                )
            await port._raise_wire_refusal()
            confirmation_ids = sorted(await port._confirmation_ids())
            records = _merge_project_records(listing_records, list(port._records.values()))
            if not records:
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                    "structured GenerationRecord enumeration is unavailable",
                )
            if not any(
                item.workflow_id == _HISTORICAL_WORKFLOW_ID
                and item.media_id == _HISTORICAL_MEDIA_ID
                for item in records
            ):
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                    "merged provider records omit the known historical record",
                )
            record_exec = sorted({item.workflow_id for item in records})
            record_media = sorted({item.media_id.lower() for item in records})
            _require_historical_records(records, historical_execution_ids, historical_media_ids)
            record_signature = tuple(
                (item.workflow_id, item.project_id, item.media_id.lower(), item.status)
                for item in records
            )
            if (
                previous_record_signature is not None
                and record_signature != previous_record_signature
            ):
                raise PhaseGError(
                    "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                    "structured provider records changed during observation",
                )
            previous_record_signature = record_signature
            snapshots.append(
                {
                    "ELAPSED_SECONDS": int(time.monotonic() - start),
                    "HYDRATED": True,
                    "ENUMERATION_COMPLETE": True,
                    "PENDING_COUNT": state.get("pending"),
                    "CONFIRMATION_IDS": confirmation_ids,
                    "EXECUTION_IDS": record_exec,
                    "MEDIA_IDS": record_media,
                    "DOM_MEDIA_IDS": dom_media_ids,
                    "DOM_FINISHED_MEDIA_IDS": dom_finished_media_ids,
                }
            )

        await port._raise_wire_refusal()
        final_state_value = await page.evaluate(_VIDEO_STATE_JS)
        final_chat_value = await page.evaluate(_CHAT_SCAN_JS)
        if not isinstance(final_state_value, dict) or not isinstance(final_chat_value, dict):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "final Flow state is unreadable"
            )
        final_state = cast(dict[str, object], final_state_value)
        final_chat = cast(dict[str, object], final_chat_value)
        final_chat_count = final_state.get("chat_count")
        if (
            not isinstance(final_chat_count, int)
            or isinstance(final_chat_count, bool)
            or final_chat.get("count") != final_chat_count
        ):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "final chat snapshot is inconsistent"
            )
        final_chat_ids = final_chat.get("provider_identifiers")
        if not isinstance(final_chat_ids, list) or any(
            not isinstance(item, str) for item in final_chat_ids
        ):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "final chat identifier scan is unavailable"
            )
        chat_provider_identifiers.update(cast(list[str], final_chat_ids))
        final_dom_media_ids = _uuid_ids(
            final_state.get("media_ids"), field="final project media IDs"
        )
        final_dom_finished_media_ids = _uuid_ids(
            final_state.get("finished_media_ids"), field="final finished media IDs"
        )
        if not set(final_dom_media_ids).issubset(historical_media_ids) or not set(
            final_dom_finished_media_ids
        ).issubset(historical_media_ids):
            raise PhaseGError(
                "PHASE_G_PROVIDER_OUTCOME_PRESENT", "new final DOM media identity found"
            )
        final_records = _merge_project_records(listing_records, list(port._records.values()))
        final_record_signature = tuple(
            (item.workflow_id, item.project_id, item.media_id.lower(), item.status)
            for item in final_records
        )
        if final_record_signature != previous_record_signature:
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE",
                "structured provider records changed after the final snapshot",
            )
        records = final_records
        chat_metadata = final_chat
        chat_hashes_value = final_state.get("chat_bubble_hashes")
        if (
            not isinstance(chat_hashes_value, list)
            or len(chat_hashes_value) != final_chat_count
            or any(
                not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item)
                for item in chat_hashes_value
            )
        ):
            raise PhaseGError(
                "PHASE_G_FORENSIC_EVIDENCE_INCOMPLETE", "final chat hashes are unreadable"
            )
        chat_hashes = cast(list[str], chat_hashes_value)
        structured_ids = set(record_exec) | set(record_media) | {_PROJECT_ID.lower()}
        chat_ids = sorted(chat_provider_identifiers)
        if any(value not in structured_ids and value != _RUN_ID for value in chat_ids):
            raise PhaseGError(
                "PHASE_G_PROVIDER_OUTCOME_PRESENT",
                "chat contains an identifier without matching structured provider evidence",
            )
        baseline_marker = str(attempt.get("baseline_agent_turn_id") or "0:")
        baseline_count_text = baseline_marker.partition(":")[0]
        baseline_count = int(baseline_count_text)
        baseline_hashes_value = attempt.get("baseline_agent_turn_bubble_hashes", [])
        baseline_hashes = (
            cast(list[str], baseline_hashes_value)
            if isinstance(baseline_hashes_value, list)
            else []
        )
        hash_prefix_stable = (
            len(chat_hashes) >= baseline_count and chat_hashes[:baseline_count] == baseline_hashes
        )
        records_value = [
            {
                "WORKFLOW_ID": item.workflow_id,
                "PROJECT_ID": item.project_id,
                "MEDIA_ID": item.media_id,
                "STATUS": item.status,
            }
            for item in records
        ]
        final_confirmation_ids = sorted(await port._confirmation_ids())
        if final_state.get("pending") != 0 or final_confirmation_ids:
            raise PhaseGError(
                "PHASE_G_PROVIDER_OUTCOME_PRESENT", "project activity changed during observation"
            )
        evidence: dict[str, object] = {
            "SCHEMA_VERSION": "D2_FORENSIC_PROVIDER_SILENT_CLOSURE_V3",
            "D2_RUN_ID": _RUN_ID,
            "CANONICAL_LEDGER_SHA256": ledger_sha,
            "IMPLEMENTATION_COMMIT": args.implementation_commit,
            "PROVIDER_ACCOUNT_ID": _ACCOUNT_ID,
            "PROVIDER_PROJECT_ID": _PROJECT_ID,
            "PROVIDER_PROJECT_NAME": _PROJECT_NAME,
            "CURRENT_SUBMISSION_STATE": "UNKNOWN_SUBMISSION_STATE",
            "CURRENT_TERMINAL_STATE": "NONE",
            "GENERATE_ACTION_COUNT": attempt["generate_action_count"],
            "APPROVAL_ACTION_COUNT": attempt["approval_action_count"],
            "ACCOUNTING_DATE_EXPECTED": attempt["accounting_date"],
            "ACCOUNTED_CREDIT_COST_EXPECTED": attempt["accounted_credit_cost"],
            "SCENE_ID": attempt["scene_id"],
            "SCENE_PROMPT_ID": attempt["scene_prompt_id"],
            "GENERATION_PROFILE_ID": attempt["generation_profile_id"],
            "PROVIDER_ID": attempt["provider_id"],
            "MODEL_ID": attempt["model_id"],
            "GENERATION_MODE": attempt["generation_mode"],
            "PROMPT_SHA256": attempt["prompt_sha256"],
            "SOURCE_CONTEXT_SHA256": attempt["source_context_sha256"],
            "REQUESTED_DURATION_SEC": attempt["requested_duration_sec"],
            "REQUESTED_ASPECT_RATIO": attempt["requested_aspect_ratio"],
            "REQUESTED_OUTPUT_COUNT": attempt["requested_output_count"],
            "BASELINE_CAPTURED": attempt["baseline_captured"],
            "BASELINE_PENDING_COUNT": attempt["baseline_pending_count"],
            "BASELINE_EXECUTION_IDS": attempt["baseline_execution_ids"],
            "BASELINE_MEDIA_IDS": attempt["baseline_media_ids"],
            "BASELINE_CONFIRMATION_IDS": attempt["baseline_confirmation_ids"],
            "RECOVERY_JOURNAL_CLEAN": True,
            "PROVIDER_EXECUTION_ID_ABSENT": attempt["provider_execution_id"] is None,
            "PROVIDER_MEDIA_ID_ABSENT": attempt["provider_media_id"] is None,
            "PROVIDER_MEDIA_IDS_ABSENT": attempt["provider_media_ids"] == [],
            "LOCAL_ATTEMPT_OUTPUT_ABSENT": not (Path(str(attempt["temp_root"])) / _RUN_ID).exists(),
            "REMOTE_UPLOAD_ABSENT": attempt.get("remote_file_id") is None,
            "REMOTE_VERIFIED_ABSENT": attempt.get("remote_verified") in (None, False),
            "LIVE_PROJECT_HYDRATED": True,
            "LIVE_SNAPSHOT_COUNT": len(snapshots),
            "LIVE_OBSERVATION_DURATION_SECONDS": int(time.monotonic() - start),
            "LIVE_PENDING_COUNT": final_state.get("pending"),
            "LIVE_CONFIRMATION_COUNT": len(final_confirmation_ids),
            "LIVE_CONFIRMATION_IDS": final_confirmation_ids,
            "LIVE_STRUCTURED_RECORDS_ENUMERATED": True,
            "LIVE_STRUCTURED_RECORD_COUNT": len(records_value),
            "LIVE_STRUCTURED_RECORDS": records_value,
            "LIVE_SNAPSHOTS": snapshots,
            "HISTORICAL_EXECUTION_IDS": historical_execution_ids,
            "HISTORICAL_MEDIA_IDS": historical_media_ids,
            "CANDIDATE_NEW_EXECUTION_IDS": [],
            "CANDIDATE_NEW_MEDIA_IDS": [],
            "CANDIDATE_NEW_EXECUTION_COUNT": 0,
            "CANDIDATE_NEW_MEDIA_COUNT": 0,
            "ALL_LIVE_STRUCTURED_IDS_HISTORICAL": True,
            "CHAT_ACTIVITY_PRESENT": int(chat_metadata["count"]) > 0,
            "CHAT_COUNT": chat_metadata["count"],
            "CHAT_HASH_PREFIX_STABLE": hash_prefix_stable,
            "CHAT_STABLE_IDS_AVAILABLE": chat_metadata["stable_ids_available"],
            "CHAT_TIMESTAMPS_AVAILABLE": chat_metadata["timestamps_available"],
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
        if sha256_file(ledger.path) != ledger_sha:
            raise PhaseGError(
                "PHASE_G_LEDGER_CHANGED", "ledger changed during read-only collection"
            )
        summary = validate_provider_silent_evidence_v3(evidence)
        evidence_path = (
            args.runtime_state_root / "phase-g" / "forensic-evidence-v3" / f"{_RUN_ID}.json"
        )
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        evidence_path.write_bytes(raw)
        return {
            "evidence_path": str(evidence_path),
            "evidence_sha256": hashlib.sha256(raw).hexdigest(),
            "snapshot_count": len(snapshots),
            "project_listing_rpc_observed": True,
            "project_listing_enumeration": "PROVEN",
            "listing_snapshot_count": len(snapshots),
            "observation_duration_seconds": evidence["LIVE_OBSERVATION_DURATION_SECONDS"],
            "pending_count": evidence["LIVE_PENDING_COUNT"],
            "confirmation_count": evidence["LIVE_CONFIRMATION_COUNT"],
            "structured_record_count": len(records_value),
            "chat_count": chat_metadata["count"],
            "chat_hash_prefix_stable": hash_prefix_stable,
        }
    finally:
        if listing_observer is not None:
            await listing_observer.close()
        await port.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-state-root", type=Path, required=True)
    parser.add_argument("--expected-email", required=True)
    parser.add_argument("--implementation-commit", required=True)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(_collect(args))
        for key, value in result.items():
            print(f"{key.upper()} = {value}")
        return 0
    except (PhaseGError, OSError, TimeoutError) as exc:
        print(f"ERROR_CLASS = {type(exc).__name__}")
        print(f"ERROR = {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
