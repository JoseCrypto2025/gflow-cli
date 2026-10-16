"""Compatibility contract for translating legacy requests; no server/listener is started."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import CatalogResolutionError


@dataclass(frozen=True, slots=True)
class LegacyVideoRequest:
    prompt: str
    duration: int
    aspect_ratio: str = ""
    profile_id: str | None = None
    image_url: str | None = None
    zoom: str | None = None

    @property
    def duration_sec(self) -> int:
        return self.duration


@dataclass(frozen=True, slots=True)
class LegacyCompatibilityPlan:
    accepted: bool
    disposition: str
    requested_duration_sec: int
    max_supported_scene_duration_sec: int | None
    message: str


def plan_legacy_request(
    request: LegacyVideoRequest, supported_durations: set[int]
) -> LegacyCompatibilityPlan:
    """Preserve request intent, but never split or execute an overlong request silently."""
    if request.duration_sec <= 0 or not request.prompt.strip():
        raise CatalogResolutionError("legacy request needs a prompt and positive duration")
    if not supported_durations:
        raise CatalogResolutionError("no verified duration capabilities were supplied")
    max_duration = max(supported_durations)
    if request.duration_sec not in supported_durations:
        return LegacyCompatibilityPlan(
            False,
            "requires_scene_plan",
            request.duration_sec,
            max_duration,
            "duration is unsupported; create explicit scenes before any generation request",
        )
    return LegacyCompatibilityPlan(
        True,
        "profile_resolution_required",
        request.duration_sec,
        max_duration,
        "resolve profile and capability data before dispatch",
    )
