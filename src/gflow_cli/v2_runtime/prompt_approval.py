"""Immutable lifecycle checks for persisted SCENE_PROMPT_VERSION rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .phase_e import PreflightResult

_APPROVED_STATUS = "APPROVED"
_SUPERSEDED_STATUS = "SUPERSEDED"
_VALIDATED_STATUS = "VALIDATED"
_APPROVAL_FIELDS = (
    "Scene_Prompt_ID",
    "Scene_ID",
    "Prompt_Version",
    "Generation_Profile_ID",
    "Compiled_Prompt_Text",
    "Compiled_Negative_Prompt",
    "Compiler_Version",
    "Source_Context_SHA256",
    "Prompt_SHA256",
    "Approved_At",
)
_ROW_FIELDS = frozenset(
    {
        *_APPROVAL_FIELDS,
        "Prompt_Status",
        "Validation_Message",
        "Created_At",
        "Updated_At",
    }
)


class PromptApprovalIntegrityError(ValueError):
    """Stable failure raised when a persisted prompt approval transition is unsafe."""

    code = "APPROVED_PROMPT_IMMUTABLE"


def guard_scene_prompt_version_update(
    previous: Mapping[str, object] | None,
    proposed: Mapping[str, object],
    *,
    fresh_phase_e: PreflightResult | None = None,
) -> None:
    """Validate one row transition before any Sheets or storage write.

    An APPROVED row accepts only an exact idempotent rewrite or a status-only
    transition to SUPERSEDED with its update timestamp. A VALIDATED row can be
    approved only against an exact, successful fresh Phase E result.
    """
    if frozenset(proposed) != _ROW_FIELDS:
        raise PromptApprovalIntegrityError("prompt row does not match the known schema")

    next_status = proposed.get("Prompt_Status")
    prior_status = previous.get("Prompt_Status") if previous is not None else None

    if prior_status == _APPROVED_STATUS:
        if frozenset(previous or {}) != _ROW_FIELDS:
            raise PromptApprovalIntegrityError("existing approved prompt row is incomplete")
        assert previous is not None
        if dict(previous) == dict(proposed):
            return
        if next_status == _SUPERSEDED_STATUS and all(
            previous[field] == proposed[field]
            for field in _ROW_FIELDS - {"Prompt_Status", "Updated_At"}
        ):
            return
        raise PromptApprovalIntegrityError("approved prompt approval contract is immutable")

    if next_status == _APPROVED_STATUS:
        if prior_status != _VALIDATED_STATUS or fresh_phase_e is None:
            raise PromptApprovalIntegrityError(
                "approval requires a VALIDATED row and fresh Phase E"
            )
        _require_exact_fresh_phase_e(proposed, fresh_phase_e)


def _require_exact_fresh_phase_e(row: Mapping[str, object], result: PreflightResult) -> None:
    """Fail closed unless every approved prompt value matches a fresh PASS."""
    if result.status != "PREFLIGHT_PASS" or not result.ready_for_provider_adapter or result.errors:
        raise PromptApprovalIntegrityError("fresh Phase E did not pass without errors")
    expected = {
        "Scene_ID": result.scene_id,
        "Generation_Profile_ID": result.generation_profile_id,
        "Compiled_Prompt_Text": result.compiled_prompt,
        "Compiled_Negative_Prompt": result.compiled_negative_prompt or "",
        "Source_Context_SHA256": result.source_context_sha256,
        "Prompt_SHA256": result.prompt_sha256,
    }
    if any(row.get(field) != value for field, value in expected.items()):
        raise PromptApprovalIntegrityError("candidate differs from fresh Phase E")


def build_reapproval_candidate(
    previous_approved: Mapping[str, object],
    *,
    scene_prompt_id: str,
    fresh_phase_e: PreflightResult,
    compiler_version: str,
    created_at: str,
) -> dict[str, object]:
    """Build a distinct VALIDATED version without mutating an approved row."""
    if previous_approved.get("Prompt_Status") != _APPROVED_STATUS:
        raise PromptApprovalIntegrityError("reapproval source must be APPROVED")
    _require_exact_fresh_phase_e(
        {
            "Scene_ID": fresh_phase_e.scene_id,
            "Generation_Profile_ID": fresh_phase_e.generation_profile_id,
            "Compiled_Prompt_Text": fresh_phase_e.compiled_prompt,
            "Compiled_Negative_Prompt": fresh_phase_e.compiled_negative_prompt or "",
            "Source_Context_SHA256": fresh_phase_e.source_context_sha256,
            "Prompt_SHA256": fresh_phase_e.prompt_sha256,
        },
        fresh_phase_e,
    )
    if fresh_phase_e.scene_id != previous_approved.get(
        "Scene_ID"
    ) or fresh_phase_e.generation_profile_id != previous_approved.get("Generation_Profile_ID"):
        raise PromptApprovalIntegrityError("fresh Phase E identity differs from approved source")
    if scene_prompt_id == previous_approved.get("Scene_Prompt_ID"):
        raise PromptApprovalIntegrityError("reapproval requires a new prompt ID")
    prompt_version = previous_approved.get("Prompt_Version")
    if isinstance(prompt_version, bool) or not isinstance(prompt_version, int):
        raise PromptApprovalIntegrityError("approved source has no valid prompt version")
    return {
        "Scene_Prompt_ID": scene_prompt_id,
        "Scene_ID": fresh_phase_e.scene_id,
        "Prompt_Version": prompt_version + 1,
        "Generation_Profile_ID": fresh_phase_e.generation_profile_id,
        "Compiled_Prompt_Text": fresh_phase_e.compiled_prompt,
        "Compiled_Negative_Prompt": fresh_phase_e.compiled_negative_prompt or "",
        "Compiler_Version": compiler_version,
        "Source_Context_SHA256": fresh_phase_e.source_context_sha256,
        "Prompt_SHA256": fresh_phase_e.prompt_sha256,
        "Prompt_Status": _VALIDATED_STATUS,
        "Validation_Message": "PREFLIGHT_PASS; fresh Phase E exact-match required before approval",
        "Approved_At": "",
        "Created_At": created_at,
        "Updated_At": created_at,
    }


def require_single_current_approval(
    rows: Sequence[Mapping[str, object]], *, scene_id: str, generation_profile_id: str
) -> None:
    """Require exactly one APPROVED version for a Scene/profile pair."""
    current = [
        row
        for row in rows
        if row.get("Scene_ID") == scene_id
        and row.get("Generation_Profile_ID") == generation_profile_id
        and row.get("Prompt_Status") == _APPROVED_STATUS
    ]
    if len(current) != 1:
        raise PromptApprovalIntegrityError(
            "Scene/profile must have exactly one current approved prompt version"
        )
