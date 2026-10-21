from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import gflow_cli.v2_runtime.phase_g_live_flow as live_flow
from gflow_cli.v2_runtime.phase_g import FlowSubmissionBaseline, PhaseGError
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
        self.matched_count = 1
        self.first = _Gate()
        self.last = _Gate()

    async def count(self) -> int:
        self.polls += 1
        if self.gate_after_polls is None or self.polls < self.gate_after_polls:
            return 0
        return self.matched_count


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


class _Base:
    async def select_project(self, project_id: str) -> SimpleNamespace:
        return SimpleNamespace(project_id=project_id)


def _port(gate_after_polls: int | None) -> GoogleFlowAgentOnlyGenerationPort:
    port = GoogleFlowAgentOnlyGenerationPort.__new__(GoogleFlowAgentOnlyGenerationPort)
    port._page = _Page(_Locator(gate_after_polls))  # type: ignore[assignment]
    port._response_tasks = set()
    port._waf_rejected = False
    port._records = {}
    port._baseline_head_key = ""
    port._approved_gate_id = None
    port._generate_clicked = False
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

    monkeypatch.setattr(live_flow, "_monotonic", monotonic)
    monkeypatch.setattr(live_flow, "_async_sleep", no_wait)
    gate = await port.await_confirmation(_baseline(), observation_deadline=500)

    assert gate is not None
    assert gate.video_count == 1
    assert gate.credit_cost == 10
    assert gate.confirmation_id == hashlib.sha256(b"<gate>late confirmation</gate>").hexdigest()


@pytest.mark.asyncio
async def test_live_adapter_persists_ordered_baseline_chat_hashes() -> None:
    port = _port(gate_after_polls=None)
    port._base = _Base()  # type: ignore[assignment]
    port._request = SimpleNamespace(provider_project_id="project")  # type: ignore[assignment]
    hashes = [hashlib.sha256(b"chat one").hexdigest(), hashlib.sha256(b"chat two").hexdigest()]
    port._page.evaluate = lambda _script: _async_result(  # type: ignore[method-assign]
        {
            "pending": 0,
            "media_ids": [],
            "finished_media_ids": [],
            "finished_keys": [],
            "chat_count": 2,
            "chat_tail_hash": "tail",
            "chat_media_ids": [],
            "chat_bubble_hashes": hashes,
        }
    )

    baseline = await port.capture_submission_baseline()

    assert baseline.agent_turn_id == "2:tail"
    assert baseline.agent_turn_bubble_hashes == tuple(hashes)


async def _async_result(value: dict[str, object]) -> dict[str, object]:
    return value


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

    monkeypatch.setattr(live_flow, "_monotonic", monotonic)
    monkeypatch.setattr(live_flow, "_async_sleep", no_wait)
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


@pytest.mark.asyncio
async def test_read_current_confirmation_waits_for_hydrated_single_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(gate_after_polls=3)
    now = 0.0

    def monotonic() -> float:
        return now

    async def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(live_flow, "_monotonic", monotonic)
    monkeypatch.setattr(live_flow, "_async_sleep", advance)

    gate = await port.read_current_confirmation(_baseline())

    assert gate.confirmation_id == hashlib.sha256(b"<gate>late confirmation</gate>").hexdigest()
    assert gate.video_count == 1
    assert gate.credit_cost == 10
    assert gate.progress_before_confirmation is False
    assert port._page._locator.polls == 3  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_current_confirmation_fails_closed_after_zero_gate_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(gate_after_polls=None)
    now = 0.0

    def monotonic() -> float:
        return now

    async def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(live_flow, "_monotonic", monotonic)
    monkeypatch.setattr(live_flow, "_async_sleep", advance)

    with pytest.raises(PhaseGError, match=r"observed count = 0") as error:
        await port.read_current_confirmation(_baseline())

    assert error.value.code == "PHASE_G_CONFIRMATION_NOT_ATTRIBUTABLE"
    assert port._approved_gate_id is None
    assert now == 15.0
    assert port._page._locator.polls == 16  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_read_current_confirmation_rejects_multiple_gates_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(gate_after_polls=1)
    port._page._locator.matched_count = 2  # type: ignore[attr-defined]
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(live_flow, "_async_sleep", record_sleep)

    with pytest.raises(PhaseGError, match=r"observed count = 2") as error:
        await port.read_current_confirmation(_baseline())

    assert error.value.code == "PHASE_G_CONFIRMATION_NOT_ATTRIBUTABLE"
    assert sleeps == []
    assert port._approved_gate_id is None


@pytest.mark.asyncio
async def test_read_current_confirmation_rejects_single_stale_gate_after_hydration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(gate_after_polls=1)
    gate_hash = hashlib.sha256(b"<gate>late confirmation</gate>").hexdigest()

    with pytest.raises(PhaseGError) as error:
        await port.read_current_confirmation(
            FlowSubmissionBaseline(
                "project", frozenset(), frozenset(), 0, frozenset({gate_hash}), "0:"
            )
        )

    assert error.value.code == "PHASE_G_STALE_CONFIRMATION"
    assert port._approved_gate_id is None


@pytest.mark.asyncio
async def test_read_current_confirmation_readiness_never_approves_generates_or_mutates_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port(gate_after_polls=2)
    ledger = tmp_path / "d2-credit-ledger.json"
    ledger.write_text('{"sentinel":true}', encoding="utf-8")
    ledger_before = ledger.read_bytes()
    now = 0.0

    def monotonic() -> float:
        return now

    async def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    async def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("readiness polling cannot perform a provider action")

    port.approve_confirmation = forbidden  # type: ignore[method-assign]
    port.click_generate_once = forbidden  # type: ignore[method-assign]
    monkeypatch.setattr(live_flow, "_monotonic", monotonic)
    monkeypatch.setattr(live_flow, "_async_sleep", advance)

    gate = await port.read_current_confirmation(_baseline())

    assert gate.video_count == 1
    assert ledger.read_bytes() == ledger_before
    assert port._generate_clicked is False
