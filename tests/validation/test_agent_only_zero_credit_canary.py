from __future__ import annotations

import asyncio

import pytest

from gflow_cli.v2_runtime.live_flow import GoogleFlowAgentOnlyPort
from scripts.operations import agent_only_zero_credit_canary as canary
from scripts.operations.agent_only_zero_credit_canary import (
    _sanitized_rpcids,
    _wait_for_stable_video_state,
)


class _FakeLoopClock:
    def __init__(self) -> None:
        self.current = 0.0

    def time(self) -> float:
        return self.current

    def advance(self, milliseconds: int) -> None:
        self.current += milliseconds / 1000


class _FakePage:
    def __init__(
        self, states: list[dict[str, object]], *, clock: _FakeLoopClock | None = None
    ) -> None:
        self.states = states
        self.reads = 0
        self.waited_ms = 0
        self.clock = clock

    async def evaluate(self, script: str) -> dict[str, object]:
        self.reads += 1
        return self.states[min(self.reads - 1, len(self.states) - 1)]

    async def wait_for_timeout(self, milliseconds: int) -> None:
        self.waited_ms += milliseconds
        if self.clock is not None:
            self.clock.advance(milliseconds)
            return
        await asyncio.sleep(min(milliseconds / 1000, 0.001))


def test_canary_keeps_only_rpcids_and_discards_other_url_parameters() -> None:
    assert _sanitized_rpcids(
        "https://flow.google.com/data/batchexecute?rpcids=WuwhI&prompt=private&token=secret"
    ) == ["WuwhI"]


def test_canary_ignores_rpcids_from_non_batchexecute_urls() -> None:
    assert _sanitized_rpcids("https://flow.google.com/project/123?rpcids=WuwhI") == []


def test_canary_service_worker_block_is_explicit_and_opt_in() -> None:
    base = GoogleFlowAgentOnlyPort(
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_account_id="PVA_0001",
        provider_project_id="34e60953-e534-46e9-ac65-993893e61065",
        project_name="AI Video Automation V2 - TEST",
        model_display_names={"MDL_0001": "Veo 3.1 - Lite"},
        block_service_workers=True,
    )
    normal = GoogleFlowAgentOnlyPort(
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_account_id="PVA_0001",
        provider_project_id="34e60953-e534-46e9-ac65-993893e61065",
        project_name="AI Video Automation V2 - TEST",
        model_display_names={"MDL_0001": "Veo 3.1 - Lite"},
    )

    assert base.block_service_workers is True
    assert normal.block_service_workers is False


async def test_canary_waits_for_project_state_to_hydrate_before_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeLoopClock()
    monkeypatch.setattr(canary.asyncio, "get_running_loop", lambda: clock)
    page = _FakePage(
        [
            {"chat_count": 0, "chat_bubble_hashes": []},
            {"chat_count": 0, "chat_bubble_hashes": []},
            {"chat_count": 0, "chat_bubble_hashes": []},
            {"chat_count": 6, "chat_bubble_hashes": ["loading"]},
            {"chat_count": 6, "chat_bubble_hashes": ["ready"]},
            {"chat_count": 6, "chat_bubble_hashes": ["ready"]},
            {"chat_count": 6, "chat_bubble_hashes": ["ready"]},
        ],
        clock=clock,
    )

    state = await _wait_for_stable_video_state(page, expected_chat_count=6)

    assert state["chat_count"] == 6
    assert page.reads == 7
    assert page.waited_ms >= 5_000


async def test_canary_fails_closed_if_project_state_never_stabilizes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _FakeLoopClock()
    monkeypatch.setattr(canary.asyncio, "get_running_loop", lambda: clock)
    monkeypatch.setattr(canary, "_STATE_STABILITY_TIMEOUT_S", 0.003)
    monkeypatch.setattr(canary, "_STATE_HYDRATION_SETTLE_MS", 0)
    monkeypatch.setattr(canary, "_STATE_STABILITY_INTERVAL_MS", 1)
    page = _FakePage(
        [
            {"chat_count": 0},
            {"chat_count": 1},
            {"chat_count": 0},
        ],
        clock=clock,
    )

    with pytest.raises(canary.FlowPresubmitError) as caught:
        await _wait_for_stable_video_state(page, expected_chat_count=1)

    assert caught.value.code == "AGENT_CANARY_STATE_UNSTABLE"
