from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

import pytest
from pytest_bdd import given, scenarios, then, when

from gflow_cli.v2_runtime.phase_g import D2DailyCreditLedger
from gflow_cli.v2_runtime.phase_g_prod_legacy_job_linkage import (
    LegacyJobLinkageProofError,
    build_legacy_job_linkage_repair_proof,
)
from gflow_cli.v2_runtime.phase_g_prod_legacy_reconciliation import (
    LegacyProdAttemptSources,
    LegacySourceSnapshot,
)
from tests.validation.test_phase_g_prod_legacy_reconciliation import _linkage_proof_fixture

scenarios(Path(__file__).with_name("legacy_job_linkage_repair.feature"))


@pytest.fixture
def repair_context() -> dict[str, object]:
    return {}


@given("a legacy attempt with a unique canonical linkage proof")
def unique_linkage_proof(tmp_path: Path, repair_context: dict[str, object]) -> None:
    sources, policy_event, reconciliation_event = _linkage_proof_fixture(
        tmp_path / "proof", repair_target=True
    )
    _prepare_context(
        tmp_path,
        repair_context,
        sources,
        policy_event,
        reconciliation_event,
    )


@given("a legacy attempt with duplicate canonical scene-job candidates")
def ambiguous_linkage_proof(tmp_path: Path, repair_context: dict[str, object]) -> None:
    sources, policy_event, reconciliation_event = _linkage_proof_fixture(
        tmp_path / "proof", repair_target=True
    )
    sources = sources.with_rows("scene_jobs", (*sources.scene_jobs, dict(sources.scene_jobs[0])))
    _prepare_context(
        tmp_path,
        repair_context,
        sources,
        policy_event,
        reconciliation_event,
    )


def _prepare_context(
    tmp_path: Path,
    context: dict[str, object],
    sources: LegacyProdAttemptSources,
    policy_event: dict[str, object],
    reconciliation_event: dict[str, object],
) -> None:
    attempt = dict(sources.d2_attempts[0])
    ledger = D2DailyCreditLedger(tmp_path / "runtime")
    ledger.root.mkdir(parents=True, exist_ok=True)
    ledger.path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "daily_budget": 50,
                "attempts": [attempt],
                "test_sessions": [],
                "legacy_replacement_authorizations": [],
            }
        ),
        encoding="utf-8",
    )
    snapshots = dict(sources.source_snapshots)
    prior_d2_snapshot = snapshots["D2_LEDGER"]
    snapshots["D2_LEDGER"] = LegacySourceSnapshot(
        source="D2_LEDGER",
        sha256=hashlib.sha256(ledger.path.read_bytes()).hexdigest(),
        observed_at=prior_d2_snapshot.observed_at,
    )
    sources = sources.with_values(source_snapshots=snapshots)
    context.update(
        sources=sources,
        policy_event=policy_event,
        reconciliation_event=reconciliation_event,
        attempt=attempt,
        before=ledger.path.read_bytes(),
        ledger=ledger,
    )


@when("the proof-bound repair is applied")
def apply_proof_bound_repair(repair_context: dict[str, object]) -> None:
    sources = cast(LegacyProdAttemptSources, repair_context["sources"])
    proof = _build_proof(repair_context, sources)
    ledger = cast(D2DailyCreditLedger, repair_context["ledger"])
    repair_context["proof"] = proof
    repair_context["result"] = ledger.repair_legacy_job_linkage(proof)


@when("the same proof-bound repair is replayed")
def replay_proof_bound_repair(repair_context: dict[str, object]) -> None:
    ledger = cast(D2DailyCreditLedger, repair_context["ledger"])
    result = cast(dict[str, object], repair_context["result"])
    proof = cast(dict[str, object], repair_context["proof"])
    repair_context["replay_result"] = ledger.repair_legacy_job_linkage(proof)
    repair_context["first_result"] = result


@when("the proof-bound repair is attempted")
def attempt_ambiguous_repair(repair_context: dict[str, object]) -> None:
    sources = cast(LegacyProdAttemptSources, repair_context["sources"])
    with pytest.raises(LegacyJobLinkageProofError) as error:
        _build_proof(repair_context, sources)
    repair_context["proof_error"] = error.value


def _build_proof(
    context: dict[str, object], sources: LegacyProdAttemptSources
) -> dict[str, object]:
    return build_legacy_job_linkage_repair_proof(
        sources,
        attempt_id=str(sources.d2_attempts[0]["d2_run_id"]),
        production_job_id=str(sources.scene_jobs[0]["Production_Job_ID"]),
        scene_job_id=str(sources.scene_jobs[0]["Scene_Job_ID"]),
        policy_event=cast(dict[str, object], context["policy_event"]),
        reconciliation_event=cast(dict[str, object], context["reconciliation_event"]),
        verified_at="2026-10-29T12:30:00Z",
        software_commit="c" * 40,
    )


@then("only the two parent job IDs are added with a durable audit")
def only_linkage_and_audit_are_added(repair_context: dict[str, object]) -> None:
    ledger = cast(D2DailyCreditLedger, repair_context["ledger"])
    saved = ledger._read()  # noqa: SLF001
    attempts = cast(list[dict[str, object]], saved["attempts"])
    original = cast(dict[str, object], repair_context["attempt"])
    assert attempts == [
        {
            **original,
            "production_job_id": "PRD_000001",
            "scene_job_id": "SJB_000001",
        }
    ]
    assert len(cast(list[object], saved["legacy_job_linkage_repairs"])) == 1


@then("the reservation, state, Generate count, and provider evidence stay unchanged")
def historical_evidence_stays_unchanged(repair_context: dict[str, object]) -> None:
    ledger = cast(D2DailyCreditLedger, repair_context["ledger"])
    saved = ledger._read()  # noqa: SLF001
    attempt = cast(list[dict[str, object]], saved["attempts"])[0]
    original = cast(dict[str, object], repair_context["attempt"])
    for field in (
        "submission_state",
        "accounted_credit_cost",
        "generate_action_count",
        "provider_submit_request_count",
        "provider_execution_id",
        "provider_media_id",
        "provider_media_ids",
        "confirmation_id",
    ):
        assert attempt.get(field) == original.get(field)


@then("the original linkage audit is returned without a duplicate event")
def replay_returns_existing_audit(repair_context: dict[str, object]) -> None:
    assert repair_context["first_result"] == repair_context["replay_result"]
    ledger = cast(D2DailyCreditLedger, repair_context["ledger"])
    saved = ledger._read()  # noqa: SLF001
    assert len(cast(list[object], saved["legacy_job_linkage_repairs"])) == 1


@then("the proof is rejected and the ledger remains unchanged")
def ambiguous_repair_is_non_mutating(repair_context: dict[str, object]) -> None:
    error = cast(LegacyJobLinkageProofError, repair_context["proof_error"])
    assert error.code == "AMBIGUOUS"
    ledger = cast(D2DailyCreditLedger, repair_context["ledger"])
    assert ledger.path.read_bytes() == repair_context["before"]
