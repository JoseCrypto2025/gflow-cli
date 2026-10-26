from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when

from gflow_cli.v2_runtime.legacy_replacement import (
    ReplacementQuoteStatus,
    consume_legacy_replacement_authorization,
    evaluate_replacement_decision,
)
from gflow_cli.v2_runtime.phase_g import D2DailyCreditLedger, PhaseGError, PhaseGExecutionRequest
from tests.validation.test_legacy_replacement import (
    _approved_ledger,
    _quote,
    _replacement_request,
    _seed_parent,
)

scenarios(Path(__file__).with_name("legacy_replacement.feature"))


@pytest.fixture
def replacement_context() -> dict[str, object]:
    return {}


@given("a quarantined ambiguous legacy attempt with a 50-credit unresolved reservation")
def quarantined_legacy_parent(tmp_path: Path, replacement_context: dict[str, object]) -> None:
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    _seed_parent(ledger, tmp_path)
    replacement_context.update(ledger=ledger, before=ledger.path.read_bytes())


@given("no applicable verified credit-price row")
def no_verified_credit_row(replacement_context: dict[str, object]) -> None:
    quote = _quote()
    replacement_context["quote"] = quote
    replacement_context["decision"] = evaluate_replacement_decision(
        gate_a_pass=True,
        gate_b_pass=True,
        quote=quote,
        existing_unresolved_exposure=Decimal("50"),
        replacement_authorization_ready=True,
        parent_linkage_ready=True,
        parent_quarantined=True,
        zero_existing_replacements=True,
        zero_conflicting_authorizations=True,
        evidence_capture_ready=True,
    )


@when("the replacement decision is evaluated")
def replacement_decision_evaluated(replacement_context: dict[str, object]) -> None:
    assert replacement_context["decision"]


@then("the quote is unavailable and the decision is HOLD")
def quote_unavailable_and_hold(replacement_context: dict[str, object]) -> None:
    quote = replacement_context["quote"]
    decision = replacement_context["decision"]
    assert quote.status is ReplacementQuoteStatus.QUOTE_UNAVAILABLE
    assert decision.status == "HOLD"


@then("no authorization, replacement attempt, or reservation is created")
def no_replacement_side_effects(replacement_context: dict[str, object]) -> None:
    ledger = replacement_context["ledger"]
    before = replacement_context["before"]
    assert ledger.path.read_bytes() == before
    assert len(ledger.attempts()) == 1
    assert ledger._read()["legacy_replacement_authorizations"] == []  # noqa: SLF001


@given("an approved authorization bound to a quarantined legacy parent")
def approved_quarantined_parent(tmp_path: Path, replacement_context: dict[str, object]) -> None:
    ledger, state = _approved_ledger(tmp_path)
    replacement_context.update(ledger=ledger, **state)


@when("a controlled replacement creation consumes the authorization")
def consume_approved_authorization(replacement_context: dict[str, object], tmp_path: Path) -> None:
    ledger = replacement_context["ledger"]
    parent = replacement_context["parent"]
    quote = replacement_context["quote"]
    authorization = replacement_context["authorization"]
    assert isinstance(parent, PhaseGExecutionRequest)
    replacement_context["replacement"] = consume_legacy_replacement_authorization(
        ledger,
        authorization["authorization_id"],
        _replacement_request(tmp_path, parent),
        parent_quarantined=True,
        current_quote=quote,
    )


@then("the replacement records its parent and authorization IDs")
def replacement_lineage_is_persisted(replacement_context: dict[str, object]) -> None:
    replacement = replacement_context["replacement"]
    authorization = replacement_context["authorization"]
    lineage = replacement["legacy_replacement"]
    assert lineage["parent_attempt_id"] == authorization["parent_attempt_id"]
    assert lineage["authorization_id"] == authorization["authorization_id"]


@then("a second or different consumption fails")
@then("the authorization is consumed exactly once")
def authorization_consumed_once(replacement_context: dict[str, object], tmp_path: Path) -> None:
    ledger = replacement_context["ledger"]
    parent = replacement_context["parent"]
    quote = replacement_context["quote"]
    authorization = replacement_context["authorization"]
    replacement = replacement_context["replacement"]
    assert isinstance(parent, PhaseGExecutionRequest)
    consumed = ledger._read()["legacy_replacement_authorizations"][0]  # noqa: SLF001
    assert consumed["authorization_status"] == "CONSUMED"
    assert len(ledger.attempts()) == 2
    assert ledger.attempts()[-1] == replacement
    with pytest.raises(PhaseGError):
        consume_legacy_replacement_authorization(
            ledger,
            authorization["authorization_id"],
            _replacement_request(tmp_path, parent, "D2_test-replacement-0002"),
            parent_quarantined=True,
            current_quote=quote,
        )
