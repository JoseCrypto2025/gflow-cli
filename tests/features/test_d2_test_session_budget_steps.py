from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, scenarios, then, when

from gflow_cli.v2_runtime.google_flow import FlowPresubmitRequest, PersistedPromptVersion
from gflow_cli.v2_runtime.phase_e import PreflightResult
from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    D2TestSessionAuthorization,
    FlowSubmissionBaseline,
    PhaseGError,
    PhaseGExecutionRequest,
)
from gflow_cli.v2_runtime.phase_g_artifacts import sha256_file

scenarios("d2_test_session_budget.feature")

_BANGKOK = timezone(timedelta(hours=7))


@pytest.fixture
def context(tmp_path: Path) -> dict[str, Any]:
    root = tmp_path / "state"
    ledger = D2DailyCreditLedger(root)
    return {"root": root, "ledger": ledger}


def _request(context: dict[str, Any], run_id: str) -> PhaseGExecutionRequest:
    root = context["root"]
    prompt_text = "A red kite over the ocean."
    source_hash = "a" * 64
    prompt_hash = "b" * 64
    preflight = PreflightResult(
        "PREFLIGHT_PASS",
        True,
        "SCN_000001",
        "GPR_0002",
        resolved={
            "provider_id": "PRV_0001",
            "provider_account_id": "PVA_0001",
            "model_id": "MDL_0001",
            "generation_mode": "TEXT_TO_VIDEO",
        },
        technical_configuration={
            "generation_mode": "TEXT_TO_VIDEO",
            "duration_sec": 8,
            "aspect_ratio": "9:16",
            "output_count": 1,
            "canonical_parameters": {"DURATION_SEC": 8},
            "adapter_parameters": {"google_flow.agent.video.duration_instruction": 8},
        },
        compiled_prompt=prompt_text,
        compiled_negative_prompt="",
        source_context_sha256=source_hash,
        prompt_sha256=prompt_hash,
    )
    prompt = PersistedPromptVersion(
        "SPV_000002",
        "SCN_000001",
        "GPR_0002",
        "APPROVED",
        prompt_text,
        "",
        source_hash,
        prompt_hash,
    )
    presubmit = FlowPresubmitRequest(
        preflight,
        prompt,
        "PRV_0001",
        "PVA_0001",
        "MDL_0001",
        "TEXT_TO_VIDEO",
        "34e60953-e534-46e9-ac65-993893e61065",
        root,
        root / "temp",
    )
    return PhaseGExecutionRequest.from_presubmit(presubmit, run_id)


def _open_session(ledger: D2DailyCreditLedger, starting_balance: int = 40) -> str:
    now = datetime.now(_BANGKOK)
    expiry = datetime.combine(now.date(), time(23, 59, 59), tzinfo=_BANGKOK)
    auth = D2TestSessionAuthorization(
        session_id="D2TS_0123456789abcdef",
        provider_account_id="PVA_0001",
        provider_project_id="34e60953-e534-46e9-ac65-993893e61065",
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        runtime_state_root=ledger.root.parent,
        starting_provider_credits=Decimal(starting_balance),
        authorization_evidence_sha256="d" * 64,
        implementation_commit="a" * 40,
        expires_at=expiry.isoformat(),
    )
    expected_sha = (
        sha256_file(ledger.path) if ledger.path.exists() else hashlib.sha256(b"").hexdigest()
    )
    ledger.open_test_session(auth, expected_ledger_sha256=expected_sha)
    return auth.session_id


def _create_attempt(
    ledger: D2DailyCreditLedger, request: PhaseGExecutionRequest, session_id: str, balance: int
) -> None:
    ledger.create_test_session_attempt(
        request,
        session_id=session_id,
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_credits_before=balance,
    )


@given("a ledger with a preserved historical 50-credit reservation")
def _historical_reservation(context: dict[str, Any]) -> None:
    historical = {
        "d2_run_id": "D2_history-0001",
        "submission_state": "FORENSICALLY_CLOSED_UNKNOWN",
        "terminal_state": "FORENSICALLY_CLOSED_UNKNOWN",
        "accounted_credit_cost": "50",
        "accounting_date": datetime.now(_BANGKOK).date().isoformat(),
        "generate_action_count": 1,
    }
    context["ledger"].root.mkdir(parents=True, exist_ok=True)
    context["ledger"].path.write_text(
        json.dumps({"schema_version": 1, "daily_budget": 50, "attempts": [historical]}),
        encoding="utf-8",
    )
    context["historical"] = historical


@given("an exact TEST authorization capped at 40 new credits")
def _authorized_session(context: dict[str, Any]) -> None:
    context["session_id"] = _open_session(context["ledger"])


@when("one authorized TEST attempt is reserved")
def _reserve_one(context: dict[str, Any]) -> None:
    request = _request(context, "D2_test-session-0001")
    context["request"] = request
    context["attempt"] = _create_attempt(context["ledger"], request, context["session_id"], 40)


@then("the historical row is unchanged")
def _historical_unchanged(context: dict[str, Any]) -> None:
    payload = json.loads(context["ledger"].path.read_text(encoding="utf-8"))
    assert payload["attempts"][0] == context["historical"]
    assert payload["daily_budget"] == 50


@then("the attempt is bound to the exact TEST account and project")
def _attempt_bound(context: dict[str, Any]) -> None:
    attempt = context["ledger"].attempts()[-1]
    assert attempt["test_session_id"] == context["session_id"]
    assert attempt["provider_account_id"] == "PVA_0001"
    assert attempt["provider_project_id"] == "34e60953-e534-46e9-ac65-993893e61065"


@given("an authorized TEST session with remaining credit allowance")
@given("one authorized TEST session")
def _fresh_session(context: dict[str, Any]) -> None:
    context["session_id"] = _open_session(context["ledger"])


@when("an attempt has an unknown provider outcome")
def _unknown_outcome(context: dict[str, Any]) -> None:
    ledger = context["ledger"]
    request = _request(context, "D2_test-session-unknown")
    _create_attempt(ledger, request, context["session_id"], 40)
    ledger.capture_baseline(
        request.d2_run_id,
        FlowSubmissionBaseline(
            request.provider_project_id, frozenset(), frozenset(), 0, frozenset(), "0:"
        ),
    )
    ledger.mark_generate_action(request.d2_run_id)
    ledger.record_state(request.d2_run_id, submission_state="UNKNOWN_SUBMISSION_STATE")
    session = ledger.reconcile_test_session_attempt(
        request.d2_run_id, provider_credits_before=40, provider_credits_after=40
    )
    context["session"] = session
    context["next_request"] = _request(context, "D2_test-session-after-unknown")


@then("a second attempt is rejected before Generate")
def _second_attempt_rejected(context: dict[str, Any]) -> None:
    assert context["session"]["status"] == "STOPPED"
    with pytest.raises(PhaseGError, match="not active"):
        _create_attempt(context["ledger"], context["next_request"], context["session_id"], 40)
    assert context["ledger"].attempts()[-1]["generate_action_count"] == 1


@when("two distinct attempts are reserved concurrently")
def _concurrent_reservations(context: dict[str, Any]) -> None:
    ledger = context["ledger"]
    candidates = [
        _request(context, "D2_test-session-concurrent-1"),
        _request(context, "D2_test-session-concurrent-2"),
    ]
    context["candidate_ids"] = {request.d2_run_id for request in candidates}

    def reserve(request: PhaseGExecutionRequest) -> str:
        try:
            _create_attempt(ledger, request, context["session_id"], 40)
        except PhaseGError as exc:
            return exc.code
        return "CREATED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        context["reservation_results"] = list(pool.map(reserve, candidates))


@then("exactly one attempt is in flight")
def _one_in_flight(context: dict[str, Any]) -> None:
    results = context["reservation_results"]
    assert results.count("CREATED") == 1
    assert results.count("PHASE_G_TEST_SESSION_IN_FLIGHT") == 1
    session = json.loads(context["ledger"].path.read_text(encoding="utf-8"))["test_sessions"][0]
    assert session["in_flight_run_id"] in context["candidate_ids"]
