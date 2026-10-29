from __future__ import annotations

import hashlib
import io
import tarfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.operations import deploy_v2_runtime as deploy


def _archive(entries: list[tuple[str, bytes, str]]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, content, kind in entries:
            info = tarfile.TarInfo(name)
            info.mode = 0o644
            info.size = len(content)
            if kind == "file":
                archive.addfile(info, io.BytesIO(content))
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = "../../outside"
                archive.addfile(info)
    return stream.getvalue()


def _required_entries() -> list[tuple[str, bytes, str]]:
    paths = (
        "src/gflow_cli/v2_runtime/phase_e.py",
        "src/gflow_cli/v2_runtime/phase_g.py",
        "src/gflow_cli/v2_runtime/phase_g_run.py",
        "src/gflow_cli/v2_runtime/phase_g_live_flow.py",
        "src/gflow_cli/v2_runtime/google_sheets.py",
        "schemas/master/PROVIDER_ACCOUNT_MASTER.schema.json",
        "scripts/operations/vps_flow_presubmit.py",
        "scripts/operations/run_production_scene.py",
        "scripts/operations/reconcile_legacy_prod_attempt.py",
        "scripts/operations/apply_ambiguous_legacy_recovery_policy.py",
        "scripts/operations/inspect_flow_pricing_read_only.py",
        "scripts/operations/run_legacy_replacement.py",
        "scripts/operations/repair_legacy_parent_job_linkage.py",
        "scripts/validation/v2_runtime_doctor.py",
        "scripts/validation/validate_v2_data_architecture.py",
        "pyproject.toml",
        "uv.lock",
    )
    return [(path, b"x", "file") for path in paths]


def test_archive_plan_hashes_only_allowlisted_runtime_files() -> None:
    content = b"runtime source"
    data = _archive(
        [
            *_required_entries(),
            ("src/gflow_cli/__init__.py", content, "file"),
            ("schemas/example.json", b"{}", "file"),
        ]
    )

    plan = deploy.inspect_archive(data, "a" * 40)

    assert plan.total_bytes == len(_required_entries()) + len(content) + 2
    assert plan.file_hashes["src/gflow_cli/__init__.py"] == hashlib.sha256(content).hexdigest()
    assert plan.file_hashes["schemas/example.json"] == hashlib.sha256(b"{}").hexdigest()
    assert "scripts/validation/validate_v2_data_architecture.py" in plan.file_hashes
    assert "scripts/operations/reconcile_legacy_prod_attempt.py" in plan.file_hashes
    assert "scripts/operations/apply_ambiguous_legacy_recovery_policy.py" in plan.file_hashes
    assert "scripts/operations/inspect_flow_pricing_read_only.py" in plan.file_hashes
    assert "scripts/operations/run_legacy_replacement.py" in plan.file_hashes


@pytest.mark.parametrize(
    "name",
    ["../escape", "/absolute", "tests/test_should_not_deploy.py", ".env"],
)
def test_archive_plan_rejects_unsafe_or_unapproved_paths(name: str) -> None:
    with pytest.raises(deploy.DeploymentError):
        deploy.inspect_archive(_archive([(name, b"x", "file")]), "a" * 40)


def test_archive_plan_rejects_symbolic_links() -> None:
    data = _archive([*_required_entries(), ("src/gflow_cli/escape", b"", "symlink")])

    with pytest.raises(deploy.DeploymentError, match="link"):
        deploy.inspect_archive(data, "a" * 40)


def test_runtime_dependency_sync_uses_the_staged_venv_and_locked_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage = tmp_path / "stage"
    staged_python = stage / ".venv" / "bin" / "python"
    staged_python.parent.mkdir(parents=True)
    staged_python.write_text("staged interpreter", encoding="utf-8")
    commands: list[list[str]] = []

    monkeypatch.setattr(
        deploy.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    monkeypatch.setattr(deploy, "_run", lambda command, cwd=None: commands.append(command) or "")

    deploy._sync_runtime_dependencies(stage)

    assert commands == [
        [
            "/usr/bin/uv",
            "sync",
            "--frozen",
            "--no-dev",
            "--no-build",
            "--no-install-project",
            "--project",
            str(stage),
            "--python",
            str(staged_python),
        ]
    ]


def test_production_media_import_dependencies_are_not_dev_only() -> None:
    project_file = Path(__file__).resolve().parents[2] / "pyproject.toml"
    project = tomllib.loads(project_file.read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]

    assert any(dependency.startswith("av>=") for dependency in dependencies)
    assert any(dependency.startswith("numpy>=") for dependency in dependencies)


def test_manifest_verification_detects_post_deploy_file_change(tmp_path) -> None:
    expected = hashlib.sha256(b"expected").hexdigest()
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_bytes(b"changed")
    manifest = {"files": {"src/module.py": expected}}

    with pytest.raises(deploy.DeploymentError, match="parity"):
        deploy.verify_deployed_files(tmp_path, manifest)


def test_rollback_manifest_rejects_unrecorded_files(tmp_path) -> None:
    backup = tmp_path / "backup"
    backup.mkdir()
    (backup / ".gflow-v2-build.json").write_text(
        '{"commit_sha":"' + "a" * 40 + '"}\n', encoding="utf-8"
    )
    (backup / "runtime.py").write_text("original", encoding="utf-8")
    manifest_path, manifest_sha = deploy._write_rollback_manifest(backup)

    assert deploy._sha256_file(manifest_path) == manifest_sha
    assert deploy._verify_rollback_manifest(backup) == "a" * 40

    (backup / "unrecorded.py").write_text("unrecorded", encoding="utf-8")
    with pytest.raises(deploy.DeploymentError, match="contents"):
        deploy._verify_rollback_manifest(backup)


def test_promotion_restores_runtime_if_rollback_backup_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "runtime"
    venv = target / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("existing interpreter", encoding="utf-8")
    (target / ".gflow-v2-build.json").write_text(
        '{"commit_sha":"' + "b" * 40 + '"}\n', encoding="utf-8"
    )
    backups = tmp_path / "backups"
    backups.mkdir()

    monkeypatch.setattr(deploy, "TARGET_ROOT", target)
    monkeypatch.setattr(deploy, "BACKUP_ROOT", backups)
    monkeypatch.setattr(deploy.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(deploy, "_check_no_runtime_processes", lambda: None)
    monkeypatch.setattr(deploy, "_sync_runtime_dependencies", lambda stage: None)
    real_mkdtemp = deploy.tempfile.mkdtemp
    monkeypatch.setattr(
        deploy.tempfile,
        "mkdtemp",
        lambda *, prefix, dir: real_mkdtemp(prefix=prefix, dir=tmp_path),
    )
    real_stat = Path.stat

    def deployment_root_stat(path: Path, *args: object, **kwargs: object) -> object:
        info = real_stat(path, *args, **kwargs)
        if path in {target, backups}:
            return SimpleNamespace(
                st_dev=1,
                st_uid=0,
                st_gid=0,
                st_mode=0o40755,
            )
        return info

    monkeypatch.setattr(Path, "stat", deployment_root_stat)
    monkeypatch.setattr(
        deploy,
        "_extract_archive",
        lambda data, plan, destination, commit: (destination / "candidate.py").write_text(
            "candidate", encoding="utf-8"
        ),
    )
    verification_calls = 0
    runtime_check_calls = 0

    def reject_backup(backup: Path) -> str:
        nonlocal verification_calls
        verification_calls += 1
        return "c" * 40

    def runtime_checks(commit: str) -> str:
        nonlocal runtime_check_calls
        runtime_check_calls += 1
        return "PASS"

    monkeypatch.setattr(deploy, "_verify_rollback_manifest", reject_backup)
    monkeypatch.setattr(deploy, "_run_runtime_checks", runtime_checks)

    with pytest.raises(deploy.DeploymentError, match="rollback backup is not ready"):
        deploy._promote(
            b"archive",
            deploy.ArchivePlan("c" * 64, {}, {}, 0),
            "a" * 40,
            "b" * 40,
            venv,
        )

    assert verification_calls == 1
    assert runtime_check_calls == 0
    assert target.is_dir()
    assert (target / ".gflow-v2-build.json").read_text(encoding="utf-8") == (
        '{"commit_sha":"' + "b" * 40 + '"}\n'
    )
    assert list(backups.iterdir()) == []
