from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from gflow_cli.v2_runtime.google_drive_storage import GoogleDriveStorageAdapter
from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_JOB_ARTIFACTS_FOLDER_ID,
    APPROVED_SPREADSHEET_ID,
    GoogleSheetsApiTransport,
    GoogleSheetsSnapshot,
    GoogleSheetsSnapshotSource,
)
from gflow_cli.v2_runtime.google_sheets_production_jobs import GoogleSheetsProductionJobStore
from gflow_cli.v2_runtime.google_sheets_transactions import (
    GoogleSheetsTransactionApiTransport,
    OptimisticGoogleSheetsJobStore,
)
from gflow_cli.v2_runtime.phase_e import sha256_json
from gflow_cli.v2_runtime.phase_g_artifacts import validate_video
from gflow_cli.v2_runtime.production_execution import ProductionCreditGuard
from gflow_cli.v2_runtime.upstream_canary import CanaryRequest
from scripts.operations import upstream_canary_runtime as runtime


class _TokenProvider:
    def access_token(self) -> str:
        return "test-token"


class _SnapshotSource:
    def __init__(self, snapshot: GoogleSheetsSnapshot) -> None:
        self.snapshot = snapshot

    def read(self) -> GoogleSheetsSnapshot:
        return self.snapshot


def _request(tmp_path: Path) -> CanaryRequest:
    return CanaryRequest(
        script_id="SCR_000001",
        scene_id="SCN_000001",
        prompt_id="SPV_000007",
        generation_profile_id="GPR_0002",
        profile_name="gflow-prod",
        account_email="singleava508@gmail.com",
        project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
        request_idempotency_key="upstream-canary-v1:SPV_000007:preflight",
        authorization_id="CANARY_TEST_AUTH",
        authorized=True,
        ceo_credit_ceiling=Decimal("30"),
        staging_root=tmp_path / "stage",
        verification_temp_dir=tmp_path / "verify",
    )


def _reader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    prompt_text: str = "A quiet cinematic scene.",
) -> runtime.ProductionCanaryFreshReader:
    from gflow_cli.v2_runtime.legacy_replacement import ReplacementQuote, ReplacementQuoteStatus

    negative_prompt_text = ""
    prompt_sha = sha256_json({"prompt": prompt_text, "negative_prompt": negative_prompt_text})
    snapshot = GoogleSheetsSnapshot(
        spreadsheet_id="approved",
        read_at=datetime.now(UTC).isoformat(),
        source="LIVE_GOOGLE_SHEETS",
        tables={
            "SCENES": [
                {
                    "Scene_ID": "SCN_000001",
                    "Script_ID": "SCR_000001",
                    "Scene_Definition_Status": "LOCKED",
                }
            ],
            "GENERATION_PROFILE_MASTER": [
                {
                    "Generation_Profile_ID": "GPR_0002",
                    "Environment": "PROD",
                    "Status": "ACTIVE",
                    "Provider_Account_ID": "PVA_0002",
                }
            ],
            "SCENE_PROMPT_VERSION": [
                {
                    "Scene_Prompt_ID": "SPV_000007",
                    "Scene_ID": "SCN_000001",
                    "Generation_Profile_ID": "GPR_0002",
                    "Prompt_Status": "APPROVED",
                    "Compiled_Prompt_Text": prompt_text,
                    "Compiled_Negative_Prompt": negative_prompt_text,
                    "Prompt_SHA256": prompt_sha,
                }
            ],
        },
    )
    monkeypatch.setattr(runtime, "APPROVED_SPREADSHEET_ID", "approved")
    monkeypatch.setattr(
        runtime,
        "preflight_scene",
        lambda *_args: type(
            "Preflight",
            (),
            {
                "status": "PREFLIGHT_PASS",
                "ready_for_provider_adapter": True,
                "references": (),
                "technical_configuration": {
                    "duration_sec": 8,
                    "output_count": 1,
                    "aspect_ratio": "9:16",
                },
                "compiled_prompt": prompt_text,
                "compiled_negative_prompt": negative_prompt_text,
                "prompt_sha256": prompt_sha,
            },
        )(),
    )
    monkeypatch.setattr(runtime, "_validate_prod_account", lambda *_args: None)
    monkeypatch.setattr(runtime, "_validate_preclaim", lambda **_kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_validated_quote",
        lambda *_args, **_kwargs: ReplacementQuote(
            ReplacementQuoteStatus.VALID,
            Decimal("10"),
            "CREDIT",
            "PCT_000001",
            "PCT_000001:fixture",
            "a" * 64,
            datetime.now(UTC).isoformat(),
            True,
            "",
        ),
    )
    monkeypatch.setenv("GFLOW_CLI_HOME", runtime._PROD_BROWSER_STATE_ROOT.as_posix())
    monkeypatch.setattr(runtime, "_require_production_profile_home", lambda: None)
    monkeypatch.setattr(
        runtime,
        "profile_dir",
        lambda _name: Path("/var/lib/gflow/browser-state/profile_gflow-prod"),
    )
    reader = runtime.ProductionCanaryFreshReader(
        cast(GoogleSheetsSnapshotSource, _SnapshotSource(snapshot)),
        profile_credits_reader=lambda _email: Decimal("50"),
    )
    return reader


def test_production_reader_returns_fresh_bound_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _reader(monkeypatch, tmp_path)
    evidence = reader.read_fresh(_request(tmp_path))

    assert evidence.prompt_status == "APPROVED"
    assert evidence.prompt_sha256
    assert evidence.profile_home == Path("/var/lib/gflow/browser-state/profile_gflow-prod")
    assert evidence.project_policy_verified is True
    assert evidence.profile_lease_released is True
    assert evidence.quote_credits == Decimal("10")


def test_production_reader_rejects_prompt_text_sha_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _reader(monkeypatch, tmp_path, prompt_text="A changed prompt.")
    snapshot = reader._snapshot_source.snapshot  # type: ignore[attr-defined]
    snapshot.tables["SCENE_PROMPT_VERSION"][0]["Prompt_SHA256"] = "d" * 64
    with pytest.raises(runtime.ProductionSceneOperatorError, match="canonical prompt payload"):
        reader.read_fresh(_request(tmp_path))


def test_production_reader_rejects_negative_prompt_different_from_phase_e(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _reader(monkeypatch, tmp_path)
    snapshot = reader._snapshot_source.snapshot  # type: ignore[attr-defined]
    row = snapshot.tables["SCENE_PROMPT_VERSION"][0]
    row["Compiled_Negative_Prompt"] = "Avoid subtitles."
    row["Prompt_SHA256"] = sha256_json(
        {
            "prompt": str(row["Compiled_Prompt_Text"]),
            "negative_prompt": "Avoid subtitles.",
        }
    )

    with pytest.raises(runtime.ProductionSceneOperatorError, match="canonical prompt payload"):
        reader.read_fresh(_request(tmp_path))


def test_production_reader_rejects_multiple_approved_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reader = _reader(monkeypatch, tmp_path)
    snapshot = reader._snapshot_source.snapshot  # type: ignore[attr-defined]
    duplicate = dict(snapshot.tables["SCENE_PROMPT_VERSION"][0])
    duplicate["Scene_Prompt_ID"] = "SPV_000008"
    snapshot.tables["SCENE_PROMPT_VERSION"].append(duplicate)

    with pytest.raises(runtime.ProductionSceneOperatorError, match="exactly one approved prompt"):
        reader.read_fresh(_request(tmp_path))


def test_production_reader_rejects_quote_without_source_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gflow_cli.v2_runtime.legacy_replacement import ReplacementQuote, ReplacementQuoteStatus

    reader = _reader(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runtime,
        "_validated_quote",
        lambda *_args, **_kwargs: ReplacementQuote(
            ReplacementQuoteStatus.VALID,
            Decimal("10"),
            "CREDIT",
            "PCT_000001",
            None,
            "a" * 64,
            datetime.now(UTC).isoformat(),
            True,
            "",
        ),
    )

    with pytest.raises(
        runtime.ProductionSceneOperatorError, match="quote is unavailable or incomplete"
    ):
        reader.read_fresh(_request(tmp_path))


def test_existing_production_ports_construct_without_network_io(tmp_path: Path) -> None:
    token_provider = _TokenProvider()
    sheets_reader = GoogleSheetsApiTransport(token_provider)
    sheets_writer = GoogleSheetsTransactionApiTransport(token_provider)
    snapshot_source = GoogleSheetsSnapshotSource(sheets_reader, Path("schemas"))
    rows = OptimisticGoogleSheetsJobStore(sheets_writer, Path("schemas"), writer_lock_root=tmp_path)
    jobs = GoogleSheetsProductionJobStore(rows)
    guard = ProductionCreditGuard(tmp_path)
    storage = GoogleDriveStorageAdapter(
        token_provider,
        approved_folder_id=APPROVED_JOB_ARTIFACTS_FOLDER_ID,
    )
    try:
        assert snapshot_source.spreadsheet_id == APPROVED_SPREADSHEET_ID
        assert jobs is not None
        assert guard.path == tmp_path / "production-credit" / "shared-ledger.json"
        assert callable(validate_video)
        assert storage is not None
    finally:
        sheets_reader.close()
        sheets_writer.close()
        storage.close()


def test_caller_binding_uses_only_pinned_absolute_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "python"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(runtime, "UPSTREAM_PYTHON_EXECUTABLE", executable)
    caller, adapter = runtime.bind_upstream_canary_caller(
        reader=cast(runtime.FreshCanaryReader, object()),
        jobs=cast(GoogleSheetsProductionJobStore, object()),
        credit_guard=ProductionCreditGuard(tmp_path),
        storage=cast(GoogleDriveStorageAdapter, object()),
    )

    assert isinstance(caller, runtime.UpstreamCanaryProductionCaller)
    assert adapter._python == executable
    assert adapter._python.is_absolute()


def test_missing_port_fails_before_adapter_is_built(tmp_path: Path) -> None:
    with pytest.raises(runtime.ProductionSceneOperatorError, match="all production canary ports"):
        runtime.bind_upstream_canary_caller(
            reader=cast(runtime.FreshCanaryReader, None),
            jobs=cast(GoogleSheetsProductionJobStore, object()),
            credit_guard=ProductionCreditGuard(tmp_path),
            storage=cast(GoogleDriveStorageAdapter, object()),
        )


def test_binding_rejects_legacy_or_other_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pinned = tmp_path / "pinned-python"
    legacy = tmp_path / "legacy-gflow"
    pinned.write_text("", encoding="utf-8")
    legacy.write_text("", encoding="utf-8")
    monkeypatch.setattr(runtime, "UPSTREAM_PYTHON_EXECUTABLE", pinned)

    with pytest.raises(
        runtime.ProductionSceneOperatorError, match="Python executable is unavailable"
    ):
        runtime.bind_upstream_canary_caller(
            reader=cast(runtime.FreshCanaryReader, object()),
            jobs=cast(GoogleSheetsProductionJobStore, object()),
            credit_guard=ProductionCreditGuard(tmp_path),
            storage=cast(GoogleDriveStorageAdapter, object()),
            upstream_python_executable=legacy,
        )


def test_upstream_pin_uses_scoped_git_safe_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    wheel = tmp_path / "upstream.whl"
    wheel.write_bytes(b"wheel")
    cli = tmp_path / "gflow"
    cli.write_bytes(b"cli")
    python = tmp_path / "python"
    python.write_bytes(b"python")
    manifest_path = tmp_path / "runtime-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "source_repository": "https://github.com/ffroliva/gflow-cli",
                "source_commit": runtime.UPSTREAM_COMMIT,
                "package_version": runtime.UPSTREAM_VERSION,
                "source_tree_clean": True,
                "source_tree_digest": "tree-digest",
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                "cli_executable_sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
                "python_executable": str(python),
                "cli_executable": str(cli),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime, "_UPSTREAM_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(runtime, "_UPSTREAM_SOURCE_ROOT", source_root)
    monkeypatch.setattr(runtime, "_UPSTREAM_WHEEL", wheel)
    monkeypatch.setattr(runtime, "_UPSTREAM_CLI_EXECUTABLE", cli)
    monkeypatch.setattr(runtime, "UPSTREAM_PYTHON_EXECUTABLE", python)
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[0] == "git":
            if command[-1] == "HEAD":
                stdout = runtime.UPSTREAM_COMMIT
            elif command[-1] == "HEAD^{tree}":
                stdout = "tree-digest"
            else:
                stdout = ""
        elif command[-1] == "--version":
            stdout = "gflow-cli, version 0.92.1"
        else:
            stdout = (
                "PROMPT --profile --project --model --aspect --duration --count --output --json"
            )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(runtime.subprocess, "run", run)

    runtime._verify_upstream_runtime_pin()

    git_commands = [command for command in commands if command[0] == "git"]
    assert len(git_commands) == 3
    assert all(command[1:3] == ["-c", f"safe.directory={source_root}"] for command in git_commands)


def test_read_only_preflight_version_probe_does_not_inherit_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/opt/gflow-v2/src:/opt/gflow-v2-src")
    monkeypatch.setattr(runtime, "_verify_upstream_runtime_pin", lambda: None)
    reader = _reader(monkeypatch, tmp_path)
    transaction_rows = SimpleNamespace(read_rows=lambda _tab: ())
    runtime_context = cast(
        runtime.ProductionCanaryRuntime,
        SimpleNamespace(
            settings=SimpleNamespace(temp_root=tmp_path),
            reader=reader,
            transaction_rows=transaction_rows,
        ),
    )
    observed: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        return subprocess.CompletedProcess(
            command, 0, stdout="gflow-cli, version 0.92.1", stderr=""
        )

    monkeypatch.setattr(runtime.subprocess, "run", run)

    evidence = runtime.verify_zero_generate_preflight(runtime_context, _request(tmp_path))

    assert evidence.prompt_id == "SPV_000007"
    assert "PYTHONPATH" not in cast(dict[str, str], observed["env"])
