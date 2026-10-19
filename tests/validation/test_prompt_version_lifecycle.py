from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gflow_cli.v2_runtime.phase_e import PreflightResult
from gflow_cli.v2_runtime.prompt_approval import (
    PromptApprovalIntegrityError,
    guard_scene_prompt_version_update,
)
from gflow_cli.v2_runtime.prompt_version_lifecycle import (
    PromptVersionLifecycle,
    PromptVersionLifecycleError,
)


def _fresh(**changes: object) -> PreflightResult:
    values: dict[str, object] = {
        "status": "PREFLIGHT_PASS",
        "ready_for_provider_adapter": True,
        "scene_id": "SCN_000001",
        "generation_profile_id": "GPR_0002",
        "compiled_prompt": "fresh prompt",
        "compiled_negative_prompt": "",
        "source_context_sha256": "a" * 64,
        "prompt_sha256": "b" * 64,
        "errors": (),
    }
    values.update(changes)
    return PreflightResult(**values)  # type: ignore[arg-type]


def _row(
    *, prompt_id: str = "SPV_000006", version: int = 6, status: str = "APPROVED"
) -> dict[str, object]:
    return {
        "Scene_Prompt_ID": prompt_id,
        "Scene_ID": "SCN_000001",
        "Prompt_Version": version,
        "Generation_Profile_ID": "GPR_0002",
        "Compiled_Prompt_Text": "old prompt" if status == "APPROVED" else "fresh prompt",
        "Compiled_Negative_Prompt": "",
        "Compiler_Version": "phase-e/1",
        "Source_Context_SHA256": "c" * 64 if status == "APPROVED" else "a" * 64,
        "Prompt_SHA256": "d" * 64 if status == "APPROVED" else "b" * 64,
        "Prompt_Status": status,
        "Validation_Message": "PREFLIGHT_PASS",
        "Approved_At": "2026-10-17T14:02:29Z" if status == "APPROVED" else "",
        "Created_At": "2026-10-17T14:00:28Z",
        "Updated_At": "2026-10-17T14:08:35Z",
    }


class FakePromptStore:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = [dict(row) for row in rows]
        self.fail_readback = False
        self.drift_before_approve = False
        self.mutations: list[str] = []

    def read_prompt_rows(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.rows]

    def append_candidate(
        self,
        row: dict[str, object],
        *,
        expected_snapshot_sha256: str,
        fresh_phase_e: PreflightResult,
    ) -> dict[str, object]:
        del expected_snapshot_sha256, fresh_phase_e
        if any(item["Scene_Prompt_ID"] == row["Scene_Prompt_ID"] for item in self.rows):
            raise PromptVersionLifecycleError("DUPLICATE_PROMPT_ID", "duplicate prompt id")
        self.rows.append(dict(row))
        self.mutations.append("append")
        return dict(self.rows[-1])

    def update_candidate(
        self, row: dict[str, object], *, expected_row_sha256: str, fresh_phase_e: PreflightResult
    ) -> dict[str, object]:
        del expected_row_sha256, fresh_phase_e
        for index, current in enumerate(self.rows):
            if current["Scene_Prompt_ID"] == row["Scene_Prompt_ID"]:
                self.rows[index] = dict(row)
                self.mutations.append("validate")
                return dict(row)
        raise PromptVersionLifecycleError("PROMPT_NOT_FOUND", "prompt row missing")

    def approve_pair(
        self,
        previous: dict[str, object],
        candidate: dict[str, object],
        *,
        expected_previous_sha256: str,
        expected_candidate_sha256: str,
        fresh_phase_e: PreflightResult,
    ) -> tuple[dict[str, object], dict[str, object]]:
        del expected_previous_sha256, expected_candidate_sha256, fresh_phase_e
        if self.drift_before_approve:
            self.rows[0]["Updated_At"] = "2026-10-18T00:00:00Z"
        old_index = next(
            i
            for i, row in enumerate(self.rows)
            if row["Scene_Prompt_ID"] == previous["Scene_Prompt_ID"]
        )
        new_index = next(
            i
            for i, row in enumerate(self.rows)
            if row["Scene_Prompt_ID"] == candidate["Scene_Prompt_ID"]
        )
        if self.drift_before_approve:
            raise PromptVersionLifecycleError("CONCURRENT_MODIFICATION", "prompt state changed")
        self.rows[old_index] = dict(previous)
        self.rows[new_index] = dict(candidate)
        self.mutations.append("approve")
        if self.fail_readback:
            raise PromptVersionLifecycleError("READBACK_MISMATCH", "approval readback failed")
        return dict(previous), dict(candidate)


def _service(
    store: FakePromptStore, phase: PreflightResult | None = None
) -> PromptVersionLifecycle:
    current = phase or _fresh()
    return PromptVersionLifecycle(
        store,
        phase_e=lambda _scene, _profile: current,
        compiler_version="phase-e/1",
        clock=lambda: datetime(2026, 10, 18, tzinfo=UTC),
    )


def test_create_allocates_next_id_and_version_as_draft() -> None:
    store = FakePromptStore([_row()])
    candidate = _service(store).create_prompt_version(
        "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
    )
    assert candidate["Scene_Prompt_ID"] == "SPV_000007"
    assert candidate["Prompt_Version"] == 7
    assert candidate["Prompt_Status"] == "DRAFT"
    assert store.rows[0]["Prompt_Status"] == "APPROVED"


def test_next_version_uses_max_persisted_version_not_current_version() -> None:
    store = FakePromptStore([_row(), _row(prompt_id="SPV_000004", version=11, status="SUPERSEDED")])
    candidate = _service(store).create_prompt_version(
        "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
    )
    assert candidate["Prompt_Version"] == 12


def test_duplicate_logical_version_in_live_rows_is_rejected() -> None:
    store = FakePromptStore([_row(), _row(prompt_id="SPV_000005", version=6, status="SUPERSEDED")])
    with pytest.raises(PromptVersionLifecycleError, match="duplicate"):
        _service(store).create_prompt_version(
            "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
        )
    assert store.mutations == []


def test_duplicate_prompt_id_readback_is_rejected() -> None:
    store = FakePromptStore([_row(), _row(prompt_id="SPV_000006", version=7, status="SUPERSEDED")])
    with pytest.raises(PromptVersionLifecycleError):
        _service(store).create_prompt_version(
            "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
        )
    assert store.mutations == []


def test_exact_fresh_validation_transitions_draft_to_validated() -> None:
    store = FakePromptStore([_row()])
    service = _service(store)
    candidate = service.create_prompt_version(
        "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
    )
    validated = service.validate_prompt_version(str(candidate["Scene_Prompt_ID"]))
    assert validated["Prompt_Status"] == "VALIDATED"
    assert validated["Prompt_SHA256"] == "b" * 64


@pytest.mark.parametrize(
    ("field", "code"),
    [
        ("prompt_sha256", "PROMPT_HASH_MISMATCH"),
        ("source_context_sha256", "SOURCE_CONTEXT_HASH_MISMATCH"),
    ],
)
def test_hash_mismatch_does_not_validate_or_mutate_previous_approval(field: str, code: str) -> None:
    store = FakePromptStore([_row()])
    service = _service(store)
    candidate = service.create_prompt_version(
        "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
    )
    stale = _fresh(**{field: "e" * 64})
    service.phase_e = lambda _scene, _profile: stale
    before = dict(store.rows[0])
    with pytest.raises(PromptVersionLifecycleError) as exc:
        service.validate_prompt_version(str(candidate["Scene_Prompt_ID"]))
    assert exc.value.code == code
    assert store.rows[0] == before
    assert store.rows[-1]["Prompt_Status"] == "DRAFT"


def test_draft_cannot_be_approved_directly() -> None:
    store = FakePromptStore([_row()])
    candidate = _service(store).create_prompt_version(
        "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
    )
    with pytest.raises(PromptVersionLifecycleError, match="VALIDATED"):
        _service(store).approve_prompt_version(
            str(candidate["Scene_Prompt_ID"]), expected_previous_id="SPV_000006"
        )
    assert store.rows[0]["Prompt_Status"] == "APPROVED"


def test_validated_candidate_stale_at_approval_is_rejected() -> None:
    store = FakePromptStore([_row()])
    service = _service(store)
    candidate = service.create_prompt_version(
        "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
    )
    service.validate_prompt_version(str(candidate["Scene_Prompt_ID"]))
    service.phase_e = lambda _scene, _profile: _fresh(source_context_sha256="e" * 64)
    with pytest.raises(PromptVersionLifecycleError) as exc:
        service.approve_prompt_version(
            str(candidate["Scene_Prompt_ID"]), expected_previous_id="SPV_000006"
        )
    assert exc.value.code == "SOURCE_CONTEXT_HASH_MISMATCH"
    assert store.rows[0]["Prompt_Status"] == "APPROVED"


def test_approval_supersedes_previous_and_leaves_one_approved() -> None:
    store = FakePromptStore([_row()])
    service = _service(store)
    candidate = service.create_prompt_version(
        "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
    )
    service.validate_prompt_version(str(candidate["Scene_Prompt_ID"]))
    result = service.approve_prompt_version(
        str(candidate["Scene_Prompt_ID"]), expected_previous_id="SPV_000006"
    )
    assert store.rows[0]["Prompt_Status"] == "SUPERSEDED"
    assert result["Prompt_Status"] == "APPROVED"
    assert sum(row["Prompt_Status"] == "APPROVED" for row in store.rows) == 1


def test_current_approval_is_derived_from_approved_status() -> None:
    from gflow_cli.v2_runtime.prompt_version_lifecycle import current_approved_rows

    assert current_approved_rows([_row(), _row(prompt_id="SPV_000005", status="SUPERSEDED")]) == [
        _row()
    ]


def test_prompt_payload_immutability_guard_rejects_field_edit() -> None:
    original = _row()
    proposed = {**original, "Compiled_Prompt_Text": "tampered"}
    with pytest.raises(PromptApprovalIntegrityError):
        guard_scene_prompt_version_update(original, proposed)


def test_concurrent_current_row_drift_aborts_approval() -> None:
    store = FakePromptStore([_row()])
    service = _service(store)
    candidate = service.create_prompt_version(
        "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
    )
    service.validate_prompt_version(str(candidate["Scene_Prompt_ID"]))
    store.drift_before_approve = True
    with pytest.raises(PromptVersionLifecycleError) as exc:
        service.approve_prompt_version(
            str(candidate["Scene_Prompt_ID"]), expected_previous_id="SPV_000006"
        )
    assert exc.value.code == "CONCURRENT_MODIFICATION"
    assert store.rows[0]["Prompt_Status"] == "APPROVED"


def test_failed_approval_does_not_supersede_current_prompt() -> None:
    store = FakePromptStore([_row()])
    service = _service(store)
    candidate = service.create_prompt_version(
        "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
    )
    service.validate_prompt_version(str(candidate["Scene_Prompt_ID"]))
    store.drift_before_approve = True
    with pytest.raises(PromptVersionLifecycleError):
        service.approve_prompt_version(
            str(candidate["Scene_Prompt_ID"]), expected_previous_id="SPV_000006"
        )
    assert store.rows[0]["Prompt_Status"] == "APPROVED"


def test_failed_readback_stops_approval() -> None:
    store = FakePromptStore([_row()])
    service = _service(store)
    candidate = service.create_prompt_version(
        "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
    )
    service.validate_prompt_version(str(candidate["Scene_Prompt_ID"]))
    store.fail_readback = True
    with pytest.raises(PromptVersionLifecycleError) as exc:
        service.approve_prompt_version(
            str(candidate["Scene_Prompt_ID"]), expected_previous_id="SPV_000006"
        )
    assert exc.value.code == "READBACK_MISMATCH"


def test_preview_has_no_side_effect_and_exposes_only_hashes_and_ids() -> None:
    store = FakePromptStore([_row()])
    preview = _service(store).preview("SCN_000001", "GPR_0002")
    assert preview.scene_prompt_id == "SPV_000007"
    assert preview.prompt_version == 7
    assert preview.prompt_sha256 == "b" * 64
    assert preview.source_context_sha256 == "a" * 64
    assert store.mutations == []


def test_phase_e_identity_mismatch_is_rejected_before_write() -> None:
    store = FakePromptStore([_row()])
    service = _service(store, _fresh(scene_id="SCN_000002"))
    with pytest.raises(PromptVersionLifecycleError):
        service.create_prompt_version("SCN_000001", "GPR_0002", expected_current_id="SPV_000006")
    assert store.mutations == []
