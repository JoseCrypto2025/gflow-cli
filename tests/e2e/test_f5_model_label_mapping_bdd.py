"""Read-only live regression for Flow's model-label separator presentation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when

from gflow_cli.v2_runtime.google_flow import prompt_sha256
from gflow_cli.v2_runtime.live_flow import GoogleFlowAgentOnlyPort

scenarios("../features/f5_model_label_mapping.feature")
pytestmark = [pytest.mark.e2e, pytest.mark.e2e_auth]


@pytest.fixture
def world() -> dict[str, object]:
    return {}


@given("the isolated gflow production profile and pinned Flow project")
def _configured_prod(world: dict[str, object], e2e_profile_dir: Path) -> None:
    assert e2e_profile_dir.name == "profile_gflow-prod"
    world["profile"] = e2e_profile_dir


@when("the repository-owned Agent port reads the live baseline")
def _read_live_baseline(world: dict[str, object]) -> None:
    async def read() -> tuple[dict[str, object], dict[str, object]]:
        port = await GoogleFlowAgentOnlyPort.open(
            auth_profile_key="GFLOW_PROD_AUTH",
            provider_account_id="PVA_0002",
            provider_project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
            project_name="AI Video Automation V2 - PROD",
            model_display_names={"MDL_0001": "Veo 3.1 Lite"},
            expected_email="singleava508@gmail.com",
            headless=False,
        )
        try:
            session = await port.inspect_session()
            project = await port.select_project("aafe9282-071f-4333-baa4-92b9f76a9c27")
            surface = await port.inspect_surface()
            readback = await port.readback()
            baseline = {
                "model": readback.settings.get("model_id"),
                "aspect": readback.settings.get("aspect_ratio"),
                "output_count": readback.settings.get("output_count"),
                "prompt_sha256": prompt_sha256(readback.prompt or "", ""),
                "generate_present": readback.generate_present,
                "generate_enabled": readback.generate_enabled,
            }
            return (
                {
                    "session": session.status,
                    "project_id": project.project_id,
                    "surface": surface,
                },
                baseline,
            )
        finally:
            await port.close()

    metadata, baseline = asyncio.run(read())
    world.update(metadata)
    world["baseline"] = baseline
    print(json.dumps(baseline, sort_keys=True))


@then("the session project and Agent-only surface are verified")
def _verified_surface(world: dict[str, object]) -> None:
    assert world["session"] == "FLOW_SESSION_READY"
    assert world["project_id"] == "aafe9282-071f-4333-baa4-92b9f76a9c27"
    assert world["surface"] == "AGENT_ONLY"


@then("the displayed Veo label resolves uniquely to MDL_0001")
def _model_is_mapped(world: dict[str, object]) -> None:
    baseline = world["baseline"]
    assert isinstance(baseline, dict)
    assert baseline["model"] == "MDL_0001"
