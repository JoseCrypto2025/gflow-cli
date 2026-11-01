from __future__ import annotations

from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when

from tests.validation.test_upstream_canary import _make_caller, _request

scenarios(Path(__file__).with_name("upstream_first_canary_caller_v1.feature"))


@pytest.fixture
def canary_context() -> dict[str, object]:
    return {}


@given("an authorized SPV_000007 request with a fresh 10-credit quote")
def authorized_canary_request(tmp_path: Path, canary_context: dict[str, object]) -> None:
    caller, adapter, guard = _make_caller(tmp_path)
    canary_context.update(caller=caller, adapter=adapter, guard=guard, request=_request(tmp_path))


@when("the offline upstream canary caller runs twice")
def caller_runs_twice(canary_context: dict[str, object]) -> None:
    caller = canary_context["caller"]
    request = canary_context["request"]
    canary_context["first"] = caller.run(request)
    canary_context["second"] = caller.run(request)


@then("one durable invocation is retained with an exact 10-credit reservation")
def durable_invocation_is_reused(canary_context: dict[str, object]) -> None:
    first = canary_context["first"]
    second = canary_context["second"]
    adapter = canary_context["adapter"]
    guard = canary_context["guard"]
    assert first.canary_invocation_id == second.canary_invocation_id
    assert adapter.calls == 1
    row = guard.upstream_canary_row(first.canary_invocation_id)
    assert row["reserved_credits"] == "10"
    assert row["upstream_invocation_count"] == 1
