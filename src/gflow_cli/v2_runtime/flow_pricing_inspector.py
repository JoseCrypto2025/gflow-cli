"""One-shot, read-only inspection of the pinned VPS Flow pricing surface."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from playwright.async_api import Page

from gflow_cli import auth
from gflow_cli.profile_lease import ProfileLease

from .errors import FlowPresubmitError
from .google_flow import FlowProjectState
from .live_flow import GoogleFlowAgentOnlyPort

EXPECTED_PROVIDER_ACCOUNT_ID = "PVA_0002"
EXPECTED_PROFILE_NAME = "gflow-prod"
EXPECTED_PROFILE_PATH = Path("/var/lib/gflow/browser-state/profile_gflow-prod")
EXPECTED_EMAIL = "singleava508@gmail.com"
EXPECTED_PROJECT_ID = "aafe9282-071f-4333-baa4-92b9f76a9c27"
EXPECTED_PROJECT_NAME = "AI Video Automation V2 - PROD"
EXPECTED_MODEL_ID = "MDL_0001"
MODEL_DISPLAY_NAMES = {EXPECTED_MODEL_ID: "Veo 3.1 Lite"}
EXPECTED_CONFIGURATION = {"duration": 8, "aspect_ratio": "9:16", "output_count": 1}

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_CREDIT_QUOTE_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s+credits?\b", re.IGNORECASE)
_AMBIGUOUS_QUOTE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:or|/|to)\s*\d+(?:\.\d+)?\s+credits?\b", re.I)
_MODEL_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()\[\]-]{0,79}$")
_RESOLUTION_RE = re.compile(r"^(?:\d{3,4}p|[124]K)$", re.I)
_AUDIO_MODES = frozenset({"ON", "OFF", "AUTO", "ENABLED", "DISABLED"})
_QUALITY_TIERS = frozenset({"LITE", "FAST", "QUALITY", "STANDARD", "HIGH", "LOW"})


@dataclass(frozen=True, slots=True)
class FlowPricingObservation:
    """Allowlisted observations collected from one verified project page."""

    account_email: str | None
    project_id: str | None
    locale: str | None
    settings: Mapping[str, object] | None
    generate_control_label: str | None
    observed_at: datetime
    screenshot_evidence: tuple[str, ...] = ()
    software_commit: str | None = None
    error_code: str | None = None


class ReadOnlyFlowPort(Protocol):
    """The deliberately small read-only subset used by this inspector."""

    @property
    def session_email(self) -> str | None: ...

    async def select_project(self, project_id: str) -> FlowProjectState: ...

    async def inspect_surface(self) -> str | None: ...

    async def snapshot_settings(
        self, *, screenshot_path: Path | None = None, allow_unmapped_model: bool = False
    ) -> Mapping[str, object]: ...

    async def active_page(self) -> Page: ...

    async def read_generate_control_label(
        self, *, screenshot_path: Path | None = None
    ) -> str | None: ...


def validate_profile_path(actual: Path | str, expected: Path | str = EXPECTED_PROFILE_PATH) -> Path:
    """Accept only the configured production profile at its canonical VPS path."""
    actual_path = Path(actual).resolve(strict=False)
    expected_path = Path(expected).resolve(strict=False)
    if actual_path != expected_path:
        raise ValueError("PROFILE_MISMATCH")
    return actual_path


def extract_pre_submit_quote(
    generate_control_label: str | None, *, locale: str | None
) -> tuple[str, str, str] | None:
    """Read an unambiguous English credit amount from the visible submit label.

    The label is never returned. No page-wide text scan or price inference is
    used; unfamiliar locales and ambiguous labels remain unavailable.
    """
    if not isinstance(generate_control_label, str) or len(generate_control_label) > 200:
        return None
    if not isinstance(locale, str) or not locale.casefold().startswith("en"):
        return None
    generate_at = re.search(r"\bgenerate\b", generate_control_label, re.IGNORECASE)
    if generate_at is None:
        return None
    if _AMBIGUOUS_QUOTE_RE.search(generate_control_label):
        return None
    matches = list(_CREDIT_QUOTE_RE.finditer(generate_control_label))
    if len(matches) != 1 or matches[0].start() < generate_at.end():
        return None
    return matches[0].group(1), "credits", "Generate control label"


def _safe_text(value: object, *, limit: int = 100) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned[:limit] if cleaned else None


def _safe_model_label(value: object) -> str | None:
    cleaned = _safe_text(value, limit=80)
    return cleaned if cleaned is not None and _MODEL_LABEL_RE.fullmatch(cleaned) else None


def _safe_screenshot_evidence(values: tuple[str, ...]) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    for value in values:
        path = Path(value)
        run_stamp = path.parent.name
        if (
            path.name in {"settings.png", "quote.png"}
            and path.parent.parent.name == "flow-pricing-inspection"
            and re.fullmatch(r"\d{8}T\d{6}_\d{6}Z", run_stamp)
            and path.is_file()
            and not path.is_symlink()
        ):
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            evidence.append({"path": str(path), "sha256": digest})
    return evidence


def _dom_evidence_digest(values: dict[str, object]) -> str:
    payload = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_inspection_report(observation: FlowPricingObservation) -> dict[str, object]:
    """Build a sanitized, allowlisted report without retaining raw UI strings."""
    account_known = isinstance(observation.account_email, str)
    account_match = bool(
        isinstance(observation.account_email, str)
        and observation.account_email.casefold() == EXPECTED_EMAIL.casefold()
    )
    project_known = isinstance(observation.project_id, str)
    project_match = bool(project_known and observation.project_id == EXPECTED_PROJECT_ID)
    report: dict[str, object] = {
        "PROVIDER_ACCOUNT_ID": EXPECTED_PROVIDER_ACCOUNT_ID,
        "BROWSER_IDENTITY": EXPECTED_PROFILE_NAME,
        "BROWSER_PROFILE": str(EXPECTED_PROFILE_PATH),
        "FLOW_ACCOUNT": observation.account_email if account_match else None,
        "ACCOUNT_MATCH": "PASS" if account_match else "FAIL" if account_known else "UNKNOWN",
        "FLOW_PROJECT_ID": observation.project_id if project_match else None,
        "PROJECT_MATCH": "PASS" if project_match else "FAIL" if project_known else "UNKNOWN",
        "ACTIVE_MODEL_NAME": None,
        "ACTIVE_MODEL_VARIANT": "UNKNOWN",
        "ACTIVE_MODEL_PRIORITY": "UNKNOWN",
        "GENERATION_MODE": "UNKNOWN",
        "VISIBLE_DURATION": "UNKNOWN",
        "DURATION_READBACK": "UNAVAILABLE",
        "DURATION_SELECTOR_CONFIDENCE": "NONE",
        "VISIBLE_ASPECT_RATIO": "UNKNOWN",
        "VISIBLE_OUTPUT_COUNT": "UNKNOWN",
        "VISIBLE_RESOLUTION": "UNKNOWN",
        "VISIBLE_AUDIO_MODE": "UNKNOWN",
        "VISIBLE_QUALITY_TIER": "UNKNOWN",
        "CONFIG_MATCH": "UNKNOWN",
        "VISIBLE_CONFIG": {},
        "UNAVAILABLE_CONFIG": [],
        "DISPLAYED_CREDIT_COST": None,
        "DISPLAYED_CREDIT_UNIT": None,
        "CREDIT_COST_CONTEXT": None,
        "CREDIT_COST_AUTHORITATIVE": "NO",
        "ACCOUNT_PLAN_NAME": "UNKNOWN",
        "ACCOUNT_PRICING_CLASS": "UNKNOWN",
        "SCREENSHOT_EVIDENCE": _safe_screenshot_evidence(observation.screenshot_evidence),
        "DOM_EVIDENCE_DIGEST": None,
        "OBSERVED_AT": observation.observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "SOFTWARE_COMMIT": observation.software_commit
        if isinstance(observation.software_commit, str)
        and _COMMIT_RE.fullmatch(observation.software_commit)
        else "UNKNOWN",
        "PROFILE_LOCK_RESPECTED": "PASS",
        "PROVIDER_GENERATE_ACTIONS": 0,
        "NEW_ATTEMPTS_CREATED": 0,
        "NEW_RESERVATIONS_CREATED": 0,
        "APPROVAL_ACTIONS": 0,
        "PROVIDER_CREDITS_CONSUMED": 0,
        "FLOW_STATE_CHANGED": "NO",
        "INSPECTION_STATUS": "HOLD",
        "REASON_CODE": observation.error_code,
    }

    if observation.error_code == "FLOW_ACCOUNT_MISMATCH":
        report["ACCOUNT_MATCH"] = "FAIL"
    if observation.error_code == "FLOW_PROJECT_MISMATCH":
        report["PROJECT_MATCH"] = "FAIL"

    if account_known and not account_match:
        report["REASON_CODE"] = "FLOW_ACCOUNT_MISMATCH"
        return report
    if project_known and not project_match:
        report["REASON_CODE"] = "FLOW_PROJECT_MISMATCH"
        return report
    if observation.error_code:
        report["REASON_CODE"] = observation.error_code
        return report
    if not account_match:
        report["REASON_CODE"] = "FLOW_ACCOUNT_UNVERIFIED"
        return report
    if not project_match:
        report["REASON_CODE"] = "FLOW_PROJECT_UNVERIFIED"
        return report

    settings = observation.settings or {}
    model_id = settings.get("model_id")
    model_name = _safe_model_label(settings.get("model_name"))
    if model_name is None and isinstance(model_id, str):
        model_name = MODEL_DISPLAY_NAMES.get(model_id)
    report["ACTIVE_MODEL_NAME"] = model_name or "UNKNOWN"
    visible_config: dict[str, object] = {}
    model_variant = _safe_model_label(settings.get("model_variant"))
    if model_variant is not None:
        report["ACTIVE_MODEL_VARIANT"] = model_variant
    model_priority = settings.get("model_priority")
    if model_priority in {"STANDARD", "LOWER_PRIORITY"}:
        report["ACTIVE_MODEL_PRIORITY"] = model_priority
        visible_config["model_priority"] = model_priority
    report["GENERATION_MODE"] = _safe_text(settings.get("generation_mode")) or "UNKNOWN"

    duration = settings.get("duration")
    duration_confidence = settings.get("duration_selector_confidence")
    aspect_ratio = settings.get("aspect_ratio")
    output_count = settings.get("output_count")
    if (
        isinstance(duration, int)
        and not isinstance(duration, bool)
        and duration_confidence == "AUTHORITATIVE"
    ):
        report["VISIBLE_DURATION"] = duration
        report["DURATION_READBACK"] = duration
        report["DURATION_SELECTOR_CONFIDENCE"] = "AUTHORITATIVE"
        visible_config["duration"] = duration
    if aspect_ratio in {"9:16", "16:9"}:
        report["VISIBLE_ASPECT_RATIO"] = aspect_ratio
        visible_config["aspect_ratio"] = aspect_ratio
    if isinstance(output_count, int) and not isinstance(output_count, bool):
        report["VISIBLE_OUTPUT_COUNT"] = output_count
        visible_config["output_count"] = output_count
    for key, output_key in (
        ("resolution", "VISIBLE_RESOLUTION"),
        ("audio_mode", "VISIBLE_AUDIO_MODE"),
        ("quality_tier", "VISIBLE_QUALITY_TIER"),
    ):
        raw_value = _safe_text(settings.get(key))
        value = raw_value
        if key == "resolution" and (raw_value is None or not _RESOLUTION_RE.fullmatch(raw_value)):
            value = None
        elif key == "audio_mode" and (raw_value is None or raw_value.upper() not in _AUDIO_MODES):
            value = None
        elif key == "quality_tier" and (
            raw_value is None or raw_value.upper() not in _QUALITY_TIERS
        ):
            value = None
        elif key in {"audio_mode", "quality_tier"} and raw_value is not None:
            value = raw_value.upper()
        if value is not None:
            report[output_key] = value
            visible_config[key] = value
    report["VISIBLE_CONFIG"] = visible_config
    unavailable_config = set(EXPECTED_CONFIGURATION) - visible_config.keys()
    for key in ("resolution", "audio_mode", "quality_tier"):
        if key not in visible_config:
            unavailable_config.add(key)
    if report["ACTIVE_MODEL_PRIORITY"] == "UNKNOWN":
        unavailable_config.add("model_priority")
    if report["ACTIVE_MODEL_VARIANT"] == "UNKNOWN":
        unavailable_config.add("model_variant")
    if report["GENERATION_MODE"] == "UNKNOWN":
        unavailable_config.add("generation_mode")
    report["UNAVAILABLE_CONFIG"] = sorted(unavailable_config)
    missing_expected = set(EXPECTED_CONFIGURATION) - visible_config.keys()
    mismatched_expected = {
        key
        for key, expected in EXPECTED_CONFIGURATION.items()
        if key in visible_config and visible_config[key] != expected
    }
    if mismatched_expected:
        report["CONFIG_MATCH"] = "MISMATCH"
    elif missing_expected:
        report["CONFIG_MATCH"] = "UNKNOWN"
    else:
        report["CONFIG_MATCH"] = "PASS"

    if model_id is None and model_name is None:
        report["REASON_CODE"] = "FLOW_MODEL_UNKNOWN"
    elif model_id != EXPECTED_MODEL_ID or report["ACTIVE_MODEL_PRIORITY"] == "LOWER_PRIORITY":
        report["REASON_CODE"] = "FLOW_MODEL_CONFIG_MISMATCH"
    elif mismatched_expected:
        report["REASON_CODE"] = "FLOW_MODEL_CONFIG_MISMATCH"
    elif missing_expected:
        report["REASON_CODE"] = "FLOW_CONFIG_UNAVAILABLE"
    elif report["ACTIVE_MODEL_PRIORITY"] == "UNKNOWN":
        report["REASON_CODE"] = "FLOW_MODEL_PRIORITY_UNAVAILABLE"

    quote = extract_pre_submit_quote(observation.generate_control_label, locale=observation.locale)
    if quote is not None:
        (
            report["DISPLAYED_CREDIT_COST"],
            report["DISPLAYED_CREDIT_UNIT"],
            report["CREDIT_COST_CONTEXT"],
        ) = quote
        if (
            report["CONFIG_MATCH"] == "PASS"
            and model_id == EXPECTED_MODEL_ID
            and report["ACTIVE_MODEL_PRIORITY"] == "STANDARD"
        ):
            report["CREDIT_COST_AUTHORITATIVE"] = "YES"
    elif report["REASON_CODE"] is None:
        report["REASON_CODE"] = "PRE_SUBMIT_CREDIT_COST_NOT_VISIBLE"

    digest_values = {
        "account_match": account_match,
        "project_id": EXPECTED_PROJECT_ID,
        "model_id": model_id if model_id == EXPECTED_MODEL_ID else None,
        "model_name": model_name,
        "model_priority": report["ACTIVE_MODEL_PRIORITY"],
        "generation_mode": report["GENERATION_MODE"],
        "visible_config": visible_config,
        "locale": observation.locale,
        "displayed_credit_cost": report["DISPLAYED_CREDIT_COST"],
        "displayed_credit_unit": report["DISPLAYED_CREDIT_UNIT"],
        "observed_at": report["OBSERVED_AT"],
    }
    report["DOM_EVIDENCE_DIGEST"] = _dom_evidence_digest(digest_values)
    if report["REASON_CODE"] not in {
        "FLOW_MODEL_CONFIG_MISMATCH",
        "FLOW_MODEL_UNKNOWN",
        "FLOW_CONFIG_UNAVAILABLE",
        "FLOW_MODEL_PRIORITY_UNAVAILABLE",
        "FLOW_UI_SURFACE_UNSUPPORTED",
        "FLOW_UI_LOCALE_UNSUPPORTED",
        "PRE_SUBMIT_CREDIT_COST_NOT_VISIBLE",
    }:
        report["INSPECTION_STATUS"] = "PASS"
    return report


async def inspect_with_port(
    port: ReadOnlyFlowPort,
    *,
    expected_email: str = EXPECTED_EMAIL,
    expected_project_id: str = EXPECTED_PROJECT_ID,
    screenshot_paths: tuple[Path, ...] | None,
    screenshot_root: Path | None = None,
    software_commit: str | None,
) -> dict[str, object]:
    """Read the current pinned project through only the narrow port protocol."""
    observed_at = datetime.now(UTC)
    if (
        not isinstance(port.session_email, str)
        or port.session_email.casefold() != expected_email.casefold()
    ):
        return build_inspection_report(
            FlowPricingObservation(
                port.session_email,
                None,
                None,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code="FLOW_ACCOUNT_MISMATCH",
            )
        )

    project = await port.select_project(expected_project_id)
    project_id = getattr(project, "project_id", None)
    if project_id != expected_project_id:
        return build_inspection_report(
            FlowPricingObservation(
                expected_email,
                project_id if isinstance(project_id, str) else None,
                None,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code="FLOW_PROJECT_MISMATCH",
            )
        )

    surface = await port.inspect_surface()
    if surface != "AGENT_ONLY":
        return build_inspection_report(
            FlowPricingObservation(
                expected_email,
                expected_project_id,
                None,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code="FLOW_UI_SURFACE_UNSUPPORTED",
            )
        )

    page = await port.active_page()
    locale_value = await page.evaluate("document.documentElement.lang || navigator.language")
    locale = _safe_text(locale_value, limit=20)
    if locale is None or not locale.casefold().startswith("en"):
        return build_inspection_report(
            FlowPricingObservation(
                expected_email,
                expected_project_id,
                locale,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code="FLOW_UI_LOCALE_UNSUPPORTED",
            )
        )

    if screenshot_paths is None and screenshot_root is not None:
        try:
            screenshot_paths = _create_evidence_paths(screenshot_root, observed_at)
        except (OSError, ValueError):
            screenshot_paths = None
    settings_path = screenshot_paths[0] if screenshot_paths else None
    settings = await port.snapshot_settings(
        screenshot_path=settings_path,
        allow_unmapped_model=True,
    )
    generate_control_label = await port.read_generate_control_label()
    screenshot_evidence: list[str] = []
    if settings_path is not None and settings_path.is_file() and not settings_path.is_symlink():
        if _restrict_evidence_file(settings_path):
            screenshot_evidence.append(str(settings_path))
    quote_path = (
        screenshot_paths[1] if screenshot_paths is not None and len(screenshot_paths) > 1 else None
    )
    if (
        extract_pre_submit_quote(generate_control_label, locale=locale) is not None
        and quote_path is not None
    ):
        await port.read_generate_control_label(screenshot_path=quote_path)
        if (
            quote_path.is_file()
            and not quote_path.is_symlink()
            and _restrict_evidence_file(quote_path)
        ):
            screenshot_evidence.append(str(quote_path))

    return build_inspection_report(
        FlowPricingObservation(
            expected_email,
            expected_project_id,
            locale,
            dict(settings),
            generate_control_label,
            observed_at,
            tuple(screenshot_evidence),
            software_commit,
        )
    )


def _restrict_evidence_file(path: Path) -> bool:
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return stat.S_IMODE(path.stat().st_mode) == 0o600
    except OSError:
        return False


def _create_evidence_paths(root: Path, observed_at: datetime) -> tuple[Path, ...]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("EVIDENCE_ROOT_UNSAFE")
    directory = root / "flow-pricing-inspection"
    if directory.is_symlink():
        raise ValueError("EVIDENCE_PATH_UNSAFE")
    directory.mkdir(mode=0o700, parents=False, exist_ok=True)
    if directory.stat().st_mode & 0o077:
        raise ValueError("EVIDENCE_PERMISSIONS_UNSAFE")
    stamp = observed_at.strftime("%Y%m%dT%H%M%S_%fZ")
    run_directory = directory / stamp
    run_directory.mkdir(mode=0o700, parents=False, exist_ok=False)
    if stat.S_IMODE(run_directory.stat().st_mode) != 0o700:
        raise ValueError("EVIDENCE_PERMISSIONS_UNSAFE")
    return run_directory / "settings.png", run_directory / "quote.png"


def _safe_error_code(value: str) -> str:
    return value if re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value) else "FLOW_INSPECTION_FAILED"


def _runtime_error_code(exc: Exception) -> str:
    """Map only recognizable native Chromium profile contention to a safe code."""
    message = str(exc).casefold()
    if any(
        marker in message
        for marker in (
            "processsingleton",
            "singletonlock",
            "user data directory is already in use",
            "profile is already in use",
        )
    ):
        return "PROFILE_IN_USE"
    return "FLOW_INSPECTION_FAILED"


async def inspect_flow_pricing_read_only(
    *,
    provider_account_id: str,
    expected_project_id: str,
    expected_email: str,
    runtime_state_root: Path,
    software_commit: str | None,
) -> dict[str, object]:
    """Inspect only the fixed PROD profile/project; never submit or edit Flow."""
    observed_at = datetime.now(UTC)
    if (
        provider_account_id != EXPECTED_PROVIDER_ACCOUNT_ID
        or expected_project_id != EXPECTED_PROJECT_ID
    ):
        return build_inspection_report(
            FlowPricingObservation(
                None,
                None,
                None,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code="INSPECTION_TARGET_UNSUPPORTED",
            )
        )
    if expected_email.casefold() != EXPECTED_EMAIL.casefold() or not _EMAIL_RE.fullmatch(
        expected_email
    ):
        return build_inspection_report(
            FlowPricingObservation(
                None,
                None,
                None,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code="INSPECTION_TARGET_UNSUPPORTED",
            )
        )

    try:
        configured_profile = auth.profile_dir(EXPECTED_PROFILE_NAME)
        if configured_profile.is_symlink():
            raise ValueError("PROFILE_PATH_UNSAFE")
        profile_dir = validate_profile_path(configured_profile)
    except (OSError, ValueError):
        return build_inspection_report(
            FlowPricingObservation(
                None,
                None,
                None,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code="PROFILE_MISMATCH",
            )
        )
    if profile_dir.is_symlink() or not profile_dir.is_dir():
        return build_inspection_report(
            FlowPricingObservation(
                None,
                None,
                None,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code="PROFILE_UNAVAILABLE",
            )
        )

    lease = ProfileLease(profile_dir)
    try:
        acquired = lease.try_acquire()
    except Exception:
        return build_inspection_report(
            FlowPricingObservation(
                None,
                None,
                None,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code="PROFILE_LOCK_FAILED",
            )
        )
    if not acquired:
        return build_inspection_report(
            FlowPricingObservation(
                None,
                None,
                None,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code="PROFILE_IN_USE",
            )
        )

    port: GoogleFlowAgentOnlyPort | None = None
    try:
        # The outer lease spans auth verification, exact-project navigation,
        # DOM reads, and browser teardown. Pass it through verification so a
        # cookie-decryption fallback borrows it instead of nesting a lease.
        port = await GoogleFlowAgentOnlyPort.open(
            auth_profile_key="GFLOW_PROD_AUTH",
            provider_account_id=provider_account_id,
            provider_project_id=expected_project_id,
            project_name=EXPECTED_PROJECT_NAME,
            model_display_names=MODEL_DISPLAY_NAMES,
            expected_email=expected_email,
            headless=False,
            profile_lease=lease,
        )
        return await inspect_with_port(
            port,
            expected_email=expected_email,
            expected_project_id=expected_project_id,
            screenshot_paths=None,
            screenshot_root=runtime_state_root,
            software_commit=software_commit,
        )
    except FlowPresubmitError as exc:
        return build_inspection_report(
            FlowPricingObservation(
                None,
                None,
                None,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code=_safe_error_code(exc.code),
            )
        )
    except Exception as exc:
        return build_inspection_report(
            FlowPricingObservation(
                None,
                None,
                None,
                None,
                None,
                observed_at,
                software_commit=software_commit,
                error_code=_runtime_error_code(exc),
            )
        )
    finally:
        try:
            if port is not None:
                await port.close()
        finally:
            lease.release()
