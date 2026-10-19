from __future__ import annotations

from datetime import UTC, datetime

from pytest_bdd import given, scenarios, then, when

from gflow_cli.v2_runtime.phase_e import PreflightResult
from gflow_cli.v2_runtime.prompt_version_lifecycle import (
    PromptVersionLifecycle,
    PromptVersionLifecycleError,
)

scenarios("prompt_version_lifecycle.feature")

_FIELDS = (
    "Scene_Prompt_ID",
    "Scene_ID",
    "Prompt_Version",
    "Generation_Profile_ID",
    "Compiled_Prompt_Text",
    "Compiled_Negative_Prompt",
    "Compiler_Version",
    "Source_Context_SHA256",
    "Prompt_SHA256",
    "Prompt_Status",
    "Validation_Message",
    "Approved_At",
    "Created_At",
    "Updated_At",
)


def _row(
    prompt_id: str, version: int, status: str, *, source: str, prompt: str, text: str
) -> dict[str, object]:
    values = {
        "Scene_Prompt_ID": prompt_id,
        "Scene_ID": "SCN_000001",
        "Prompt_Version": version,
        "Generation_Profile_ID": "GPR_0002",
        "Compiled_Prompt_Text": text,
        "Compiled_Negative_Prompt": "",
        "Compiler_Version": "phase-e/1",
        "Source_Context_SHA256": source,
        "Prompt_SHA256": prompt,
        "Prompt_Status": status,
        "Validation_Message": "PREFLIGHT_PASS",
        "Approved_At": "",
        "Created_At": "2026-10-18T00:00:00Z",
        "Updated_At": "2026-10-18T00:00:00Z",
    }
    return {name: values[name] for name in _FIELDS}


def _phase_e() -> PreflightResult:
    return PreflightResult(
        status="PREFLIGHT_PASS",
        ready_for_provider_adapter=True,
        scene_id="SCN_000001",
        generation_profile_id="GPR_0002",
        compiled_prompt="fresh prompt",
        compiled_negative_prompt="",
        source_context_sha256="a" * 64,
        prompt_sha256="b" * 64,
        errors=(),
    )


class _Store:
    def __init__(self) -> None:
        self.rows = [
            _row("SPV_000006", 6, "APPROVED", source="c" * 64, prompt="d" * 64, text="old")
        ]

    def read_prompt_rows(self):
        return [dict(row) for row in self.rows]

    def append_candidate(self, row, *, expected_snapshot_sha256, fresh_phase_e):
        del expected_snapshot_sha256, fresh_phase_e
        self.rows.append(dict(row))
        return dict(row)

    def update_candidate(self, row, *, expected_row_sha256, fresh_phase_e):
        del expected_row_sha256, fresh_phase_e
        index = next(
            i
            for i, item in enumerate(self.rows)
            if item["Scene_Prompt_ID"] == row["Scene_Prompt_ID"]
        )
        self.rows[index] = dict(row)
        return dict(row)

    def approve_pair(
        self,
        previous,
        candidate,
        *,
        expected_previous_sha256,
        expected_candidate_sha256,
        fresh_phase_e,
    ):
        del expected_previous_sha256, expected_candidate_sha256, fresh_phase_e
        old_index = next(
            i
            for i, item in enumerate(self.rows)
            if item["Scene_Prompt_ID"] == previous["Scene_Prompt_ID"]
        )
        new_index = next(
            i
            for i, item in enumerate(self.rows)
            if item["Scene_Prompt_ID"] == candidate["Scene_Prompt_ID"]
        )
        self.rows[old_index], self.rows[new_index] = dict(previous), dict(candidate)
        return dict(previous), dict(candidate)


def _new_state() -> dict[str, object]:
    store = _Store()
    service = PromptVersionLifecycle(
        store,
        phase_e=lambda _scene, _profile: _phase_e(),
        clock=lambda: datetime(2026, 10, 18, tzinfo=UTC),
    )
    return {"store": store, "service": service, "result": None, "candidate_id": None}


@given("a current approved prompt and a stale validated candidate", target_fixture="state")
def stale_approval_state():
    state = _new_state()
    store = state["store"]
    store.rows.append(
        _row("SPV_000007", 7, "VALIDATED", source="e" * 64, prompt="b" * 64, text="fresh prompt")
    )
    state["candidate_id"] = "SPV_000007"
    return state


@given("a persisted candidate that exactly matches fresh Phase E", target_fixture="state")
def matching_approval_state():
    return _new_state()


@when("prompt approval is attempted against fresh Phase E")
@when("the candidate is approved")
def attempt_approval(state):
    service = state["service"]
    store = state["store"]
    try:
        if state["candidate_id"] is None:
            draft = service.create_prompt_version(
                "SCN_000001", "GPR_0002", expected_current_id="SPV_000006"
            )
            state["candidate_id"] = draft["Scene_Prompt_ID"]
            service.validate_prompt_version(str(state["candidate_id"]))
        result = service.approve_prompt_version(
            str(state["candidate_id"]), expected_previous_id="SPV_000006"
        )
        state["result"] = result["Prompt_Status"]
    except PromptVersionLifecycleError as exc:
        state["result"] = exc.code
    state["approved_rows"] = [row for row in store.rows if row["Prompt_Status"] == "APPROVED"]
    state["previous"] = next(row for row in store.rows if row["Scene_Prompt_ID"] == "SPV_000006")


@then("approval stops and the previous row remains unchanged")
def stale_does_not_supersede(state):
    assert state["result"] == "SOURCE_CONTEXT_HASH_MISMATCH"
    assert state["previous"]["Prompt_Status"] == "APPROVED"


@then("the prior approval is superseded and exactly one prompt remains approved")
def one_current_approval(state):
    assert state["result"] == "APPROVED"
    assert state["previous"]["Prompt_Status"] == "SUPERSEDED"
    assert len(state["approved_rows"]) == 1
