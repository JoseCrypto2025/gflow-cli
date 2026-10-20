from __future__ import annotations

import hashlib

import pytest

import gflow_cli.v2_runtime.phase_g_live_flow as live_flow
from gflow_cli.v2_runtime.phase_g import FlowSubmissionBaseline
from gflow_cli.v2_runtime.phase_g_live_flow import GoogleFlowAgentOnlyGenerationPort


class _Gate:
    async def evaluate(self, _script: str) -> str:
        return "<gate>late confirmation</gate>"

    async def inner_text(self) -> str:
        return "Generate 1 video for 10 credits"


class _Locator:
    def __init__(self, gate_after_polls: int | None) -> None:
        self.gate_after_polls = gate_after_polls
        self.polls = 0
        self.first = _Gate()
        self.last = _Gate()

    async def count(self) -> int:
        self.polls += 1
        return int(self.gate_after_polls is not None and self.polls >= self.gate_after_polls)


class _Page:
    def __init__(self, locator: _Locator) -> None:
        self._locator = locator

    def locator(self, _selector: str) -> _Locator:
        return self._locator

    async def evaluate(self, _script: str) -> dict[str, object]:
        return {
            "pending": 0,
            "media_ids": [],
            "finished_media_ids": [],
            "finished_keys": [],
            "chat_count": 0,
            "chat_tail_hash": "",
            "chat_media_ids": [],
        }


def _port(gate_after_polls: int | None) -> GoogleFlowAgentOnlyGenerationPort:
    port = GoogleFlowAgentOnlyGenerationPort.__new__(GoogleFlowAgentOnlyGenerationPort)
    port._page = _Page(_Locator(gate_after_polls))  # type: ignore[assignment]
    port._response_tasks = set()
    port._waf_rejected = False
    port._records = {}
    port._baseline_head_key = ""
    port._approved_gate_id = None
    port._observed_media_ids = set()
    port._observed_finished_media_ids = set()
    port._observed_peak_pending = 0
    port._observed_new_turn = False
    port._observed_head_changed = False
    return port


def _baseline() -> FlowSubmissionBaseline:
    return FlowSubmissionBaseline("project", frozenset(), frozenset(), 0, frozenset(), "0:")


@pytest.mark.asyncio
async def test_live_adapter_observes_gate_after_initial_30_second_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(gate_after_polls=32)
    tick = 0

    def monotonic() -> float:
        nonlocal tick
        tick += 1
        return float(tick)

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(live_flow.time, "monotonic", monotonic)
    monkeypatch.setattr(live_flow.asyncio, "sleep", no_wait)
    gate = await port.await_confirmation(_baseline(), observation_deadline=500)

    assert gate is not None
    assert gate.video_count == 1
    assert gate.credit_cost == 10
    assert gate.confirmation_id == hashlib.sha256(b"<gate>late confirmation</gate>").hexdigest()


@pytest.mark.asyncio
async def test_live_adapter_returns_no_gate_without_failing_after_bounded_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(gate_after_polls=None)
    tick = 0

    def monotonic() -> float:
        nonlocal tick
        value = float(tick)
        tick += 1
        return value

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(live_flow.time, "monotonic", monotonic)
    monkeypatch.setattr(live_flow.asyncio, "sleep", no_wait)
    gate = await port.await_confirmation(_baseline(), observation_deadline=4)

    assert gate is None


@pytest.mark.asyncio
async def test_live_adapter_rejects_gate_already_present_in_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(gate_after_polls=1)
    gate_hash = hashlib.sha256(b"<gate>late confirmation</gate>").hexdigest()
    baseline = FlowSubmissionBaseline(
        "project", frozenset(), frozenset(), 0, frozenset({gate_hash}), "0:"
    )

    gate = await port.await_confirmation(baseline, observation_deadline=0)

    assert gate is None
    assert port._approved_gate_id is None
