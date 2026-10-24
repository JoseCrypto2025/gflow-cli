from __future__ import annotations

import hashlib
import io
import tarfile
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
        "scripts/validation/v2_runtime_doctor.py",
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
