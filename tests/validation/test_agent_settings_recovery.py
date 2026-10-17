from __future__ import annotations

import json
from pathlib import Path

import pytest

from gflow_cli.v2_runtime.agent_recovery import (
    AgentSettingsRecoveryJournal,
    RecoveryJournalError,
)


def _journal(root: Path) -> AgentSettingsRecoveryJournal:
    return AgentSettingsRecoveryJournal(root, "PVA_0001", "test-project-1")


def test_journal_is_dirty_before_mutation_and_can_be_marked_clean(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "runtime")
    original = {"model_id": "MDL_0001", "aspect_ratio": "16:9", "output_count": 1}
    intended = {"model_id": "MDL_0001", "aspect_ratio": "9:16", "output_count": 1}

    record = journal.begin(
        correlation_id="f5-test-1",
        original_settings=original,
        intended_settings=intended,
    )

    assert record.state == "DIRTY"
    assert journal.read() == record
    assert json.loads(journal.path.read_text(encoding="utf-8"))["state"] == "DIRTY"
    assert journal.set_state("RESTORING").state == "RESTORING"
    assert journal.set_state("CLEAN").state == "CLEAN"
    assert journal.require_clean_or_missing() is not None


def test_unclean_journal_blocks_a_second_transaction(tmp_path: Path) -> None:
    journal = _journal(tmp_path / "runtime")
    journal.begin(
        correlation_id="f5-test-1",
        original_settings={"aspect_ratio": "16:9"},
        intended_settings={"aspect_ratio": "9:16"},
    )

    with pytest.raises(RecoveryJournalError, match="blocks new work"):
        journal.begin(
            correlation_id="f5-test-2",
            original_settings={"aspect_ratio": "9:16"},
            intended_settings={"aspect_ratio": "16:9"},
        )
    with pytest.raises(RecoveryJournalError, match="recovery is required"):
        journal.require_clean_or_missing()


def test_account_level_journal_is_visible_when_project_changes(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    original = _journal(root)
    original.begin(
        correlation_id="f5-test-1",
        original_settings={"aspect_ratio": "16:9"},
        intended_settings={"aspect_ratio": "9:16"},
    )

    next_project = AgentSettingsRecoveryJournal(root, "PVA_0001", "another-project")

    assert next_project.path == original.path
    assert next_project.read() is not None
    with pytest.raises(RecoveryJournalError, match="recovery is required"):
        next_project.require_clean_or_missing()


@pytest.mark.parametrize(
    "settings",
    [
        {"api_key": "never-store"},
        {"cookie": "never-store"},
        {"token": "never-store"},
    ],
)
def test_journal_rejects_credential_shaped_settings(
    tmp_path: Path, settings: dict[str, object]
) -> None:
    with pytest.raises(RecoveryJournalError, match="non-semantic"):
        _journal(tmp_path / "runtime").begin(
            correlation_id="f5-test-1",
            original_settings=settings,
            intended_settings={},
        )


def test_journal_rejects_unsafe_roots_ids_and_symlinks(tmp_path: Path) -> None:
    with pytest.raises(RecoveryJournalError, match="absolute and non-root"):
        AgentSettingsRecoveryJournal(Path("relative"), "PVA_0001", "project")
    with pytest.raises(RecoveryJournalError, match="provider account ID"):
        AgentSettingsRecoveryJournal(tmp_path, "../outside", "project")

    journal = _journal(tmp_path / "runtime")
    journal.begin(
        correlation_id="f5-test-1",
        original_settings={"aspect_ratio": "16:9"},
        intended_settings={"aspect_ratio": "9:16"},
    )
    record = json.loads(journal.path.read_text(encoding="utf-8"))
    record["api_key"] = "never-store"
    journal.path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(RecoveryJournalError, match="unreadable or malformed"):
        journal.read()
