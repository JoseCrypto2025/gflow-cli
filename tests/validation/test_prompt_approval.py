from __future__ import annotations

import pytest

from gflow_cli.v2_runtime.phase_e import PreflightResult
from gflow_cli.v2_runtime.prompt_approval import (
    PromptApprovalIntegrityError,
    build_reapproval_candidate,
    guard_scene_prompt_version_update,
    require_single_current_approval,
)


def _approved_row() -> dict[str, object]:
    return {
        "Scene_Prompt_ID": "SPV_000001",
        "Scene_ID": "SCN_000001",
        "Prompt_Version": 1,
        "Generation_Profile_ID": "GPR_0001",
        "Compiled_Prompt_Text": "approved prompt",
        "Compiled_Negative_Prompt": "",
        "Compiler_Version": "phase-e/1",
        "Source_Context_SHA256": "a" * 64,
        "Prompt_SHA256": "b" * 64,
        "Prompt_Status": "APPROVED",
        "Validation_Message": "PREFLIGHT_PASS",
        "Approved_At": "2026-10-17T14:02:29Z",
        "Created_At": "2026-10-17T14:00:28Z",
        "Updated_At": "2026-10-17T14:08:35Z",
    }


def _fresh_result(**changes: object) -> PreflightResult:
    values: dict[str, object] = {
        "status": "PREFLIGHT_PASS",
        "ready_for_provider_adapter": True,
        "scene_id": "SCN_000001",
        "generation_profile_id": "GPR_0001",
        "compiled_prompt": "new prompt",
        "compiled_negative_prompt": "",
        "source_context_sha256": "c" * 64,
        "prompt_sha256": "d" * 64,
        "errors": (),
    }
    values.update(changes)
    return PreflightResult(**values)  # type: ignore[arg-type]


def test_identical_approved_rewrite_is_idempotent() -> None:
    row = _approved_row()
    guard_scene_prompt_version_update(row, dict(row))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Compiled_Prompt_Text", "mutated prompt"),
        ("Compiled_Negative_Prompt", "mutated negative"),
        ("Source_Context_SHA256", "c" * 64),
        ("Prompt_SHA256", "d" * 64),
        ("Generation_Profile_ID", "GPR_9999"),
        ("Approved_At", "2026-10-18T00:00:00Z"),
        ("Scene_ID", "SCN_000002"),
        ("Prompt_Version", 2),
        ("Compiler_Version", "phase-e/2"),
        ("Created_At", "2026-10-18T00:00:00Z"),
    ],
)
def test_approved_semantic_mutation_is_rejected(field: str, value: object) -> None:
    before = _approved_row()
    after = {**before, field: value}
    with pytest.raises(PromptApprovalIntegrityError) as exc:
        guard_scene_prompt_version_update(before, after)
    assert exc.value.code == "APPROVED_PROMPT_IMMUTABLE"
    assert before[field] != after[field]


def test_approved_to_superseded_allows_only_status_and_update_timestamp() -> None:
    before = _approved_row()
    after = {**before, "Prompt_Status": "SUPERSEDED", "Updated_At": "2026-10-17T15:00:00Z"}
    guard_scene_prompt_version_update(before, after)


def test_approved_supersede_cannot_change_validation_evidence() -> None:
    before = _approved_row()
    after = {
        **before,
        "Prompt_Status": "SUPERSEDED",
        "Updated_At": "2026-10-17T15:00:00Z",
        "Validation_Message": "rewritten",
    }
    with pytest.raises(PromptApprovalIntegrityError):
        guard_scene_prompt_version_update(before, after)


def test_changed_phase_e_builds_new_validated_version() -> None:
    old = _approved_row()
    result = _fresh_result()
    candidate = build_reapproval_candidate(
        old,
        scene_prompt_id="SPV_000002",
        fresh_phase_e=result,
        compiler_version="phase-e/1",
        created_at="2026-10-17T15:00:00Z",
    )
    assert candidate["Scene_Prompt_ID"] == "SPV_000002"
    assert candidate["Prompt_Version"] == 2
    assert candidate["Prompt_Status"] == "VALIDATED"
    assert old["Prompt_Status"] == "APPROVED"
    assert old["Source_Context_SHA256"] == "a" * 64
    assert candidate["Source_Context_SHA256"] == "c" * 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("compiled_prompt", "tampered"),
        ("compiled_negative_prompt", "tampered"),
        ("source_context_sha256", "e" * 64),
        ("prompt_sha256", "f" * 64),
        ("scene_id", "SCN_000002"),
        ("generation_profile_id", "GPR_0002"),
        ("status", "PREFLIGHT_FAIL"),
        ("ready_for_provider_adapter", False),
        ("errors", (object(),)),
    ],
)
def test_reapproval_candidate_requires_exact_fresh_phase_e(field: str, value: object) -> None:
    before = {
        **_approved_row(),
        "Scene_Prompt_ID": "SPV_000002",
        "Prompt_Version": 2,
        "Compiled_Prompt_Text": "new prompt",
        "Source_Context_SHA256": "c" * 64,
        "Prompt_SHA256": "d" * 64,
        "Prompt_Status": "VALIDATED",
        "Approved_At": "",
    }
    after = {
        **before,
        "Prompt_Status": "APPROVED",
        "Approved_At": "2026-10-17T15:00:00Z",
        field: value,
    }
    with pytest.raises(PromptApprovalIntegrityError):
        guard_scene_prompt_version_update(before, after, fresh_phase_e=_fresh_result())


def test_validated_row_cannot_be_approved_without_fresh_phase_e() -> None:
    row = _approved_row()
    before = {**row, "Prompt_Status": "VALIDATED", "Approved_At": ""}
    after = {**before, "Prompt_Status": "APPROVED", "Approved_At": "2026-10-17T15:00:00Z"}
    with pytest.raises(PromptApprovalIntegrityError):
        guard_scene_prompt_version_update(before, after)


def test_validated_row_approval_requires_exact_fresh_phase_e() -> None:
    before = {
        **_approved_row(),
        "Scene_Prompt_ID": "SPV_000002",
        "Prompt_Version": 2,
        "Compiled_Prompt_Text": "new prompt",
        "Source_Context_SHA256": "c" * 64,
        "Prompt_SHA256": "d" * 64,
        "Prompt_Status": "VALIDATED",
        "Approved_At": "",
    }
    after = {**before, "Prompt_Status": "APPROVED", "Approved_At": "2026-10-17T15:00:00Z"}
    guard_scene_prompt_version_update(before, after, fresh_phase_e=_fresh_result())
    stale = _fresh_result(source_context_sha256="e" * 64)
    with pytest.raises(PromptApprovalIntegrityError):
        guard_scene_prompt_version_update(before, after, fresh_phase_e=stale)


def test_row_schema_is_required_for_guard() -> None:
    with pytest.raises(PromptApprovalIntegrityError):
        guard_scene_prompt_version_update(None, {"Prompt_Status": "APPROVED"})


def test_one_current_approved_version_is_required() -> None:
    old = {**_approved_row(), "Prompt_Status": "SUPERSEDED"}
    current = {
        **_approved_row(),
        "Scene_Prompt_ID": "SPV_000002",
        "Prompt_Version": 2,
    }
    require_single_current_approval(
        [old, current], scene_id="SCN_000001", generation_profile_id="GPR_0001"
    )
    with pytest.raises(PromptApprovalIntegrityError):
        require_single_current_approval(
            [old, current, _approved_row()],
            scene_id="SCN_000001",
            generation_profile_id="GPR_0001",
        )
