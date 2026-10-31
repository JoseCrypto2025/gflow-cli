from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import gflow_cli.v2_runtime.flow_pricing_inspector as inspector
from gflow_cli.auth.verification import FlowSessionOutcome
from gflow_cli.v2_runtime.flow_pricing_inspector import (
    EXPECTED_EMAIL,
    EXPECTED_PROFILE_PATH,
    EXPECTED_PROJECT_ID,
    FlowPricingObservation,
    _runtime_error_code,
    build_inspection_report,
    extract_pre_submit_quote,
    inspect_with_port,
    validate_profile_path,
)
from gflow_cli.v2_runtime.live_flow import GoogleFlowAgentOnlyPort


def _observation(**overrides: Any) -> FlowPricingObservation:
    values: dict[str, Any] = {
        "account_email": EXPECTED_EMAIL,
        "project_id": EXPECTED_PROJECT_ID,
        "locale": "en",
        "settings": {
            "model_id": "MDL_0001",
            "generation_mode": "TEXT_TO_VIDEO",
            "aspect_ratio": "9:16",
            "output_count": 1,
        },
        "generate_control_label": "Generate 15 credits",
        "observed_at": datetime(2026, 10, 27, tzinfo=UTC),
        "screenshot_evidence": (),
        "software_commit": "a" * 40,
    }
    values.update(overrides)
    return FlowPricingObservation(**values)


def _run_immediate(coroutine: Any) -> Any:
    """Drive a coroutine whose test doubles complete without suspension."""
    try:
        coroutine.send(None)
    except StopIteration as result:
        return result.value
    raise AssertionError("test coroutine unexpectedly suspended")


def test_expected_profile_path_is_accepted() -> None:
    assert validate_profile_path(EXPECTED_PROFILE_PATH, EXPECTED_PROFILE_PATH) == (
        EXPECTED_PROFILE_PATH.resolve(strict=False)
    )


def test_wrong_profile_path_is_rejected() -> None:
    with pytest.raises(ValueError, match="PROFILE_MISMATCH"):
        validate_profile_path(Path("/tmp/profile_gflow-prod"), EXPECTED_PROFILE_PATH)


def test_wrong_account_is_reported_without_returning_unexpected_email() -> None:
    report = build_inspection_report(_observation(account_email="other@example.com"))
    assert report["ACCOUNT_MATCH"] == "FAIL"
    assert report["FLOW_ACCOUNT"] is None
    assert report["ACTIVE_MODEL_NAME"] is None
    assert report["REASON_CODE"] == "FLOW_ACCOUNT_MISMATCH"


def test_wrong_project_is_reported_without_reading_pricing_values() -> None:
    report = build_inspection_report(_observation(project_id="other-project"))
    assert report["PROJECT_MATCH"] == "FAIL"
    assert report["FLOW_PROJECT_ID"] is None
    assert report["DISPLAYED_CREDIT_COST"] is None
    assert report["REASON_CODE"] == "FLOW_PROJECT_MISMATCH"


def test_model_mismatch_is_reported_not_corrected() -> None:
    report = build_inspection_report(
        _observation(
            settings={
                "model_id": "MDL_OTHER",
                "model_name": "Veo 3.1 - Fast",
                "aspect_ratio": "9:16",
                "output_count": 1,
            }
        )
    )
    assert report["ACTIVE_MODEL_NAME"] == "Veo 3.1 - Fast"
    assert report["REASON_CODE"] == "FLOW_MODEL_CONFIG_MISMATCH"
    assert report["INSPECTION_STATUS"] == "HOLD"


def test_lower_priority_model_variant_is_reported_as_config_mismatch() -> None:
    report = build_inspection_report(
        _observation(
            settings={
                "model_id": "MDL_0001",
                "model_name": "Veo 3.1 Lite [Lower Priority]",
                "model_priority": "LOWER_PRIORITY",
                "aspect_ratio": "9:16",
                "output_count": 1,
            }
        )
    )
    assert report["ACTIVE_MODEL_PRIORITY"] == "LOWER_PRIORITY"
    assert report["ACTIVE_MODEL_NAME"] == "Veo 3.1 Lite [Lower Priority]"
    assert report["REASON_CODE"] == "FLOW_MODEL_CONFIG_MISMATCH"


def test_visible_configuration_mismatch_is_reported_not_corrected() -> None:
    report = build_inspection_report(
        _observation(settings={"model_id": "MDL_0001", "aspect_ratio": "16:9", "output_count": 2})
    )
    assert report["VISIBLE_ASPECT_RATIO"] == "16:9"
    assert report["VISIBLE_OUTPUT_COUNT"] == 2
    assert report["CONFIG_MATCH"] == "MISMATCH"
    assert report["INSPECTION_STATUS"] == "HOLD"


def test_pre_submit_quote_is_read_from_the_generate_control_label_only() -> None:
    quote = extract_pre_submit_quote("Generate 15 credits", locale="en")
    assert quote == ("15", "credits", "Generate control label")


def test_missing_or_ambiguous_quote_is_explicitly_unavailable() -> None:
    assert extract_pre_submit_quote("Generate", locale="en") is None
    assert extract_pre_submit_quote("15 credits", locale="en") is None
    assert extract_pre_submit_quote("Generate 15 or 30 credits", locale="en") is None
    assert extract_pre_submit_quote("Generate 15 credits", locale="pt-BR") is None


def test_image_output_x2_is_not_read_as_video_duration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = ["16:9", "x2", "9:16", "x1"]

    class SelectedRadio:
        def __init__(self, label: str) -> None:
            self.label = label

        async def count(self) -> int:
            return 1

        async def inner_text(self) -> str:
            return self.label

    class RadioGroup:
        def __init__(self, label: str) -> None:
            self.label = label

        def locator(self, _: str) -> SelectedRadio:
            return SelectedRadio(self.label)

    class Groups:
        def __init__(self) -> None:
            self.read_indices: list[int] = []

        async def count(self) -> int:
            return 4

        def nth(self, index: int) -> RadioGroup:
            self.read_indices.append(index)
            return RadioGroup(labels[index])

    groups = Groups()

    class ModelButton:
        async def inner_text(self) -> str:
            return "Veo 3.1 Lite"

    class Confirmation:
        async def count(self) -> int:
            return 1

        async def get_attribute(self, _: str) -> str:
            return "2"

    class SettingsView:
        def locator(self, selector: str) -> object:
            if selector == 'button[aria-label="Video generation default model"]':
                return ModelButton()
            if selector == "mat-button-toggle-group":
                return groups
            if selector == 'input[type="radio"]:checked':
                return Confirmation()
            raise AssertionError(f"unexpected read selector: {selector}")

        async def wait_for(self, **_: object) -> None:
            return None

    class Page:
        def locator(self, _: str) -> SettingsView:
            return SettingsView()

    port = GoogleFlowAgentOnlyPort(
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_account_id="PVA_0001",
        provider_project_id="34e60953-e534-46e9-ac65-993893e61065",
        project_name="AI Video Automation V2 - TEST",
        model_display_names={"MDL_0001": "Veo 3.1 Lite"},
    )

    async def ensure_page() -> Page:
        return Page()

    monkeypatch.setattr(port, "_ensure_page", ensure_page)
    inspector_settings = _run_immediate(port._read_video_settings(allow_unmapped_model=True))
    presubmit_settings = _run_immediate(port._read_video_settings())

    assert inspector_settings["duration"] is None
    assert inspector_settings["duration_selector_confidence"] == "NONE"
    assert 1 not in groups.read_indices
    assert presubmit_settings.get("duration") is None


def test_missing_expected_configuration_is_unknown_and_holds_quote_authority() -> None:
    report = build_inspection_report(
        _observation(
            settings={
                "model_id": "MDL_0001",
                "model_name": "Veo 3.1 Lite",
                "model_priority": "STANDARD",
                "aspect_ratio": "9:16",
                "output_count": 1,
            }
        )
    )
    assert report["CONFIG_MATCH"] == "UNKNOWN"
    assert report["DISPLAYED_CREDIT_COST"] == "15"
    assert report["CREDIT_COST_AUTHORITATIVE"] == "NO"
    assert report["INSPECTION_STATUS"] == "HOLD"


def test_duration_without_authoritative_selector_confidence_is_unavailable() -> None:
    report = build_inspection_report(
        _observation(
            settings={
                "model_id": "MDL_0001",
                "model_name": "Veo 3.1 Lite",
                "model_priority": "STANDARD",
                "duration": 2,
                "aspect_ratio": "9:16",
                "output_count": 1,
            }
        )
    )

    assert report["VISIBLE_DURATION"] == "UNKNOWN"
    assert report["DURATION_READBACK"] == "UNAVAILABLE"
    assert report["DURATION_SELECTOR_CONFIDENCE"] == "NONE"
    assert report["CONFIG_MATCH"] == "UNKNOWN"
    assert report["CREDIT_COST_AUTHORITATIVE"] == "NO"


def test_complete_expected_configuration_allows_quote_authority() -> None:
    report = build_inspection_report(
        _observation(
            settings={
                "model_id": "MDL_0001",
                "model_name": "Veo 3.1 Lite",
                "model_priority": "STANDARD",
                "duration": 8,
                "duration_selector_confidence": "AUTHORITATIVE",
                "aspect_ratio": "9:16",
                "output_count": 1,
            }
        )
    )
    assert report["CONFIG_MATCH"] == "PASS"
    assert report["DURATION_READBACK"] == 8
    assert report["DURATION_SELECTOR_CONFIDENCE"] == "AUTHORITATIVE"
    assert report["CREDIT_COST_AUTHORITATIVE"] == "YES"
    assert report["INSPECTION_STATUS"] == "PASS"


def test_missing_pre_submit_quote_holds_even_when_configuration_matches() -> None:
    report = build_inspection_report(
        _observation(
            generate_control_label="Generate",
            settings={
                "model_id": "MDL_0001",
                "model_name": "Veo 3.1 Lite",
                "model_priority": "STANDARD",
                "duration": 8,
                "duration_selector_confidence": "AUTHORITATIVE",
                "aspect_ratio": "9:16",
                "output_count": 1,
            },
        )
    )
    assert report["CONFIG_MATCH"] == "PASS"
    assert report["REASON_CODE"] == "PRE_SUBMIT_CREDIT_COST_NOT_VISIBLE"
    assert report["CREDIT_COST_AUTHORITATIVE"] == "NO"
    assert report["INSPECTION_STATUS"] == "HOLD"


def test_native_chromium_profile_lock_maps_to_profile_in_use() -> None:
    assert _runtime_error_code(RuntimeError("ProcessSingleton: profile is locked")) == (
        "PROFILE_IN_USE"
    )
    assert _runtime_error_code(RuntimeError("unrelated failure")) == "FLOW_INSPECTION_FAILED"


def test_report_never_contains_credentials_or_raw_ui_text() -> None:
    report = build_inspection_report(
        _observation(
            generate_control_label="Generate 15 credits", settings={"model_id": "MDL_0001"}
        )
    )
    encoded = repr(report).casefold()
    assert "cookie" not in encoded
    assert "token" not in encoded
    assert "other@example.com" not in encoded
    assert "generate 15 credits" not in encoded


def test_report_does_not_return_unrecognized_screenshot_paths(tmp_path: Path) -> None:
    private_path = tmp_path / "unrelated-private-file.png"
    private_path.write_bytes(b"not an evidence image")
    report = build_inspection_report(_observation(screenshot_evidence=(str(private_path),)))
    assert report["SCREENSHOT_EVIDENCE"] == []


def test_inspector_calls_only_read_only_port_methods() -> None:
    class Locator:
        async def count(self) -> int:
            return 1

        async def is_visible(self) -> bool:
            return True

        async def inner_text(self) -> str:
            return "Generate 15 credits"

    class Page:
        async def evaluate(self, _: str) -> str:
            return "en"

        def locator(self, _: str) -> Locator:
            return Locator()

    class ReadOnlyPort:
        session_email = EXPECTED_EMAIL

        def __init__(self) -> None:
            self.called: list[str] = []

        async def select_project(self, project_id: str) -> object:
            self.called.append("select_project")
            return type("Project", (), {"project_id": project_id})()

        async def inspect_surface(self) -> str:
            self.called.append("inspect_surface")
            return "AGENT_ONLY"

        async def snapshot_settings(
            self,
            *,
            screenshot_path: Path | None = None,
            allow_unmapped_model: bool = False,
        ) -> dict[str, object]:
            self.called.append("snapshot_settings")
            return {
                "model_id": "MDL_0001",
                "model_name": "Veo 3.1 Lite",
                "model_priority": "STANDARD",
                "generation_mode": "TEXT_TO_VIDEO",
                "duration": 8,
                "duration_selector_confidence": "AUTHORITATIVE",
                "aspect_ratio": "9:16",
                "output_count": 1,
            }

        async def active_page(self) -> object:
            self.called.append("active_page")
            return Page()

        async def read_generate_control_label(
            self, *, screenshot_path: Path | None = None
        ) -> str | None:
            self.called.append("read_generate_control_label")
            return "Generate 15 credits"

    port = ReadOnlyPort()
    report = _run_immediate(
        inspect_with_port(
            port,
            expected_email=EXPECTED_EMAIL,
            expected_project_id=EXPECTED_PROJECT_ID,
            screenshot_paths=None,
            software_commit="a" * 40,
        )
    )
    assert report["INSPECTION_STATUS"] == "PASS"
    assert set(port.called) == {
        "select_project",
        "inspect_surface",
        "snapshot_settings",
        "active_page",
        "read_generate_control_label",
    }
    assert "apply_settings" not in port.called
    assert "fill_prompt" not in port.called


def test_inspector_source_has_no_submit_or_settings_write_call() -> None:
    source = Path(__file__).parents[2] / "src/gflow_cli/v2_runtime/flow_pricing_inspector.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    called_names = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert called_names.isdisjoint(
        {
            "apply_settings",
            "fill_prompt",
            "fill_negative_prompt",
            "click_generate",
            "submit",
            "generate",
            "approve",
            "FlowApiClient",
            "click",
        }
    )


def test_profile_lock_contention_stops_before_browser_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class BusyLease:
        def __init__(self, _: Path) -> None:
            pass

        def try_acquire(self) -> bool:
            return False

    class Port:
        @classmethod
        async def open(cls, **_: object) -> object:
            raise AssertionError("browser must not open while profile is held")

    original_is_dir = Path.is_dir

    def profile_is_dir(path: Path) -> bool:
        if path == EXPECTED_PROFILE_PATH.resolve(strict=False):
            return True
        return original_is_dir(path)

    monkeypatch.setattr(inspector.auth, "profile_dir", lambda _: EXPECTED_PROFILE_PATH)
    monkeypatch.setattr(inspector, "ProfileLease", BusyLease)
    monkeypatch.setattr(inspector, "GoogleFlowAgentOnlyPort", Port)
    monkeypatch.setattr(Path, "is_dir", profile_is_dir)
    report = _run_immediate(
        inspector.inspect_flow_pricing_read_only(
            provider_account_id="PVA_0002",
            expected_project_id=EXPECTED_PROJECT_ID,
            expected_email=EXPECTED_EMAIL,
            runtime_state_root=tmp_path,
            software_commit="a" * 40,
        )
    )
    assert report["REASON_CODE"] == "PROFILE_IN_USE"
    assert report["INSPECTION_STATUS"] == "HOLD"


def test_inspector_passes_held_profile_lease_into_auth_verification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class HeldLease:
        def __init__(self, _: Path) -> None:
            self.acquired = False
            self.released = False

        def try_acquire(self) -> bool:
            self.acquired = True
            return True

        def release(self) -> None:
            self.released = True

    lease: HeldLease | None = None

    class TrackedLease(HeldLease):
        def __init__(self, path: Path) -> None:
            nonlocal lease
            super().__init__(path)
            lease = self

    original_is_dir = Path.is_dir

    def profile_is_dir(path: Path) -> bool:
        if path == EXPECTED_PROFILE_PATH.resolve(strict=False):
            return True
        return original_is_dir(path)

    seen: dict[str, object] = {}

    async def verify(
        _profile: Path, *, source: str, profile_lease: object | None = None
    ) -> SimpleNamespace:
        seen["source"] = source
        seen["lease"] = profile_lease
        return SimpleNamespace(
            outcome=FlowSessionOutcome.AUTHENTICATED,
            user_email=EXPECTED_EMAIL,
        )

    monkeypatch.setattr(inspector.auth, "profile_dir", lambda _: EXPECTED_PROFILE_PATH)
    monkeypatch.setattr(inspector, "ProfileLease", TrackedLease)
    monkeypatch.setattr(Path, "is_dir", profile_is_dir)
    monkeypatch.setattr("gflow_cli.v2_runtime.live_flow.verify_flow_profile", verify)
    monkeypatch.setattr(
        inspector,
        "inspect_with_port",
        lambda *_args, **_kwargs: _immediate_async_value({"INSPECTION_STATUS": "READY"}),
    )

    report = _run_immediate(
        inspector.inspect_flow_pricing_read_only(
            provider_account_id="PVA_0002",
            expected_project_id=EXPECTED_PROJECT_ID,
            expected_email=EXPECTED_EMAIL,
            runtime_state_root=tmp_path,
            software_commit="a" * 40,
        )
    )

    assert report["INSPECTION_STATUS"] == "READY"
    assert seen["source"] == "v2-agent-only"
    assert seen["lease"] is lease
    assert lease is not None and lease.acquired and lease.released


def _immediate_async_value(value: object) -> Any:
    async def _value() -> object:
        return value

    return _value()
