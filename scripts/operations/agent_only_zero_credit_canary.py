"""Abort-before-network canary for the TEST Agent-only submit control.

This diagnostic deliberately does not use the D2 ledger or normal Phase G
executor. Every outbound request is aborted after canary activation, and the
report contains only RPC identifiers, methods, timestamps, and counts.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from gflow_cli.v2_runtime.errors import FlowPresubmitError
from gflow_cli.v2_runtime.google_flow import build_agent_only_directive
from gflow_cli.v2_runtime.phase_g_live_flow import (
    _LIVE_GATE,
    _VIDEO_STATE_JS,
    GoogleFlowAgentOnlyGenerationPort,
)
from gflow_cli.v2_runtime.phase_g_run import (
    build_phase_g_request_from_snapshot,
    run_fresh_presubmit,
)

_EMAIL = "singleava508@gmail.com"
_PROJECT_ID = "34e60953-e534-46e9-ac65-993893e61065"
_RPCID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
_STATE_STABILITY_TIMEOUT_S = 15.0
_STATE_STABILITY_INTERVAL_MS = 500
_STATE_STABILITY_SAMPLES = 3


def _sanitized_rpcids(url: str) -> list[str]:
    """Keep only allowlisted batchexecute rpcids from an otherwise discarded URL."""
    parsed = urlparse(url)
    if parsed.netloc != "flow.google.com" or not parsed.path.endswith("/data/batchexecute"):
        return []
    values = parse_qs(parsed.query).get("rpcids", [])
    return sorted(
        {value for item in values for value in item.split(",") if _RPCID_RE.fullmatch(value)}
    )


def _state_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FlowPresubmitError(
            "AGENT_CANARY_STATE_UNREADABLE", "Flow state snapshot is unreadable"
        )
    return value


async def _wait_for_stable_video_state(page: Any) -> dict[str, Any]:
    """Wait for Flow's project state to hydrate and repeat consistently."""
    deadline = asyncio.get_running_loop().time() + _STATE_STABILITY_TIMEOUT_S
    previous: dict[str, Any] | None = None
    repeated = 0
    while asyncio.get_running_loop().time() < deadline:
        current = _state_dict(await page.evaluate(_VIDEO_STATE_JS))
        if current == previous:
            repeated += 1
        else:
            previous = current
            repeated = 1
        if repeated >= _STATE_STABILITY_SAMPLES:
            return current
        await page.wait_for_timeout(_STATE_STABILITY_INTERVAL_MS)
    raise FlowPresubmitError(
        "AGENT_CANARY_STATE_UNSTABLE", "TEST project state did not stabilize after hydration"
    )


async def run_canary(snapshot: Path, runtime_root: Path, temp_root: Path) -> dict[str, object]:
    """Run one intercepted Agent submit; never create or update a D2 ledger row."""
    request = build_phase_g_request_from_snapshot(
        snapshot.resolve(strict=True),
        d2_run_id=f"D2_{uuid4().hex[:16]}",
        runtime_state_root=runtime_root.resolve(strict=True),
        temp_root=temp_root.resolve(strict=True),
    )
    if (
        request.provider_account_id != "PVA_0001"
        or request.provider_project_id != _PROJECT_ID
        or request.output_count != 1
    ):
        raise FlowPresubmitError(
            "AGENT_CANARY_IDENTITY_MISMATCH", "snapshot is not the pinned TEST lane"
        )
    await run_fresh_presubmit(request, expected_email=_EMAIL)

    port = await GoogleFlowAgentOnlyGenerationPort.open(
        expected_email=_EMAIL,
        block_service_workers=True,
    )
    events: list[dict[str, object]] = []
    activated = False
    try:
        await port.bind_request(request)
        page = port._require_page()
        await _wait_for_stable_video_state(page)
        baseline = await port.capture_submission_baseline()
        if baseline.pending_count != 0:
            raise FlowPresubmitError(
                "AGENT_CANARY_PENDING_BASELINE", "TEST project has pending generation"
            )
        if await page.locator(_LIVE_GATE).count() != 0:
            raise FlowPresubmitError(
                "AGENT_CANARY_LIVE_GATE_PRESENT", "TEST project has a live credit gate"
            )
        directive = build_agent_only_directive(
            request.compiled_prompt_text,
            request.presubmit_request.preflight.technical_configuration,
        )
        await port.fill_prompt(directive, "")
        if await port.read_prompt() != directive:
            raise FlowPresubmitError(
                "AGENT_CANARY_PROMPT_READBACK", "Agent directive readback mismatched"
            )
        await port.arm_submit_request_observer()
        context = page.context

        async def intercept(route: Any) -> None:
            if not activated:
                await route.continue_()
                return
            request_obj = route.request
            events.append(
                {
                    "rpcids": _sanitized_rpcids(str(request_obj.url)),
                    "method": str(request_obj.method).upper(),
                    "timestamp": datetime.now(UTC)
                    .isoformat(timespec="milliseconds")
                    .replace("+00:00", "Z"),
                }
            )
            await route.abort()

        await context.route("**/*", intercept)
        activated = True
        await port.click_generate_once()
        await page.wait_for_timeout(3_000)
        activated = False
        await context.unroute("**/*", intercept)
        await page.reload(wait_until="domcontentloaded")
        await page.locator("flow-project-page").wait_for(state="visible", timeout=15_000)
        after = await _wait_for_stable_video_state(page)
        stable = True
        baseline_media = set(baseline.media_ids)
        after_media = set(port._valid_ids(after.get("media_ids")))
        baseline_tiles = set(baseline.finished_tile_key_hashes)
        after_tiles = set(port._valid_hashes(after.get("finished_tile_key_hashes")))
        pending_after = port._integer(after.get("pending"))
        live_gates_after = await page.locator(_LIVE_GATE).count()
        baseline_chat_count = int((baseline.agent_turn_id or "0:").partition(":")[0] or "0")
        rpc_events = [event for event in events if event["rpcids"]]
        passed = bool(
            port._submission_surface == "AGENT_ONLY"
            and events
            and rpc_events
            and stable
            and after_media <= baseline_media
            and after_tiles <= baseline_tiles
            and pending_after == 0
            and live_gates_after == 0
            and port._integer(after.get("chat_count")) == baseline_chat_count
        )
        return {
            "state": "INTERCEPTION_PASS" if passed else "HOLD",
            "agent_only_surface": port._submission_surface == "AGENT_ONLY",
            "test_project_verified": baseline.project_id == _PROJECT_ID,
            "intercepted_request_count": len(events),
            "rpcids": sorted({rpcid for event in rpc_events for rpcid in event["rpcids"]}),
            "rpc_inventory": rpc_events,
            "post_snapshot_stable": stable,
            "new_media_count": len(after_media - baseline_media),
            "new_finished_tile_count": len(after_tiles - baseline_tiles),
            "pending_after": pending_after,
            "live_gates_after": live_gates_after,
            "generate_clicks": 1,
            "approval_clicks": 0,
            "d2_ledger_used": False,
        }
    finally:
        activated = False
        await port.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--runtime-state-root", required=True, type=Path)
    parser.add_argument("--temp-root", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = asyncio.run(run_canary(args.snapshot, args.runtime_state_root, args.temp_root))
    except Exception as exc:  # noqa: BLE001 — report only a safe class/code
        result = {
            "state": "HOLD",
            "failure_class": type(exc).__name__,
            "reason_code": getattr(exc, "code", "AGENT_CANARY_FAILED"),
            "d2_ledger_used": False,
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["state"] == "INTERCEPTION_PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
