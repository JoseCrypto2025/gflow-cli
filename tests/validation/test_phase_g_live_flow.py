from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import gflow_cli.v2_runtime.phase_g_live_flow as live_flow
from gflow_cli.api.transports.batchexecute import TEXT_TO_VIDEO_SUBMIT_RPC
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
            "chat_video_media_ids": [],
            "chat_count": 0,
            "chat_bubble_hashes": [],
            "chat_tail_hash": "",
            "chat_media_ids": [],
        }


class _Base:
    async def select_project(self, project_id: str) -> SimpleNamespace:
        return SimpleNamespace(project_id=project_id)

    async def inspect_surface(self) -> str:
        return "AGENT_ONLY"


class _EventPage(_Page):
    def __init__(self) -> None:
        super().__init__(_Locator(None))
        self.url = "https://flow.google.com/project/project"
        self.events: dict[str, object] = {}
        self.selectors: list[str] = []
        self.submit_request = SimpleNamespace(
            url="https://flow.google.com/data/batchexecute?rpcids=YhhmEf", method="POST"
        )

    def on(self, event: str, callback: object) -> None:
        self.events[event] = callback

    def remove_listener(self, event: str, _callback: object) -> None:
        self.events.pop(event, None)

    def locator(self, selector: str) -> Any:
        page = self
        page.selectors.append(selector)

        class Button:
            async def count(self) -> int:
                return 1

            async def is_visible(self) -> bool:
                return True

            async def is_enabled(self) -> bool:
                return True

            async def click(self, *, timeout: int) -> None:
                assert timeout == 5_000
                callback = page.events["request"]
                assert callable(callback)
                callback(page.submit_request)

        return Button()


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
    port._observed_finished_tile_key_hashes = set()
    port._observed_peak_pending = 0
    port._observed_new_turn = False
    port._observed_new_turn_bubble_index = None
    port._observed_head_changed = False
    port._submit_observer_armed = False
    port._submit_dispatch_started_at = None
    port._submit_dispatch_deadline = None
    port._submit_dispatch_window_completed = False
    port._generate_click_dispatched = None
    port._submit_requests = []
    port._submit_observed_at = None
    port._submit_project_id = None
    port._submit_request_failed = None
    port._submit_response_observed = False
    port._submit_response_status = None
    port._submit_generation_record_observed = None
    port._submit_refusal_detected = None
    port._submission_surface = "AGENT_ONLY"
    port._provider_directive_sha256 = None
    return port


def _baseline() -> FlowSubmissionBaseline:
    return FlowSubmissionBaseline("project", frozenset(), frozenset(), 0, frozenset(), "0:")


UUID_A = "aaaaaaaa-0000-0000-0000-000000000001"
UUID_B = "bbbbbbbb-0000-0000-0000-000000000002"
UUID_C = "cccccccc-0000-0000-0000-000000000003"
UUID_D = "dddddddd-0000-0000-0000-000000000004"
UUID_E = "eeeeeeee-0000-0000-0000-000000000005"
UUID_F = "ffffffff-0000-0000-0000-000000000006"


@pytest.mark.asyncio
async def test_submit_request_observer_is_armed_before_generate_click() -> None:
    port = _port(None)
    page = _EventPage()
    port._page = page  # type: ignore[assignment]
    port._base = _Base()  # type: ignore[assignment]
    port._request = SimpleNamespace(provider_project_id="project")  # type: ignore[assignment]
    port._baseline = _baseline()

    await port.arm_submit_request_observer()
    assert "request" in page.events
    await port.click_generate_once()

    evidence = port.read_submit_transport_evidence()
    assert evidence.generate_click_dispatched is True
    assert evidence.provider_submit_rpc_observed is True
    assert evidence.provider_submit_rpc_id == TEXT_TO_VIDEO_SUBMIT_RPC
    assert "flow-generate-icon-button button" in page.selectors


@pytest.mark.asyncio
async def test_agent_turn_dispatch_does_not_wait_for_or_require_yhhmef(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive = "Make me a 8 second video of approved prompt in a 9:16 aspect ratio."
    prompt_hash = hashlib.sha256(directive.encode()).hexdigest()
    baseline = FlowSubmissionBaseline("project", frozenset(), frozenset(), 0, frozenset(), "0:old")
    port = _observation_port(
        [
            _project_state(
                pending=0,
                media_ids=[],
                chat_count=1,
                chat_tail_hash="new",
                chat_bubble_hashes=[prompt_hash],
            )
        ],
        [],
    )
    port._baseline = baseline
    port._submission_surface = "AGENT_ONLY"
    port._provider_directive_sha256 = prompt_hash
    port._submit_dispatch_deadline = 15.0
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.await_submit_dispatch()

    assert evidence.submission_surface == "AGENT_ONLY"
    assert evidence.agent_turn_dispatched is True
    assert evidence.provider_submit_request_count == 0
    assert evidence.provider_submit_rpc_id is None


def test_unrelated_batchexecute_request_does_not_count_as_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(None)
    port._page = SimpleNamespace(url="https://flow.google.com/project/project")  # type: ignore[assignment]
    port._request = SimpleNamespace(provider_project_id="project")  # type: ignore[assignment]
    port._submit_observer_armed = True
    port._submit_dispatch_started_at = 1.0
    port._submit_dispatch_deadline = 20.0
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 2.0)

    request = SimpleNamespace(
        url="https://flow.google.com/data/batchexecute?rpcids=jwpduf", method="POST"
    )
    port._on_request(request)

    assert port.read_submit_transport_evidence().provider_submit_request_count == 0


def test_duplicate_submit_requests_count_without_reading_sensitive_request_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(None)
    port._page = SimpleNamespace(url="https://flow.google.com/project/project")  # type: ignore[assignment]
    port._request = SimpleNamespace(provider_project_id="project")  # type: ignore[assignment]
    port._submit_observer_armed = True
    port._submit_dispatch_started_at = 1.0
    port._submit_dispatch_deadline = 20.0
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 2.0)

    class SensitiveRequest:
        url = "https://flow.google.com/data/batchexecute?rpcids=YhhmEf"
        method = "POST"

        @property
        def post_data(self) -> str:
            raise AssertionError("request body must never be read")

        @property
        def headers(self) -> dict[str, str]:
            raise AssertionError("request headers must never be read")

    port._on_request(SensitiveRequest())
    port._on_request(SensitiveRequest())
    evidence = port.read_submit_transport_evidence()

    assert evidence.provider_submit_request_count == 2
    assert evidence.provider_submit_rpc_observed is True
    assert evidence.provider_submit_rpc_id == TEXT_TO_VIDEO_SUBMIT_RPC
    assert evidence.provider_submit_http_method == "POST"
    assert not hasattr(evidence, "post_data")
    assert not hasattr(evidence, "headers")


@pytest.mark.asyncio
async def test_submit_response_generation_record_is_correlated_to_exact_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(None)
    request = SimpleNamespace(
        url="https://flow.google.com/data/batchexecute?rpcids=YhhmEf", method="POST"
    )
    port._request = SimpleNamespace(provider_project_id="project")  # type: ignore[assignment]
    port._submit_requests = [request]

    class Response:
        url = "https://flow.google.com/data/batchexecute?rpcids=YhhmEf"
        status = 200

        def __init__(self) -> None:
            self.request = request

        async def text(self) -> str:
            return "SENSITIVE_RESPONSE_BODY"

    monkeypatch.setattr(live_flow, "rpc_errors", lambda _body: [])
    monkeypatch.setattr(live_flow, "parse_frames", lambda _body: [("YhhmEf", {})])
    monkeypatch.setattr(
        live_flow,
        "generation_record",
        lambda _rpcid, _payload: SimpleNamespace(
            workflow_id="workflow", project_id="project", media_id="media", status=6
        ),
    )

    await port._capture_response(Response())  # type: ignore[arg-type]

    evidence = port.read_submit_transport_evidence()
    assert evidence.provider_submit_response_observed is True
    assert evidence.provider_submit_response_status == 200
    assert evidence.provider_submit_generation_record_observed is True
    assert evidence.provider_submit_refusal_detected is False
    assert "SENSITIVE_RESPONSE_BODY" not in repr(evidence)


@pytest.mark.asyncio
async def test_submit_response_without_record_stays_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(None)
    request = SimpleNamespace(url="request", method="POST")
    port._submit_requests = [request]
    port._request = SimpleNamespace(provider_project_id="project")  # type: ignore[assignment]

    class Response:
        url = "https://flow.google.com/data/batchexecute?rpcids=YhhmEf"
        status = 200

        def __init__(self) -> None:
            self.request = request

        async def text(self) -> str:
            return "response without a generation record"

    monkeypatch.setattr(live_flow, "rpc_errors", lambda _body: [])
    monkeypatch.setattr(live_flow, "parse_frames", lambda _body: [])

    await port._capture_response(Response())  # type: ignore[arg-type]

    evidence = port.read_submit_transport_evidence()
    assert evidence.provider_submit_response_observed is True
    assert evidence.provider_submit_generation_record_observed is False
    assert evidence.provider_submit_refusal_detected is False


@pytest.mark.asyncio
async def test_unreadable_submit_reply_remains_observation_only_and_nonretryable() -> None:
    port = _port(None)
    request = SimpleNamespace(url="request", method="POST")
    port._submit_requests = [request]
    port._request = SimpleNamespace(provider_project_id="project")  # type: ignore[assignment]

    class Response:
        url = "https://flow.google.com/data/batchexecute?rpcids=YhhmEf"
        status = 200

        def __init__(self) -> None:
            self.request = request

        async def text(self) -> str:
            raise RuntimeError("response body unavailable")

    await port._capture_response(Response())  # type: ignore[arg-type]

    evidence = port.read_submit_transport_evidence()
    assert evidence.provider_submit_response_observed is True
    assert evidence.provider_submit_generation_record_observed is False
    assert len(port._records) == 0
    assert port._submit_requests == [request]


@pytest.mark.asyncio
async def test_submit_waf_response_keeps_existing_refusal_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _port(None)
    request = SimpleNamespace(url="request", method="POST")
    port._submit_requests = [request]
    port._request = SimpleNamespace(provider_project_id="project")  # type: ignore[assignment]

    class Response:
        url = "https://flow.google.com/data/batchexecute?rpcids=YhhmEf"
        status = 200

        def __init__(self) -> None:
            self.request = request

        async def text(self) -> str:
            return "sanitized fixture"

    monkeypatch.setattr(
        live_flow,
        "rpc_errors",
        lambda _body: [SimpleNamespace(reasons=("PUBLIC_ERROR_UNUSUAL_ACTIVITY",))],
    )
    monkeypatch.setattr(live_flow, "parse_frames", lambda _body: [])

    await port._capture_response(Response())  # type: ignore[arg-type]

    assert port._waf_rejected is True
    assert port.read_submit_transport_evidence().provider_submit_refusal_detected is True


def _wire_record(
    workflow_id: str,
    media_id: str,
    status: int,
    rpcid: str | None = None,
    submit_request: bool = False,
) -> live_flow._ObservedRecord:
    return live_flow._ObservedRecord(
        workflow_id, "project", media_id, status, rpcid, submit_request
    )


def _observation_port(
    states: list[dict[str, object]], records: list[live_flow._ObservedRecord]
) -> GoogleFlowAgentOnlyGenerationPort:
    port = _port(gate_after_polls=None)
    port._request = SimpleNamespace(d2_run_id="D2_test", provider_project_id="project")  # type: ignore[assignment]
    port._records = {
        (record.workflow_id, record.project_id, record.media_id): record for record in records
    }
    port._submission_surface = "CLASSIC_MIGRATED"
    page = port._page
    page.states = states  # type: ignore[attr-defined]
    page.evaluate_calls = 0  # type: ignore[attr-defined]

    async def evaluate(_script: str) -> dict[str, object]:
        page.evaluate_calls += 1  # type: ignore[attr-defined]
        return page.states[min(page.evaluate_calls - 1, len(page.states) - 1)]  # type: ignore[attr-defined]

    page.evaluate = evaluate  # type: ignore[method-assign]
    return port


def _project_state(
    *,
    pending: int,
    media_ids: list[str],
    finished_media_ids: list[str] | None = None,
    chat_count: int = 1,
    chat_tail_hash: str = "new-turn",
    chat_bubble_hashes: list[str] | None = None,
    chat_video_media_ids: list[str] | None = None,
    finished_tile_key_hashes: list[str] | None = None,
    chat_video_options: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "pending": pending,
        "media_ids": media_ids,
        "finished_media_ids": finished_media_ids or [],
        "finished_tile_key_hashes": finished_tile_key_hashes or [],
        "finished_keys": [],
        "chat_count": chat_count,
        "chat_bubble_hashes": chat_bubble_hashes or [],
        "chat_tail_hash": chat_tail_hash,
        "chat_media_ids": [],
        "chat_video_media_ids": chat_video_media_ids or [],
        "chat_video_options": chat_video_options or [],
    }


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


@pytest.mark.asyncio
async def test_workflow_and_media_dom_uuids_are_not_two_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _observation_port(
        [_project_state(pending=1, media_ids=[UUID_A, UUID_B])],
        [_wire_record(UUID_A, UUID_B, 2)],
    )
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(_baseline(), None, observation_deadline=0.0)

    assert evidence.submission_state == "GENERATION_PENDING"
    assert evidence.provider_execution_id == UUID_A
    assert evidence.provider_media_id == UUID_B
    assert evidence.new_provider_media_ids == (UUID_B,)


@pytest.mark.asyncio
async def test_one_wire_media_and_workflow_stays_observable_while_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _observation_port(
        [
            _project_state(pending=1, media_ids=[UUID_A, UUID_B]),
            _project_state(pending=0, media_ids=[UUID_A, UUID_B], finished_media_ids=[UUID_B]),
        ],
        [_wire_record(UUID_A, UUID_B, 2)],
    )
    now = 0.0

    def monotonic() -> float:
        return now

    async def advance(seconds: float) -> None:
        nonlocal now
        now += seconds
        if now >= 1.0:
            port._records[(UUID_A, "project", UUID_B)] = _wire_record(UUID_A, UUID_B, 3)

    monkeypatch.setattr(live_flow, "_monotonic", monotonic)
    monkeypatch.setattr(live_flow, "_async_sleep", advance)

    evidence = await port.observe_attempt(_baseline(), None, observation_deadline=4.0)

    assert evidence.submission_state == "GENERATION_SUCCEEDED"
    assert evidence.provider_execution_id == UUID_A
    assert evidence.provider_media_id == UUID_B
    assert evidence.new_provider_media_ids == (UUID_B,)
    assert port._page.evaluate_calls == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_one_completed_authoritative_media_is_generation_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _observation_port(
        [_project_state(pending=0, media_ids=[UUID_A, UUID_B], finished_media_ids=[UUID_B])],
        [_wire_record(UUID_A, UUID_B, 3)],
    )
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(_baseline(), None, observation_deadline=10.0)

    assert evidence.submission_state == "GENERATION_SUCCEEDED"
    assert evidence.provider_media_id == UUID_B
    assert evidence.new_provider_media_ids == (UUID_B,)


@pytest.mark.asyncio
async def test_agent_only_correlates_new_chat_video_with_one_finished_tile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive = "Make me a 8 second video of approved prompt in a 9:16 aspect ratio."
    prompt_hash = hashlib.sha256(directive.encode()).hexdigest()
    port = _observation_port(
        [
            _project_state(
                pending=0,
                media_ids=[UUID_A, UUID_B],
                finished_media_ids=[UUID_B],
                chat_count=1,
                chat_tail_hash="new",
                chat_bubble_hashes=[prompt_hash],
                chat_video_options=[{"media_id": UUID_B, "chat_bubble_index": 0}],
                finished_tile_key_hashes=[hashlib.sha256(b"finished-b").hexdigest()],
            )
        ],
        [],
    )
    port._submission_surface = "AGENT_ONLY"
    port._provider_directive_sha256 = prompt_hash
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(_baseline(), None, observation_deadline=0.0)

    assert evidence.submission_state == "GENERATION_SUCCEEDED"
    assert evidence.provider_media_id == UUID_B
    assert evidence.provider_execution_id is None
    assert evidence.new_provider_media_ids == (UUID_B,)


@pytest.mark.asyncio
async def test_agent_only_opaque_finished_tile_correlates_exact_new_chat_poster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive = "Make me a 8 second video of approved prompt in a 9:16 aspect ratio."
    prompt_hash = hashlib.sha256(directive.encode()).hexdigest()
    baseline = FlowSubmissionBaseline(
        "project",
        frozenset(),
        frozenset(),
        0,
        frozenset(),
        "1:baseline-tail",
        ("a" * 64,),
        ("b" * 64,),
        frozenset(),
    )
    port = _observation_port(
        [
            _project_state(
                pending=0,
                media_ids=[],
                finished_media_ids=[],
                chat_count=3,
                chat_tail_hash="new-tail",
                chat_bubble_hashes=["a" * 64, prompt_hash, "c" * 64],
                finished_tile_key_hashes=["d" * 64, "b" * 64],
                chat_video_options=[{"media_id": UUID_B, "chat_bubble_index": 2}],
            )
        ],
        [],
    )
    port._submission_surface = "AGENT_ONLY"
    port._provider_directive_sha256 = prompt_hash
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(baseline, None, observation_deadline=0.0)

    assert evidence.submission_state == "GENERATION_SUCCEEDED"
    assert evidence.provider_media_id == UUID_B
    assert evidence.new_provider_media_ids == (UUID_B,)


@pytest.mark.asyncio
async def test_agent_only_requires_new_tile_to_be_the_newest_finished_tile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive = "Make me a 8 second video of approved prompt in a 9:16 aspect ratio."
    prompt_hash = hashlib.sha256(directive.encode()).hexdigest()
    old_key = hashlib.sha256(b"old-newest").hexdigest()
    port = _observation_port(
        [
            _project_state(
                pending=0,
                media_ids=[],
                chat_count=2,
                chat_tail_hash="new-reply",
                chat_bubble_hashes=[prompt_hash, "c" * 64],
                finished_tile_key_hashes=[old_key, "d" * 64],
                chat_video_options=[{"media_id": UUID_B, "chat_bubble_index": 1}],
            )
        ],
        [],
    )
    baseline = FlowSubmissionBaseline(
        "project",
        frozenset(),
        frozenset(),
        0,
        frozenset(),
        "1:old-tail",
        ("a" * 64,),
        (old_key,),
        frozenset(),
    )
    port._submission_surface = "AGENT_ONLY"
    port._provider_directive_sha256 = prompt_hash
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(baseline, None, observation_deadline=0.0)

    assert evidence.submission_state == "UNKNOWN_SUBMISSION_STATE"
    assert evidence.new_provider_media_ids == ()


@pytest.mark.asyncio
async def test_agent_only_pending_tile_transitions_to_correlated_finished_tile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive = "Make me a 8 second video of approved prompt in a 9:16 aspect ratio."
    prompt_hash = hashlib.sha256(directive.encode()).hexdigest()
    port = _observation_port(
        [
            _project_state(
                pending=1,
                media_ids=[],
                chat_count=1,
                chat_tail_hash="new-prompt",
                chat_bubble_hashes=[prompt_hash],
            ),
            _project_state(
                pending=0,
                media_ids=[],
                chat_count=2,
                chat_tail_hash="new-reply",
                chat_bubble_hashes=[prompt_hash, "c" * 64],
                finished_tile_key_hashes=["d" * 64],
                chat_video_options=[{"media_id": UUID_B, "chat_bubble_index": 1}],
            ),
        ],
        [],
    )
    port._submission_surface = "AGENT_ONLY"
    port._provider_directive_sha256 = prompt_hash
    now = 0.0

    def monotonic() -> float:
        return now

    async def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(live_flow, "_monotonic", monotonic)
    monkeypatch.setattr(live_flow, "_async_sleep", advance)

    evidence = await port.observe_attempt(_baseline(), None, observation_deadline=2.0)

    assert evidence.submission_state == "GENERATION_SUCCEEDED"
    assert evidence.provider_media_id == UUID_B
    assert port._page.evaluate_calls == 2  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_agent_only_generation_record_is_not_media_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive = "Make me a 8 second video of approved prompt in a 9:16 aspect ratio."
    prompt_hash = hashlib.sha256(directive.encode()).hexdigest()
    port = _observation_port(
        [
            _project_state(
                pending=0,
                media_ids=[UUID_A, UUID_B],
                chat_count=1,
                chat_tail_hash="new-tail",
                chat_bubble_hashes=[prompt_hash],
                finished_media_ids=[UUID_B],
                chat_video_options=[],
            )
        ],
        [_wire_record(UUID_A, UUID_B, 3, "YhhmEf", submit_request=True)],
    )
    port._submission_surface = "AGENT_ONLY"
    port._provider_directive_sha256 = prompt_hash
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(_baseline(), None, observation_deadline=0.0)

    assert evidence.submission_state == "UNKNOWN_SUBMISSION_STATE"
    assert evidence.new_provider_media_ids == ()


@pytest.mark.asyncio
async def test_agent_only_two_finished_chat_media_ids_are_multi_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive = "Make me a 8 second video of approved prompt in a 9:16 aspect ratio."
    prompt_hash = hashlib.sha256(directive.encode()).hexdigest()
    port = _observation_port(
        [
            _project_state(
                pending=0,
                media_ids=[],
                chat_count=2,
                chat_tail_hash="new-reply",
                chat_bubble_hashes=[prompt_hash, "c" * 64],
                finished_tile_key_hashes=["d" * 64, "e" * 64],
                chat_video_options=[
                    {"media_id": UUID_B, "chat_bubble_index": 1},
                    {"media_id": UUID_C, "chat_bubble_index": 1},
                ],
            )
        ],
        [],
    )
    port._submission_surface = "AGENT_ONLY"
    port._provider_directive_sha256 = prompt_hash
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(_baseline(), None, observation_deadline=0.0)

    assert evidence.submission_state == "UNEXPECTED_MULTI_OUTPUT"
    assert evidence.new_provider_media_ids == tuple(sorted((UUID_B, UUID_C)))


@pytest.mark.asyncio
async def test_as29s_record_alone_is_not_agent_only_output_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive = "Make me a 8 second video of approved prompt in a 9:16 aspect ratio."
    prompt_hash = hashlib.sha256(directive.encode()).hexdigest()
    port = _observation_port(
        [
            _project_state(
                pending=0,
                media_ids=[UUID_A, UUID_B],
                chat_count=1,
                chat_tail_hash="new",
                chat_bubble_hashes=[prompt_hash],
                chat_video_options=[],
            )
        ],
        [_wire_record(UUID_A, UUID_B, 3, "as29s")],
    )
    port._submission_surface = "AGENT_ONLY"
    port._provider_directive_sha256 = prompt_hash
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(_baseline(), None, observation_deadline=0.0)

    assert evidence.submission_state == "UNKNOWN_SUBMISSION_STATE"
    assert evidence.new_provider_media_ids == ()


@pytest.mark.asyncio
async def test_unrelated_wire_record_is_not_agent_only_attempt_attribution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directive = "Make me a 8 second video of approved prompt in a 9:16 aspect ratio."
    prompt_hash = hashlib.sha256(directive.encode()).hexdigest()
    port = _observation_port(
        [
            _project_state(
                pending=0,
                media_ids=[UUID_A, UUID_B],
                chat_count=1,
                chat_tail_hash="new",
                chat_bubble_hashes=[prompt_hash],
            )
        ],
        [_wire_record(UUID_A, UUID_B, 3, "jwpduf")],
    )
    port._submission_surface = "AGENT_ONLY"
    port._provider_directive_sha256 = prompt_hash
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(_baseline(), None, observation_deadline=0.0)

    assert evidence.submission_state == "UNKNOWN_SUBMISSION_STATE"
    assert evidence.new_provider_media_ids == ()


@pytest.mark.asyncio
async def test_two_distinct_authoritative_media_ids_are_multi_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _observation_port(
        [_project_state(pending=0, media_ids=[])],
        [_wire_record(UUID_A, UUID_B, 3), _wire_record(UUID_A, UUID_C, 3)],
    )
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(_baseline(), None, observation_deadline=10.0)

    assert evidence.submission_state == "UNEXPECTED_MULTI_OUTPUT"
    assert evidence.new_provider_media_ids == tuple(sorted((UUID_B, UUID_C)))


@pytest.mark.parametrize("status", [2, 3])
@pytest.mark.asyncio
async def test_historical_record_omitted_from_live_dom_is_not_new_output(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    port = _observation_port(
        [_project_state(pending=0, media_ids=[])],
        [_wire_record(UUID_A, UUID_B, status)],
    )
    baseline = FlowSubmissionBaseline(
        "project", frozenset({UUID_B}), frozenset({UUID_A}), 0, frozenset(), "0:"
    )
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(baseline, None, observation_deadline=0.0)

    assert evidence.submission_state == "UNKNOWN_SUBMISSION_STATE"
    assert evidence.new_provider_media_ids == ()


@pytest.mark.asyncio
async def test_new_media_with_historical_ids_in_baseline_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _observation_port(
        [_project_state(pending=0, media_ids=[UUID_C, UUID_D], finished_media_ids=[UUID_D])],
        [_wire_record(UUID_C, UUID_D, 3)],
    )
    baseline = FlowSubmissionBaseline(
        "project", frozenset({UUID_B}), frozenset({UUID_A}), 0, frozenset(), "0:"
    )
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(baseline, None, observation_deadline=10.0)

    assert evidence.submission_state == "GENERATION_SUCCEEDED"
    assert evidence.provider_execution_id == UUID_C
    assert evidence.provider_media_id == UUID_D
    assert evidence.new_provider_media_ids == (UUID_D,)


@pytest.mark.asyncio
async def test_historical_plus_new_record_counts_only_new_media(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _observation_port(
        [_project_state(pending=0, media_ids=[UUID_A, UUID_B, UUID_C, UUID_D])],
        [_wire_record(UUID_A, UUID_B, 3), _wire_record(UUID_C, UUID_D, 3)],
    )
    baseline = FlowSubmissionBaseline(
        "project", frozenset({UUID_B}), frozenset({UUID_A}), 0, frozenset(), "0:"
    )
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(baseline, None, observation_deadline=10.0)

    assert evidence.submission_state == "GENERATION_SUCCEEDED"
    assert evidence.provider_media_id == UUID_D
    assert evidence.new_provider_media_ids == (UUID_D,)


@pytest.mark.asyncio
async def test_two_new_authoritative_records_are_multi_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _observation_port(
        [_project_state(pending=0, media_ids=[])],
        [_wire_record(UUID_C, UUID_D, 3), _wire_record(UUID_E, UUID_F, 3)],
    )
    baseline = FlowSubmissionBaseline(
        "project", frozenset({UUID_B}), frozenset({UUID_A}), 0, frozenset(), "0:"
    )
    monkeypatch.setattr(live_flow, "_monotonic", lambda: 0.0)

    evidence = await port.observe_attempt(baseline, None, observation_deadline=10.0)

    assert evidence.submission_state == "UNEXPECTED_MULTI_OUTPUT"
    assert evidence.new_provider_media_ids == tuple(sorted((UUID_D, UUID_F)))


@pytest.mark.asyncio
async def test_unfinished_workflow_and_media_keep_observation_open_to_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = _observation_port(
        [_project_state(pending=1, media_ids=[UUID_A, UUID_B])],
        [_wire_record(UUID_A, UUID_B, 2)],
    )
    now = 0.0

    def monotonic() -> float:
        return now

    async def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(live_flow, "_monotonic", monotonic)
    monkeypatch.setattr(live_flow, "_async_sleep", advance)

    evidence = await port.observe_attempt(_baseline(), None, observation_deadline=3.0)

    assert evidence.submission_state == "GENERATION_PENDING"
    assert port._page.evaluate_calls == 3  # type: ignore[attr-defined]
