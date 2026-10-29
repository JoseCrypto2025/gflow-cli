from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.operations.run_legacy_replacement as replacement_operator
from gflow_cli.v2_runtime.legacy_replacement import ReplacementQuote, ReplacementQuoteStatus
from scripts.operations.run_legacy_replacement import (
    _require_parent_job_linkage,
    is_non_subscriber_peak_window,
)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 28, 13, 59, tzinfo=UTC), False),
        (datetime(2026, 10, 28, 14, 0, tzinfo=UTC), True),
        (datetime(2026, 10, 28, 16, 59, 59, tzinfo=UTC), True),
        (datetime(2026, 10, 28, 17, 0, tzinfo=UTC), False),
        (datetime(2026, 10, 28, 21, 0, tzinfo=timezone(timedelta(hours=7))), True),
    ],
)
def test_non_subscriber_peak_window_uses_utc_edges(now: datetime, expected: bool) -> None:
    assert is_non_subscriber_peak_window(now) is expected


def test_peak_window_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="aware"):
        is_non_subscriber_peak_window(datetime(2026, 10, 28, 14))


def test_replacement_operator_accepts_both_repaired_parent_linkage_fields() -> None:
    assert _require_parent_job_linkage(
        {"production_job_id": "PRD_000001", "scene_job_id": "SJB_000001"}
    ) == ("PRD_000001", "SJB_000001")


@pytest.mark.parametrize(
    "attempt",
    [
        {"scene_job_id": "SJB_000001"},
        {"production_job_id": "PRD_000001"},
    ],
)
def test_replacement_operator_still_blocks_if_either_parent_linkage_is_missing(
    attempt: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="linkage is incomplete"):
        _require_parent_job_linkage(attempt)


def _patch_quote_operator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, now: datetime) -> None:
    monkeypatch.setenv("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", str(tmp_path / "credentials.json"))

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz: object = None) -> datetime:
            if tz is None:
                return now.replace(tzinfo=None)
            return now.astimezone(tz)  # type: ignore[arg-type]

    class Transport:
        def close(self) -> None:
            return None

    class SnapshotSource:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            return None

        def read(self) -> object:
            return object()

    tables = {
        "SCENES": [{"Scene_ID": "SCN_000001", "Dialogue_Mode": "NONE"}],
        "SCENE_PROMPT_VERSION": [
            {
                "Scene_ID": "SCN_000001",
                "Generation_Profile_ID": "GPR_0002",
                "Prompt_Status": "APPROVED",
                "Scene_Prompt_ID": "SPV_000007",
                "Prompt_SHA256": "a" * 64,
            }
        ],
        "SCENE_DIALOGUE_LINES": [],
    }
    quote = ReplacementQuote(
        status=ReplacementQuoteStatus.VALID,
        amount=Decimal("10"),
        unit="CREDIT",
        source="https://support.google.com/flow/answer/16526234?hl=en",
        source_version="PCT_000001:2026-10-28T15:09:16Z",
        input_digest="b" * 64,
        quoted_at="2026-10-29T14:00:00Z",
        valid=True,
        reason="valid",
    )
    monkeypatch.setattr(replacement_operator, "datetime", FrozenDateTime)
    monkeypatch.setattr(
        replacement_operator,
        "load_runtime_settings",
        lambda: SimpleNamespace(
            environment="production",
            storage_provider="google-drive",
            runtime_state_root=tmp_path / "runtime",
            temp_root=tmp_path / "temp",
        ),
    )
    monkeypatch.setattr(
        replacement_operator, "GoogleWorkspaceCredentialProvider", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        replacement_operator, "GoogleSheetsApiTransport", lambda *_args: Transport()
    )
    monkeypatch.setattr(
        replacement_operator, "GoogleSheetsTransactionApiTransport", lambda *_args: Transport()
    )
    monkeypatch.setattr(replacement_operator, "GoogleSheetsSnapshotSource", SnapshotSource)
    monkeypatch.setattr(replacement_operator, "_snapshot_tables", lambda _snapshot: tables)
    monkeypatch.setattr(replacement_operator, "_software_commit", lambda _root: "c" * 40)
    monkeypatch.setattr(
        replacement_operator,
        "preflight_scene",
        lambda *_args: SimpleNamespace(
            status="PREFLIGHT_PASS", ready_for_provider_adapter=True, references=()
        ),
    )
    monkeypatch.setattr(replacement_operator, "_quote", lambda *_args: quote)
    monkeypatch.setattr(
        replacement_operator,
        "_parent_credit_state",
        lambda *_args: (
            Decimal("50"),
            {"state": "UNKNOWN_SUBMISSION_STATE"},
            {"attempts": []},
        ),
    )
    monkeypatch.setattr(replacement_operator, "_read_balance", lambda _email: Decimal("50"))


def test_quote_report_marks_the_peak_window_as_advisory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_quote_operator(monkeypatch, tmp_path, datetime(2026, 10, 29, 14, 30, tzinfo=UTC))

    result = replacement_operator.run_once(
        scene_id="SCN_000001",
        profile_id="GPR_0002",
        account_id="PVA_0002",
        project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
        expected_email="singleava508@gmail.com",
        parent_attempt_id="D2_13c62112d9881710019049d95cb6da57",
        execute=False,
    )

    assert result["status"] == "QUOTE_VALID"
    assert result["peak_window_advisory"] is True


def test_execute_inside_peak_passes_time_gate_and_reaches_fresh_parent_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent_id = "D2_13c62112d9881710019049d95cb6da57"
    _patch_quote_operator(monkeypatch, tmp_path, datetime(2026, 10, 29, 14, 30, tzinfo=UTC))

    parent = {
        "d2_run_id": parent_id,
        "production_job_id": "PRD_000001",
        "scene_job_id": "SJB_000001",
        "submission_state": "UNKNOWN_SUBMISSION_STATE",
        "generate_action_count": 1,
    }

    class Ledger:
        path = tmp_path / "d2-credit-ledger.json"

        def __init__(self, _root: Path) -> None:
            return None

        def attempts(self) -> list[dict[str, object]]:
            return [parent]

    def reached_parent_evidence(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("reached fresh parent evidence check")

    monkeypatch.setattr(replacement_operator, "D2DailyCreditLedger", Ledger)
    monkeypatch.setattr(
        replacement_operator,
        "_read_json",
        lambda *_args: {"legacy_replacement_authorizations": []},
    )
    monkeypatch.setattr(
        replacement_operator,
        "_assert_parent_quarantined",
        lambda *_args: (
            {"Job_Status": "QUARANTINED"},
            {"Job_Status": "QUARANTINED", "Production_Job_ID": "PRD_000001"},
        ),
    )
    monkeypatch.setattr(replacement_operator, "_legacy_parent", reached_parent_evidence)

    with pytest.raises(RuntimeError, match="reached fresh parent evidence check"):
        replacement_operator.run_once(
            scene_id="SCN_000001",
            profile_id="GPR_0002",
            account_id="PVA_0002",
            project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
            expected_email="singleava508@gmail.com",
            parent_attempt_id=parent_id,
            execute=True,
        )


def test_consumed_authorization_rechecks_fresh_parent_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    parent_id = "D2_13c62112d9881710019049d95cb6da57"
    child_id = "D2_9c13df47d64c68de30cd036bcd1fec09"
    authorization_id = "LRA_" + "A" * 24
    _patch_quote_operator(monkeypatch, tmp_path, datetime(2026, 10, 29, 14, 30, tzinfo=UTC))

    parent = {
        "d2_run_id": parent_id,
        "production_job_id": "PRD_000001",
        "scene_job_id": "SJB_000001",
        "submission_state": "UNKNOWN_SUBMISSION_STATE",
        "generate_action_count": 1,
    }
    child = {
        "d2_run_id": child_id,
        "generate_action_count": 0,
        "legacy_replacement": {
            "authorization_id": authorization_id,
            "parent_attempt_id": parent_id,
        },
    }
    policy_id = replacement_operator.ambiguous_legacy_policy_id(parent_id)
    reconciliation_id = "d" * 64
    decision_digest = "e" * 64
    idempotency_key = replacement_operator.legacy_replacement_request_idempotency_key(
        parent_attempt_id=parent_id,
        policy_id=policy_id,
        reconciliation_audit_id=reconciliation_id,
        decision_input_digest=decision_digest,
    )
    authorization = {
        "contract_version": "D2_PROD_LEGACY_REPLACEMENT_AUTHORIZATION_V2",
        "authorization_id": authorization_id,
        "authorization_status": "CONSUMED",
        "parent_attempt_id": parent_id,
        "production_job_id": "PRD_000001",
        "scene_job_id": "SJB_000001",
        "replacement_production_job_id": "PRD_000002",
        "replacement_scene_job_id": "SJB_000002",
        "policy_id": policy_id,
        "reconciliation_audit_id": reconciliation_id,
        "decision_input_digest": decision_digest,
        "legacy_exposure": "50",
        "replacement_quote": "10",
        "replacement_quote_unit": "CREDIT",
        "replacement_quote_source": "https://support.google.com/flow/answer/16526234?hl=en",
        "replacement_quote_source_version": "PCT_000001:2026-10-28T15:09:16Z",
        "replacement_quote_input_digest": "b" * 64,
        "maximum_combined_exposure": "60",
        "authorized_replacement_count": 1,
        "generate_authorized": False,
        "replacement_created": True,
        "replacement_attempt_id": child_id,
        "replacement_request_idempotency_key": idempotency_key,
    }

    class Ledger:
        path = tmp_path / "d2-credit-ledger.json"

        def __init__(self, _root: Path) -> None:
            return None

        def attempts(self) -> list[dict[str, object]]:
            return [parent, child]

    monkeypatch.setattr(replacement_operator, "D2DailyCreditLedger", Ledger)
    monkeypatch.setattr(
        replacement_operator,
        "_read_json",
        lambda *_args: {"legacy_replacement_authorizations": [authorization]},
    )
    monkeypatch.setattr(
        replacement_operator,
        "_assert_parent_quarantined",
        lambda *_args: (
            {"Job_Status": "QUARANTINED"},
            {"Job_Status": "QUARANTINED", "Production_Job_ID": "PRD_000001"},
        ),
    )

    def changed_parent(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("fresh parent evidence check reached")

    monkeypatch.setattr(replacement_operator, "_legacy_parent", changed_parent)

    with pytest.raises(RuntimeError, match="fresh parent evidence check reached"):
        replacement_operator.run_once(
            scene_id="SCN_000001",
            profile_id="GPR_0002",
            account_id="PVA_0002",
            project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
            expected_email="singleava508@gmail.com",
            parent_attempt_id=parent_id,
            execute=True,
        )
