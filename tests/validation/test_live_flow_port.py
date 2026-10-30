from __future__ import annotations

from types import SimpleNamespace

import pytest

from gflow_cli.v2_runtime.errors import FlowPresubmitError
from gflow_cli.v2_runtime.live_flow import (
    _GENERATE_BUTTON,
    _PROMPT_SELECTOR,
    GoogleFlowAgentOnlyPort,
    _contenteditable_prompt_text,
    classify_agent_surface_evidence,
    resolve_model_id_from_display,
)


def _port() -> GoogleFlowAgentOnlyPort:
    return GoogleFlowAgentOnlyPort(
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_account_id="PVA_0001",
        provider_project_id="34e60953-e534-46e9-ac65-993893e61065",
        project_name="AI Video Automation V2 - TEST",
        model_display_names={"MDL_0001": "Veo 3.1 - Lite"},
    )


def test_live_port_resolves_repository_owned_profile_and_exposes_no_submit_api() -> None:
    port = _port()

    assert port.profile_dir.name == "profile_gflow-v2-test"
    assert port.profile_dir.is_absolute()
    assert not any(
        hasattr(port, name)
        for name in ("submit", "generate", "click_generate", "send_generation_request")
    )


def test_agent_only_controls_use_the_measured_structural_anchors() -> None:
    assert _PROMPT_SELECTOR == "div.ProseMirror"
    assert _GENERATE_BUTTON == "flow-generate-icon-button button"


def test_contenteditable_prompt_readback_preserves_paragraphs_and_blank_lines() -> None:
    assert _contenteditable_prompt_text(["first line", "", "third line"]) == (
        "first line\n\nthird line"
    )


def test_model_display_mapping_allows_hyphen_spacing_and_rejects_ambiguity() -> None:
    assert resolve_model_id_from_display("Veo 3.1 - Lite", {"MDL_0001": "Veo 3.1 Lite"}) == (
        "MDL_0001"
    )
    assert (
        resolve_model_id_from_display(
            "Veo 3.1 - Lite",
            {"MDL_0001": "Veo 3.1 Lite", "MDL_0002": "Veo 3.1-Lite"},
        )
        is None
    )


@pytest.mark.parametrize(
    ("observed", "configured", "expected"),
    [
        ("Veo 3.1 Lite", "Veo 3.1 - Lite", "MDL_0001"),
        ("Veo 3.1 - Fast", "Veo 3.1 Lite", None),
        ("Veo 3.2 - Lite", "Veo 3.1 Lite", None),
    ],
)
def test_model_display_mapping_preserves_version_and_tier(
    observed: str, configured: str, expected: str | None
) -> None:
    assert resolve_model_id_from_display(observed, {"MDL_0001": configured}) == expected


def test_live_port_rejects_personal_or_unmapped_auth_profile_names() -> None:
    with pytest.raises(ValueError, match="not mapped"):
        GoogleFlowAgentOnlyPort(
            auth_profile_key="AVA",
            provider_account_id="PVA_0001",
            provider_project_id="34e60953-e534-46e9-ac65-993893e61065",
            project_name="AI Video Automation V2 - TEST",
            model_display_names={"MDL_0001": "Veo 3.1 - Lite"},
        )


def test_select_project_mismatch_and_not_ready_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port()

    class Page:
        url = "https://flow.google.com/project/34e60953-e534-46e9-ac65-993893e61065"

        async def goto(self, *_args: object, **_kwargs: object) -> None:
            return None

        def locator(self, _selector: str) -> SimpleNamespace:
            async def wait_for(**_kwargs: object) -> None:
                return None

            return SimpleNamespace(wait_for=wait_for)

        async def title(self) -> str:
            return "Wrong Flow Project"

    page = Page()

    async def ensure_page() -> Page:
        return page

    monkeypatch.setattr(port, "_ensure_page", ensure_page)
    with pytest.raises(FlowPresubmitError) as mismatch:
        port.select_project(port.provider_project_id).send(None)
    assert mismatch.value.code == "FLOW_PROJECT_MISMATCH"

    async def unavailable_page() -> Page:
        page.url = "https://flow.google.com/"

        async def fail_goto(*_args: object, **_kwargs: object) -> None:
            raise TimeoutError("unavailable")

        page.goto = fail_goto  # type: ignore[method-assign]
        return page

    monkeypatch.setattr(port, "_ensure_page", unavailable_page)
    with pytest.raises(FlowPresubmitError) as not_ready:
        port.select_project(port.provider_project_id).send(None)
    assert not_ready.value.code == "FLOW_PROJECT_NOT_READY"


@pytest.mark.parametrize(
    ("trigger_present", "trigger_visible", "agent_panel_visible", "mode_chip_present", "expected"),
    [
        (True, True, False, True, "CLASSIC"),
        (True, False, True, False, "AGENT_ONLY"),
        (True, False, True, True, "UNKNOWN"),
        (False, False, True, False, "UNKNOWN"),
        (True, False, False, False, "UNKNOWN"),
    ],
)
def test_surface_resolver_requires_known_classic_or_agent_signature(
    trigger_present: bool,
    trigger_visible: bool,
    agent_panel_visible: bool,
    mode_chip_present: bool,
    expected: str,
) -> None:
    assert (
        classify_agent_surface_evidence(
            trigger_present=trigger_present,
            trigger_visible=trigger_visible,
            agent_panel_visible=agent_panel_visible,
            mode_chip_present=mode_chip_present,
        )
        == expected
    )


@pytest.mark.asyncio
async def test_agent_port_rejects_unmapped_parameter_before_browser_mutation() -> None:
    port = _port()

    with pytest.raises(FlowPresubmitError) as exc:
        await port.apply_settings({"duration_sec": 8})

    assert exc.value.code == "FLOW_AGENT_SETTING_UNSUPPORTED"
    assert port._context is None


@pytest.mark.asyncio
async def test_agent_port_supports_only_text_to_video_contract() -> None:
    port = _port()

    with pytest.raises(FlowPresubmitError) as exc:
        await port.apply_settings({"generation_mode": "FIRST_FRAME_TO_VIDEO"})

    assert exc.value.code == "FLOW_AGENT_MODE_UNSUPPORTED"
    assert port._context is None
