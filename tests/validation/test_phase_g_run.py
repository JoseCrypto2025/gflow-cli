from __future__ import annotations

from pathlib import Path

import pytest

from gflow_cli.v2_runtime.phase_e import InMemorySceneCatalog, preflight_scene
from gflow_cli.v2_runtime.phase_g import PhaseGError
from gflow_cli.v2_runtime.phase_g_run import build_phase_g_request
from tests.validation.test_phase_e_prompt_preflight import _fixtures


def _prompt_rows() -> list[dict[str, object]]:
    return [
        {
            "Scene_Prompt_ID": "SPV_000002",
            "Scene_ID": "SCN_000001",
            "Generation_Profile_ID": "GPR_0001",
            "Prompt_Status": "APPROVED",
            "Compiled_Prompt_Text": "A person enters the room.",
            "Compiled_Negative_Prompt": "",
            "Source_Context_SHA256": "0" * 64,
            "Prompt_SHA256": "0" * 64,
        }
    ]


def test_d2_request_builder_fails_closed_on_nonmatching_live_prompt(tmp_path: Path) -> None:
    tables = _fixtures()
    preflight = preflight_scene(InMemorySceneCatalog(tables), "SCN_000001", "GPR_0001", "DEV")
    prompt = _prompt_rows()[0]
    prompt["Compiled_Prompt_Text"] = preflight.compiled_prompt or ""
    prompt["Compiled_Negative_Prompt"] = preflight.compiled_negative_prompt or ""
    prompt["Source_Context_SHA256"] = preflight.source_context_sha256 or ""
    prompt["Prompt_SHA256"] = "0" * 64
    with pytest.raises(PhaseGError, match="hash differs"):
        build_phase_g_request(
            tables,
            [prompt],
            d2_run_id="D2_test-run-0001",
            runtime_state_root=tmp_path / "state",
            temp_root=tmp_path / "temp",
        )


def test_d2_request_builder_requires_exactly_one_approved_prompt(tmp_path: Path) -> None:
    prompt = _prompt_rows()[0]
    prompt["Prompt_Status"] = "SUPERSEDED"
    with pytest.raises(PhaseGError, match="exactly one current APPROVED"):
        build_phase_g_request(
            _fixtures(),
            [prompt],
            d2_run_id="D2_test-run-0001",
            runtime_state_root=tmp_path / "state",
            temp_root=tmp_path / "temp",
        )
