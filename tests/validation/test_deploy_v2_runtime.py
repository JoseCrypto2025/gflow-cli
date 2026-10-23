from __future__ import annotations

import hashlib
import io
import tarfile

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
    deploy._write_rollback_manifest(backup)

    assert deploy._verify_rollback_manifest(backup) == "a" * 40

    (backup / "unrecorded.py").write_text("unrecorded", encoding="utf-8")
    with pytest.raises(deploy.DeploymentError, match="contents"):
        deploy._verify_rollback_manifest(backup)
