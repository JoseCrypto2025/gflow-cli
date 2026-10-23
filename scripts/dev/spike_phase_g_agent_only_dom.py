"""Read-only TEST-project DOM probe for Phase G Agent-only anchors; $0, no submit."""

from __future__ import annotations

import argparse
import asyncio
import json
from urllib.parse import urlparse

from gflow_cli.v2_runtime.live_flow import GoogleFlowAgentOnlyPort
from gflow_cli.v2_runtime.phase_g_live_flow import _LIVE_GATE

_EMAIL = "singleava508@gmail.com"
_PROJECT = "34e60953-e534-46e9-ac65-993893e61065"
_NAMES = {
    "prosemirror": "div.ProseMirror",
    "agent_submit": "flow-generate-icon-button button",
    "agent_inflight": "flow-stop-icon-button",
    "permission_component": "flow-permission-message",
    "live_gate": _LIVE_GATE,
    "video_tile": "flow-video-tile",
    "pending_tile": "flow-pending-tile",
    "video_option": "flow-a2ui-video-option",
}


async def run_probe() -> dict[str, object]:
    port = await GoogleFlowAgentOnlyPort.open(
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_account_id="PVA_0001",
        provider_project_id=_PROJECT,
        project_name="AI Video Automation V2 - TEST",
        model_display_names={"MDL_0001": "Veo 3.1 - Lite"},
        expected_email=_EMAIL,
        headless=False,
    )
    try:
        page = await port._ensure_page()
        await page.goto(
            f"https://flow.google.com/project/{_PROJECT}", wait_until="domcontentloaded"
        )
        await page.locator("flow-project-page").wait_for(state="visible", timeout=15_000)
        snapshots: list[dict[str, int]] = []
        for _ in range(20):
            snapshots.append(
                {name: await page.locator(selector).count() for name, selector in _NAMES.items()}
            )
            if len(snapshots) >= 3 and snapshots[-3] == snapshots[-2] == snapshots[-1]:
                break
            await page.wait_for_timeout(500)
        parsed = urlparse(page.url)
        stable = len(snapshots) >= 3 and snapshots[-3] == snapshots[-2] == snapshots[-1]
        return {
            "account_verified": port._session_email == _EMAIL.casefold(),
            "project_verified": parsed.netloc == "flow.google.com"
            and parsed.path.rstrip("/") == f"/project/{_PROJECT}",
            "surface": await port.inspect_surface(),
            "stable": stable,
            "snapshots": snapshots[-3:],
            "settings_changed": False,
            "prompt_submitted": False,
            "generate_clicked": False,
        }
    finally:
        await port.close()


def main() -> int:
    argparse.ArgumentParser(description=__doc__).parse_args()
    result = asyncio.run(run_probe())
    print(json.dumps(result, sort_keys=True))
    return (
        0 if result["account_verified"] and result["project_verified"] and result["stable"] else 2
    )


if __name__ == "__main__":
    raise SystemExit(main())
