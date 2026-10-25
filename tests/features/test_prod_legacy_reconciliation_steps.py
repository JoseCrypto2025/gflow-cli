from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from pytest_bdd import given, scenarios, then, when

from gflow_cli.v2_runtime.phase_g_prod_legacy_reconciliation import (
    ProdLegacyJobDisposition,
    reconcile_legacy_prod_same_attempt,
)
from gflow_cli.v2_runtime.phase_g_prod_reconciliation import ProdReconciliationResult
from tests.validation.test_phase_g_prod_legacy_reconciliation import (
    _D2_RUN_ID,
    _OBSERVED_AT,
    _sources,
)

scenarios("prod_legacy_reconciliation.feature")


@pytest.fixture
def context() -> dict[str, Any]:
    sources = _sources()
    return {
        "sources": sources,
        "original_attempt": deepcopy(sources.d2_attempts[0]),
    }


@given("one legacy D2 attempt has unique canonical production joins")
def _unique_legacy_attempt(context: dict[str, Any]) -> None:
    pass


@given("one legacy D2 attempt has duplicate Scene Job linkage")
def _duplicate_scene_job(context: dict[str, Any]) -> None:
    sources = context["sources"]
    rows = sources.scene_jobs
    context["sources"] = sources.with_rows("scene_jobs", rows + (dict(rows[0]),))


@when("I inspect the legacy reconciliation evidence")
def _inspect_legacy_attempt(context: dict[str, Any]) -> None:
    context["decision"] = reconcile_legacy_prod_same_attempt(
        context["sources"],
        _D2_RUN_ID,
        observed_at=_OBSERVED_AT,
        software_commit="a" * 40,
    )


@then("the identity is validated with provenance")
def _identity_validated(context: dict[str, Any]) -> None:
    envelope = context["decision"].envelope
    assert envelope.identity_status == "IDENTITY_VALIDATED"
    assert envelope.field("production_job_id").sources
    assert envelope.field("production_job_id").provenance_class.value == ("DERIVED_CANONICAL_JOIN")


@then("the historical D2 row is unchanged")
def _legacy_source_unchanged(context: dict[str, Any]) -> None:
    assert context["sources"].d2_attempts[0] == context["original_attempt"]


@then("the result remains AMBIGUOUS_AFTER_RECONCILIATION")
def _result_is_ambiguous(context: dict[str, Any]) -> None:
    assert context["decision"].result is ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION


@then("the credit reservation remains unchanged")
def _reservation_is_unchanged(context: dict[str, Any]) -> None:
    assert context["decision"].audit_event["reservation_action"] == "UNCHANGED"
    assert context["decision"].envelope.field("reserved_credits").value == "50"


@then("a premature RUNNING state is reported without a Sheet write")
def _premature_running_report_only(context: dict[str, Any]) -> None:
    assert context["decision"].job_disposition is ProdLegacyJobDisposition.LEGACY_PREMATURE_RUNNING
    assert context["decision"].audit_event["mutation_performed"] is False
    assert context["sources"].production_jobs[0]["Job_Status"] == "RUNNING"
    assert context["sources"].scene_jobs[0]["Job_Status"] == "RUNNING"


@then("the identity is LEGACY_IDENTITY_AMBIGUOUS")
def _identity_is_ambiguous(context: dict[str, Any]) -> None:
    assert context["decision"].envelope.identity_status == "LEGACY_IDENTITY_AMBIGUOUS"
