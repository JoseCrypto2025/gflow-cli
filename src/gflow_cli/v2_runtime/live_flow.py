"""Concrete, submission-free Playwright port for the Flow Agent-only surface.

The port uses the repository auth profile resolver and browser lifecycle. It
does not expose a submit, Generate, or keyboard-commit method. Only controls
that are present in the observed Agent settings view are writable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import cast
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Locator, Page, Playwright

from gflow_cli import auth
from gflow_cli.api.transports.migrated_composer import AGENT_MODE_CHIP_ANY, READY_ANCHOR
from gflow_cli.auth.internal_chromium import login_launch_kwargs
from gflow_cli.auth.verification import FlowSessionOutcome, verify_flow_profile
from gflow_cli.browser_manager import channel_for_profile, ensure_profile_engine_compatible

from .errors import FlowPresubmitError
from .google_flow import (
    FlowPresubmitRequest,
    FlowPresubmitResult,
    FlowProjectState,
    FlowReadback,
    FlowSessionState,
    GoogleFlowProviderAdapter,
    ProviderReferenceBinding,
)

_FLOW_ORIGIN = "https://flow.google.com"
_PROMPT_SELECTOR = "div.ProseMirror"
_SETTINGS_BUTTON = "flow-agent-panel button:has(mat-icon:text-is('tune'))"
_SETTINGS_VIEW = "flow-settings-view"
_VIDEO_MODEL_BUTTON = 'button[aria-label="Video generation default model"]'
_SAVE_BUTTON = "button.settings-save-button"
_BACK_BUTTON = "flow-agent-panel button:has(mat-icon:text-is('arrow_back'))"
_GENERATE_BUTTON = "flow-generate-icon-button button"
_AUTH_PROFILE_NAMES = {
    "GFLOW_V2_TEST_AUTH": "gflow-v2-test",
    "GFLOW_PROD_AUTH": "gflow-prod",
}
_SETTING_KEYS = frozenset(
    {
        "model_id",
        "generation_mode",
        "aspect_ratio",
        "output_count",
        "confirm_before_generating",
    }
)


def _contenteditable_prompt_text(blocks: Sequence[str]) -> str:
    """Reconstruct Flow's ProseMirror paragraph blocks as the filled prompt."""
    return "\n".join(blocks)


def resolve_model_id_from_display(
    display_text: str, model_display_names: Mapping[str, str]
) -> str | None:
    """Resolve Flow's model label despite harmless hyphen/spacing presentation drift."""
    normalized_display = re.sub(r"[\s-]+", " ", display_text.casefold()).strip()
    matches = [
        model_id
        for model_id, label in model_display_names.items()
        if re.sub(r"[\s-]+", " ", label.casefold()).strip() == normalized_display
    ]
    return matches[0] if len(matches) == 1 else None


async def _read_prompt_field(locator: Locator) -> str:
    """Read the current prompt value without confusing DOM text with input value."""
    raw_value: object = await locator.evaluate(
        """(element) => {
          const tag = element.tagName.toLowerCase();
          if (tag === "input" || tag === "textarea") {
            return {kind: "value", value: element.value};
          }
          if (!element.isContentEditable) {
            return {kind: "text", value: element.innerText || ""};
          }
          const blocks = Array.from(element.children);
          if (blocks.every((block) => block.tagName === "P")) {
            return {
              kind: "blocks",
              blocks: blocks.map((block) => block.textContent || ""),
            };
          }
          return {kind: "text", value: element.innerText || ""};
        }"""
    )
    if not isinstance(raw_value, dict):
        raise FlowPresubmitError(
            "FLOW_PROMPT_READBACK_UNSUPPORTED", "Flow prompt readback shape is unsupported"
        )
    value = cast(dict[str, object], raw_value)
    kind = value.get("kind")
    if kind in {"value", "text"}:
        text = value.get("value")
        return text if isinstance(text, str) else ""
    if kind == "blocks":
        blocks = value.get("blocks")
        if isinstance(blocks, list):
            block_values = cast(list[object], blocks)
            text_blocks = [block for block in block_values if isinstance(block, str)]
            if len(text_blocks) == len(block_values):
                return _contenteditable_prompt_text(text_blocks)
    raise FlowPresubmitError(
        "FLOW_PROMPT_READBACK_UNSUPPORTED", "Flow prompt readback shape is unsupported"
    )


def classify_agent_surface_evidence(
    *,
    trigger_present: bool,
    trigger_visible: bool,
    agent_panel_visible: bool,
    mode_chip_present: bool,
) -> str:
    """Classify only the known classic and #799 Agent-only DOM signatures."""
    if trigger_present and trigger_visible:
        return "CLASSIC"
    if trigger_present and not trigger_visible and agent_panel_visible and not mode_chip_present:
        return "AGENT_ONLY"
    return "UNKNOWN"


async def prepare_live_flow(
    request: FlowPresubmitRequest,
    *,
    auth_profile_key: str,
    project_name: str,
    model_display_names: Mapping[str, str],
    expected_email: str | None = None,
    headless: bool = False,
) -> FlowPresubmitResult:
    """Open the mapped auth profile, run F5, and always close the browser."""
    port = await GoogleFlowAgentOnlyPort.open(
        auth_profile_key=auth_profile_key,
        provider_account_id=request.provider_account_id,
        provider_project_id=request.provider_project_id,
        project_name=project_name,
        model_display_names=model_display_names,
        expected_email=expected_email,
        headless=headless,
    )
    try:
        return await GoogleFlowProviderAdapter(request.provider_id).prepare(request, port)
    finally:
        await port.close()


class GoogleFlowAgentOnlyPort:
    """Live Flow port with profile-safe auth and no generation capability."""

    def __init__(
        self,
        *,
        auth_profile_key: str,
        provider_account_id: str,
        provider_project_id: str,
        project_name: str,
        model_display_names: Mapping[str, str],
        expected_email: str | None = None,
        headless: bool = False,
        block_service_workers: bool = False,
    ) -> None:
        profile_name = _AUTH_PROFILE_NAMES.get(auth_profile_key)
        if not profile_name:
            raise ValueError("auth profile key is not mapped to an isolated gflow profile")
        if not provider_account_id or not provider_project_id or not project_name:
            raise ValueError("profile, account, project ID, and project name are required")
        if (
            auth_profile_key == "GFLOW_V2_TEST_AUTH"
            and project_name != "AI Video Automation V2 - TEST"
        ):
            raise ValueError("TEST auth profile is pinned to the dedicated TEST project")
        if (
            auth_profile_key == "GFLOW_PROD_AUTH"
            and project_name != "AI Video Automation V2 - PROD"
        ):
            raise ValueError("PROD auth profile is pinned to the dedicated PROD project")
        self.auth_profile_key = auth_profile_key
        self.profile_name = profile_name
        self.profile_dir = auth.profile_dir(profile_name)
        self.provider_account_id = provider_account_id
        self.provider_project_id = provider_project_id
        self.project_name = project_name
        self.model_display_names = dict(model_display_names)
        self.expected_email = expected_email.casefold() if expected_email else None
        self.headless = headless
        self.block_service_workers = block_service_workers
        self._session_verified = False
        self._session_email: str | None = None
        self._playwright: Playwright | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._surface: str | None = None
        self._negative_prompt_supported = False

    @classmethod
    async def open(
        cls,
        *,
        auth_profile_key: str,
        provider_account_id: str,
        provider_project_id: str,
        project_name: str,
        model_display_names: Mapping[str, str],
        expected_email: str | None = None,
        headless: bool = False,
        block_service_workers: bool = False,
    ) -> GoogleFlowAgentOnlyPort:
        """Verify the existing profile session without starting a login flow."""
        port = cls(
            auth_profile_key=auth_profile_key,
            provider_account_id=provider_account_id,
            provider_project_id=provider_project_id,
            project_name=project_name,
            model_display_names=model_display_names,
            expected_email=expected_email,
            headless=headless,
            block_service_workers=block_service_workers,
        )
        status = await verify_flow_profile(port.profile_dir, source="v2-agent-only")
        if status.outcome is not FlowSessionOutcome.AUTHENTICATED:
            raise FlowPresubmitError(
                "FLOW_AUTH_NOT_READY", "repository auth verification did not confirm a Flow session"
            )
        email = status.user_email.casefold() if status.user_email else None
        if port.expected_email and email != port.expected_email:
            raise FlowPresubmitError(
                "FLOW_ACCOUNT_MISMATCH", "verified profile identity differs from configured account"
            )
        port._session_email = email
        port._session_verified = True
        return port

    async def close(self) -> None:
        """Close the browser context and Playwright process; never save settings."""
        if self._context is not None:
            await self._context.close()
            self._context = None
            self._page = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def inspect_session(self) -> FlowSessionState:
        if not self._session_verified:
            raise FlowPresubmitError("FLOW_AUTH_NOT_READY", "profile session was not verified")
        await self._ensure_page()
        return FlowSessionState("FLOW_SESSION_READY", self.provider_account_id)

    async def active_page(self) -> Page:
        """Expose the repository-owned page to a separate, bound extension port."""
        return await self._ensure_page()

    async def select_project(self, project_id: str) -> FlowProjectState:
        if project_id != self.provider_project_id:
            raise FlowPresubmitError(
                "FLOW_PROJECT_MISMATCH", "port is pinned to a different project"
            )
        page = await self._ensure_page()
        target_url = f"{_FLOW_ORIGIN}/project/{project_id}"
        if page.url != target_url:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=45_000)
        await page.locator("flow-agent-panel").wait_for(state="visible", timeout=30_000)
        parsed = urlparse(page.url)
        if parsed.scheme != "https" or parsed.netloc != "flow.google.com":
            return FlowProjectState(None)
        match = re.fullmatch(r"/project/([A-Za-z0-9_-]+)", parsed.path)
        title = await page.title()
        selected = match.group(1) if match else None
        if selected != project_id or self.project_name.casefold() not in title.casefold():
            return FlowProjectState(None)
        return FlowProjectState(selected)

    async def inspect_surface(self) -> str | None:
        page = await self._ensure_page()
        locale = await page.evaluate("document.documentElement.lang || navigator.language")
        if not isinstance(locale, str) or not locale.casefold().startswith("en"):
            self._surface = "UNKNOWN"
            return self._surface
        trigger = page.locator(READY_ANCHOR).first
        agent_panel = page.locator("flow-agent-panel")
        trigger_present = bool(await trigger.count())
        trigger_visible = bool(trigger_present and await trigger.is_visible())
        panel_visible = bool(await agent_panel.count() and await agent_panel.is_visible())
        chip_present = bool(await page.locator(AGENT_MODE_CHIP_ANY).count())
        # Agent-only requires a hidden existing settings anchor, visible Agent
        # panel, and no mode chip; an absent anchor remains UNKNOWN.
        self._surface = classify_agent_surface_evidence(
            trigger_present=trigger_present,
            trigger_visible=trigger_visible,
            agent_panel_visible=panel_visible,
            mode_chip_present=chip_present,
        )
        return self._surface

    async def snapshot_settings(self) -> Mapping[str, object]:
        await self._ensure_page()
        await self._open_settings()
        try:
            return await self._read_video_settings()
        finally:
            await self._close_settings()

    async def apply_settings(self, settings: Mapping[str, object]) -> Mapping[str, object]:
        unsupported = set(settings) - _SETTING_KEYS
        if unsupported:
            raise FlowPresubmitError(
                "FLOW_AGENT_SETTING_UNSUPPORTED",
                "Agent settings do not expose one or more requested controls",
            )
        if settings.get("generation_mode", "TEXT_TO_VIDEO") != "TEXT_TO_VIDEO":
            raise FlowPresubmitError(
                "FLOW_AGENT_MODE_UNSUPPORTED", "Agent-only F5 supports text-to-video only"
            )
        page = await self._ensure_page()
        await self._open_settings()
        try:
            current = await self._read_video_settings()
            if "model_id" in settings and settings["model_id"] != current.get("model_id"):
                await self._select_model(str(settings["model_id"]))
            if "aspect_ratio" in settings and settings["aspect_ratio"] != current.get(
                "aspect_ratio"
            ):
                await self._select_video_radio(2, str(settings["aspect_ratio"]))
            if "output_count" in settings and settings["output_count"] != current.get(
                "output_count"
            ):
                await self._select_video_radio(3, f"x{settings['output_count']}")
            if "confirm_before_generating" in settings and settings[
                "confirm_before_generating"
            ] != current.get("confirm_before_generating"):
                await self._select_confirmation(str(settings["confirm_before_generating"]))
            await page.locator(_SAVE_BUTTON).click(timeout=5_000)
            await page.locator(_SETTINGS_VIEW).wait_for(state="hidden", timeout=10_000)
        finally:
            # Save may leave the panel open on a future UI revision. Back is
            # read-only navigation and does not alter persisted defaults.
            if await page.locator(_SETTINGS_VIEW).count():
                await self._close_settings()
        actual = await self.snapshot_settings()
        for key, expected in settings.items():
            if actual.get(key) != expected:
                raise FlowPresubmitError(
                    "FLOW_SETTING_VALUE_MISMATCH", "Agent setting did not match live readback"
                )
        return actual

    async def restore_settings(self, settings: Mapping[str, object]) -> None:
        actual = await self.apply_settings(settings)
        for key, expected in settings.items():
            if actual.get(key) != expected:
                raise FlowPresubmitError(
                    "FLOW_SETTING_RESTORE_MISMATCH", "Agent defaults did not match live readback"
                )

    async def bind_references(
        self, references: Sequence[ProviderReferenceBinding]
    ) -> tuple[ProviderReferenceBinding, ...]:
        if references:
            raise FlowPresubmitError(
                "FLOW_AGENT_REFERENCES_UNSUPPORTED", "Agent-only F5 supports text-to-video only"
            )
        return ()

    async def fill_prompt(self, prompt: str) -> None:
        if not prompt:
            raise FlowPresubmitError("FLOW_PROMPT_EMPTY", "compiled prompt must not be empty")
        page = await self._ensure_page()
        textbox = page.locator(_PROMPT_SELECTOR)
        await textbox.wait_for(state="visible", timeout=10_000)
        # fill() updates the non-submitting editor. No keyboard Enter or submit
        # control is exposed from this module.
        await textbox.fill(prompt)

    async def supports_negative_prompt(self) -> bool:
        page = await self._ensure_page()
        self._negative_prompt_supported = bool(
            await page.locator(
                '[role="textbox"][aria-label*="negative" i], textarea[aria-label*="negative" i]'
            ).count()
        )
        return self._negative_prompt_supported

    async def fill_negative_prompt(self, prompt: str) -> None:
        if not await self.supports_negative_prompt():
            raise FlowPresubmitError(
                "FLOW_NEGATIVE_PROMPT_UNSUPPORTED",
                "Agent-only composer has no negative prompt field",
            )
        raise FlowPresubmitError(
            "FLOW_NEGATIVE_PROMPT_UNSUPPORTED", "negative prompt interaction is not mapped"
        )

    async def readback(self) -> FlowReadback:
        page = await self._ensure_page()
        settings = await self.snapshot_settings()
        surface = await self.inspect_surface()
        prompt_field = page.locator(_PROMPT_SELECTOR)
        prompt = await _read_prompt_field(prompt_field) if await prompt_field.count() else None
        negative_supported = await self.supports_negative_prompt()
        negative = None
        if negative_supported:
            negative = await page.locator(
                '[role="textbox"][aria-label*="negative" i], textarea[aria-label*="negative" i]'
            ).first.input_value()
        generate = page.locator(_GENERATE_BUTTON)
        present = bool(await generate.count())
        enabled = bool(present and await generate.is_enabled())
        project = urlparse(page.url)
        match = re.fullmatch(r"/project/([A-Za-z0-9_-]+)", project.path)
        return FlowReadback(
            "FLOW_SESSION_READY" if self._session_verified else "FLOW_AUTH_NOT_READY",
            self.provider_account_id if self._session_verified else None,
            match.group(1) if match else None,
            surface,
            settings,
            (),
            prompt,
            negative,
            negative_supported,
            present,
            enabled,
        )

    async def _ensure_page(self) -> Page:
        if self._page is not None:
            return self._page
        if not self._session_verified:
            raise FlowPresubmitError("FLOW_AUTH_NOT_READY", "profile session was not verified")
        channel = channel_for_profile(self.profile_dir)
        ensure_profile_engine_compatible(self.profile_dir, channel)
        from gflow_cli.auth.strategies import async_playwright

        self._playwright = await async_playwright().start()
        try:
            launch_kwargs = login_launch_kwargs(self.profile_dir, self.headless, channel=channel)
            if self.block_service_workers:
                launch_kwargs["service_workers"] = "block"
            self._context = await self._playwright.chromium.launch_persistent_context(
                **launch_kwargs
            )
            self._page = (
                self._context.pages[0] if self._context.pages else await self._context.new_page()
            )
            return self._page
        except Exception:
            await self.close()
            raise

    async def _open_settings(self) -> None:
        page = await self._ensure_page()
        view = page.locator(_SETTINGS_VIEW)
        if await view.count() and await view.is_visible():
            return
        trigger = page.locator(_SETTINGS_BUTTON).first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click(timeout=5_000)
        await view.wait_for(state="visible", timeout=10_000)

    async def _close_settings(self) -> None:
        page = await self._ensure_page()
        view = page.locator(_SETTINGS_VIEW)
        if not await view.count() or not await view.is_visible():
            return
        back = page.locator(_BACK_BUTTON).first
        await back.wait_for(state="visible", timeout=5_000)
        await back.click(timeout=5_000)
        await view.wait_for(state="hidden", timeout=10_000)

    async def _read_video_settings(self) -> dict[str, object]:
        page = await self._ensure_page()
        view = page.locator(_SETTINGS_VIEW)
        await view.wait_for(state="visible", timeout=10_000)
        model_button = view.locator(_VIDEO_MODEL_BUTTON)
        model_text = (await model_button.inner_text()).strip().splitlines()[0]
        model_id = resolve_model_id_from_display(model_text, self.model_display_names)
        if model_id is None:
            raise FlowPresubmitError(
                "FLOW_MODEL_UNMAPPED", "live Agent model is absent from validated model mapping"
            )
        groups = view.locator("mat-button-toggle-group")
        if await groups.count() != 4:
            raise FlowPresubmitError(
                "FLOW_AGENT_SETTINGS_SHAPE_CHANGED", "Agent video setting groups changed"
            )
        aspect = await self._selected_radio_text(groups.nth(2))
        count_text = await self._selected_radio_text(groups.nth(3))
        aspect_match = re.search(r"(?:16:9|9:16)", aspect)
        count_match = re.search(r"x([1-4])", count_text)
        if not aspect_match or not count_match:
            raise FlowPresubmitError(
                "FLOW_AGENT_SETTINGS_UNREADABLE", "video aspect or count readback is unavailable"
            )
        confirmation = view.locator('input[type="radio"]:checked')
        if await confirmation.count() != 1:
            raise FlowPresubmitError(
                "FLOW_AGENT_SETTINGS_UNREADABLE", "confirmation preference readback is unavailable"
            )
        confirmation_value = await confirmation.get_attribute("value")
        confirm_value = {"1": "Always", "2": "Never"}.get(confirmation_value or "")
        if confirm_value is None:
            raise FlowPresubmitError(
                "FLOW_AGENT_SETTINGS_UNREADABLE", "confirmation preference is not recognized"
            )
        return {
            "model_id": model_id,
            "generation_mode": "TEXT_TO_VIDEO",
            "aspect_ratio": aspect_match.group(0),
            "output_count": int(count_match.group(1)),
            "confirm_before_generating": confirm_value,
        }

    @staticmethod
    async def _selected_radio_text(group: Locator) -> str:
        selected = group.locator('[role="radio"][aria-checked="true"]')
        if await selected.count() != 1:
            raise FlowPresubmitError(
                "FLOW_AGENT_SETTINGS_UNREADABLE", "video radio selection is not unique"
            )
        return await selected.inner_text()

    async def _select_video_radio(self, group_index: int, label: str) -> None:
        page = await self._ensure_page()
        group = page.locator(f"{_SETTINGS_VIEW} mat-button-toggle-group").nth(group_index)
        option = group.get_by_role("radio", name=re.compile(rf"\b{re.escape(label)}\b"))
        await option.click(timeout=5_000)

    async def _select_confirmation(self, value: str) -> None:
        if value not in {"Always", "Never"}:
            raise FlowPresubmitError(
                "FLOW_SETTING_VALUE_INVALID", "confirmation preference is unrecognized"
            )
        page = await self._ensure_page()
        radio_value = "1" if value == "Always" else "2"
        await page.locator(f'{_SETTINGS_VIEW} input[type="radio"][value="{radio_value}"]').click(
            timeout=5_000
        )

    async def _select_model(self, model_id: str) -> None:
        display = self.model_display_names.get(model_id)
        if not display:
            raise FlowPresubmitError("FLOW_MODEL_UNMAPPED", "requested model is not validated")
        page = await self._ensure_page()
        await page.locator(f"{_SETTINGS_VIEW} {_VIDEO_MODEL_BUTTON}").click(timeout=5_000)
        choice = page.get_by_text(display, exact=True).last
        await choice.wait_for(state="visible", timeout=5_000)
        await choice.click(timeout=5_000)
