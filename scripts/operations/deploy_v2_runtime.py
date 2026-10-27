"""Deploy the approved V2 runtime tree on its dedicated Linux VPS."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

SOURCE_ROOT = Path("/opt/gflow-v2-src")
TARGET_ROOT = Path("/opt/gflow-v2")
BACKUP_ROOT = Path("/opt/gflow-v2-backups")
PROTECTED_ROOTS = (
    Path("/var/lib/gflow"),
    Path("/var/log/gflow"),
    Path("/var/tmp/gflow"),
    Path("/etc/gflow-v2"),
    Path("/var/lib/gflow/browser-state/profile_gflow-prod"),
)
IN_USE_PREFIXES = (
    str(TARGET_ROOT) + "/",
    "/var/lib/gflow/browser-state/profile_gflow-prod",
)
ARCHIVE_PATHS = (
    "src",
    "schemas",
    "scripts/operations/vps_flow_presubmit.py",
    "scripts/operations/run_production_scene.py",
    "scripts/operations/reconcile_legacy_prod_attempt.py",
    "scripts/operations/apply_ambiguous_legacy_recovery_policy.py",
    "scripts/operations/inspect_flow_pricing_read_only.py",
    "scripts/validation/v2_runtime_doctor.py",
    "scripts/validation/validate_v2_data_architecture.py",
    "pyproject.toml",
    "uv.lock",
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MANIFEST_NAME = "deployment-manifest.json"


class DeploymentError(RuntimeError):
    """A safe, bounded deployment precondition or verification failure."""


@dataclass(frozen=True, slots=True)
class ArchivePlan:
    archive_sha256: str
    file_hashes: dict[str, str]
    file_modes: dict[str, int]
    total_bytes: int


def _allowed_archive_path(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        return False
    value = path.as_posix().rstrip("/")
    return any(
        value == allowed
        or allowed.startswith(f"{value}/")
        or (allowed in {"src", "schemas"} and value.startswith(f"{allowed}/"))
        for allowed in ARCHIVE_PATHS
    )


def inspect_archive(data: bytes, expected_sha: str) -> ArchivePlan:
    """Validate the exact allowlisted Git archive without extracting it."""
    if not _SHA_RE.fullmatch(expected_sha):
        raise DeploymentError("expected source commit SHA is invalid")
    file_hashes: dict[str, str] = {}
    file_modes: dict[str, int] = {}
    total_bytes = 0
    try:
        archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:")
    except tarfile.TarError as exc:
        raise DeploymentError("source archive is unreadable") from exc
    with archive:
        for member in archive.getmembers():
            if not _allowed_archive_path(member.name):
                raise DeploymentError("source archive contains an unapproved path")
            if member.isdir():
                continue
            if not member.isfile():
                raise DeploymentError("source archive contains a link or special file")
            stream = archive.extractfile(member)
            if stream is None:
                raise DeploymentError("source archive contains an unreadable file")
            digest = hashlib.sha256()
            size = 0
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            if size != member.size:
                raise DeploymentError("source archive file size does not match its header")
            name = PurePosixPath(member.name).as_posix()
            if name in file_hashes:
                raise DeploymentError("source archive contains a duplicate file path")
            file_hashes[name] = digest.hexdigest()
            file_modes[name] = stat.S_IMODE(member.mode)
            total_bytes += size
    required = {
        "src/gflow_cli/v2_runtime/phase_e.py",
        "src/gflow_cli/v2_runtime/phase_g.py",
        "src/gflow_cli/v2_runtime/phase_g_run.py",
        "src/gflow_cli/v2_runtime/phase_g_live_flow.py",
        "src/gflow_cli/v2_runtime/google_sheets.py",
        "pyproject.toml",
        "uv.lock",
        "schemas/master/PROVIDER_ACCOUNT_MASTER.schema.json",
        "scripts/operations/vps_flow_presubmit.py",
        "scripts/operations/run_production_scene.py",
        "scripts/operations/reconcile_legacy_prod_attempt.py",
        "scripts/operations/apply_ambiguous_legacy_recovery_policy.py",
        "scripts/operations/inspect_flow_pricing_read_only.py",
        "scripts/validation/v2_runtime_doctor.py",
        "scripts/validation/validate_v2_data_architecture.py",
    }
    if not required.issubset(file_hashes):
        raise DeploymentError("source archive is missing a required runtime file")
    return ArchivePlan(hashlib.sha256(data).hexdigest(), file_hashes, file_modes, total_bytes)


def verify_deployed_files(root: Path, manifest: dict[str, Any]) -> None:
    """Require every manifest-listed application file to retain its source hash."""
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise DeploymentError("deployment manifest has no source files")
    for name, expected in files.items():
        if not isinstance(name, str) or not _allowed_archive_path(name):
            raise DeploymentError("deployment manifest contains an unsafe file path")
        path = root.joinpath(*PurePosixPath(name).parts)
        if path.is_symlink() or not path.is_file():
            raise DeploymentError("deployed file parity check failed")
        if _sha256_file(path) != expected:
            raise DeploymentError("deployed file parity check failed")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentError(f"required command failed: {type(exc).__name__}") from exc
    if result.returncode:
        raise DeploymentError(
            f"required command failed with exit {result.returncode}: {Path(command[0]).name}"
        )
    return result.stdout.strip()


def _git(source: Path, *args: str) -> str:
    return _run(["git", "-c", f"safe.directory={source}", "-C", str(source), *args], cwd=source)


def _source_archive(source: Path, commit: str) -> bytes:
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                f"safe.directory={source}",
                "-C",
                str(source),
                "archive",
                "--format=tar",
                commit,
                *ARCHIVE_PATHS,
            ],
            capture_output=True,
            check=False,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeploymentError(f"source archive failed: {type(exc).__name__}") from exc
    if result.returncode:
        raise DeploymentError("source archive command failed")
    return result.stdout


def _check_source(source: Path, commit: str) -> bytes:
    if source != SOURCE_ROOT or source.is_symlink() or not source.is_dir():
        raise DeploymentError("source root is not the approved checkout")
    if _git(source, "rev-parse", "--show-toplevel") != str(source):
        raise DeploymentError("source root is not a Git worktree root")
    if _git(source, "rev-parse", "HEAD") != commit:
        raise DeploymentError("source HEAD differs from expected commit")
    if _git(source, "branch", "--show-current") != "automation-v2":
        raise DeploymentError("source branch is not automation-v2")
    if _git(source, "status", "--porcelain"):
        raise DeploymentError("source worktree is not clean")
    return _source_archive(source, commit)


def _check_runtime() -> tuple[str, Path]:
    if TARGET_ROOT.is_symlink() or not TARGET_ROOT.is_dir():
        raise DeploymentError("runtime target is not a real directory")
    target_info = TARGET_ROOT.stat()
    if (
        target_info.st_uid != 0
        or target_info.st_gid != 0
        or stat.S_IMODE(target_info.st_mode) != 0o755
    ):
        raise DeploymentError("runtime target ownership or mode differs from root:root 755")
    venv = TARGET_ROOT / ".venv"
    if venv.is_symlink() or not venv.is_dir() or not (venv / "bin/python").is_file():
        raise DeploymentError("existing runtime virtual environment is unavailable")
    manifest_path = TARGET_ROOT / ".gflow-v2-build.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        old_commit = manifest["commit_sha"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise DeploymentError("current runtime build manifest is invalid") from exc
    if not isinstance(old_commit, str) or not _SHA_RE.fullmatch(old_commit):
        raise DeploymentError("current runtime build manifest has no valid commit")
    return old_commit, venv


def _check_protected_roots() -> None:
    target = TARGET_ROOT.resolve(strict=True)
    for protected in PROTECTED_ROOTS:
        resolved = protected.resolve(strict=False)
        if target == resolved or target in resolved.parents or resolved in target.parents:
            raise DeploymentError("runtime target overlaps a protected state root")


def _check_deployment_roots() -> None:
    opt_root = Path("/opt")
    info = opt_root.stat()
    if (
        opt_root.is_symlink()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != 0o755
    ):
        raise DeploymentError("/opt ownership or mode differs from root:root 755")


def _check_no_runtime_processes() -> None:
    own_pid = os.getpid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == own_pid:
            continue
        try:
            command = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", errors="ignore")
            )
            cwd = os.readlink(entry / "cwd")
        except (OSError, PermissionError):
            continue
        if (
            any(prefix in command for prefix in IN_USE_PREFIXES)
            or cwd == str(TARGET_ROOT)
            or cwd.startswith(IN_USE_PREFIXES[0])
        ):
            raise DeploymentError("an application process is using the runtime tree")


def _sync_runtime_dependencies(stage: Path) -> None:
    """Install locked production dependencies into the staged copy of the runtime venv."""
    uv = shutil.which("uv")
    staged_python = stage / ".venv" / "bin" / "python"
    if uv is None or not staged_python.is_file():
        raise DeploymentError("uv or the staged runtime Python is unavailable")
    _run(
        [
            uv,
            "sync",
            "--frozen",
            "--no-dev",
            "--no-build",
            "--no-install-project",
            "--project",
            str(stage),
            "--python",
            str(staged_python),
        ],
        cwd=stage,
    )


@contextmanager
def _deployment_lock() -> Any:
    """Serialize apply/rollback operations without touching application state."""
    import fcntl

    lock_path = Path("/run/lock/gflow-v2-deploy.lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise DeploymentError("another deployment operation holds the lock") from exc
        yield
    finally:
        os.close(descriptor)


def _extract_archive(data: bytes, plan: ArchivePlan, destination: Path, commit: str) -> None:
    destination.chmod(0o755)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            path = destination.joinpath(*relative.parts)
            if member.isdir():
                path.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise DeploymentError("source archive file cannot be extracted")
            with source, path.open("xb") as output:
                shutil.copyfileobj(source, output)
            path.chmod(plan.file_modes[relative.as_posix()])
    manifest = {
        "schema_version": 1,
        "commit_sha": commit,
        "archive_sha256": plan.archive_sha256,
        "files": dict(sorted(plan.file_hashes.items())),
    }
    (destination / ".gflow-v2-build.json").write_text(
        json.dumps({"commit_sha": commit}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (destination / _MANIFEST_NAME).write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    verify_deployed_files(destination, manifest)


def _tree_manifest(root: Path) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        entry: dict[str, object] = {
            "path": relative,
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "size": info.st_size,
            "kind": "symlink" if path.is_symlink() else "directory" if path.is_dir() else "file",
        }
        if stat.S_ISLNK(info.st_mode):
            entry["target"] = os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            entry["sha256"] = _sha256_file(path)
        entries.append(entry)
    return {"root": str(root), "entries": entries}


def _write_rollback_manifest(backup: Path) -> tuple[Path, str]:
    manifest = _tree_manifest(backup)
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    target = backup.with_name(backup.name + ".manifest.json")
    target.write_bytes(payload.encode("utf-8"))
    return target, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _verify_rollback_manifest(backup: Path) -> str:
    """Verify a saved full-tree rollback copy before restoring it."""
    manifest_path = backup.with_name(backup.name + ".manifest.json")
    if backup.is_symlink() or not backup.is_dir() or manifest_path.is_symlink():
        raise DeploymentError("rollback backup or manifest is unavailable")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        entries = manifest["entries"]
        commit = json.loads((backup / ".gflow-v2-build.json").read_text(encoding="utf-8"))[
            "commit_sha"
        ]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise DeploymentError("rollback backup manifest is invalid") from exc
    if (
        not isinstance(entries, list)
        or not isinstance(commit, str)
        or not _SHA_RE.fullmatch(commit)
    ):
        raise DeploymentError("rollback backup manifest is invalid")
    expected_paths: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise DeploymentError("rollback backup manifest is invalid")
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise DeploymentError("rollback backup manifest has an unsafe path")
        name = relative.as_posix()
        if name in expected_paths:
            raise DeploymentError("rollback backup manifest has duplicate paths")
        expected_paths.add(name)
        for parent in relative.parents:
            if parent == PurePosixPath("."):
                continue
            ancestor = backup.joinpath(*parent.parts)
            try:
                ancestor_info = ancestor.lstat()
            except OSError as exc:
                raise DeploymentError("rollback backup contents do not match its manifest") from exc
            if not stat.S_ISDIR(ancestor_info.st_mode):
                raise DeploymentError("rollback backup contains an unsafe parent path")
        path = backup.joinpath(*relative.parts)
        try:
            info = path.lstat()
        except OSError as exc:
            raise DeploymentError("rollback backup contents do not match its manifest") from exc
        kind = entry.get("kind")
        if (
            info.st_uid != entry.get("uid")
            or info.st_gid != entry.get("gid")
            or stat.S_IMODE(info.st_mode) != entry.get("mode")
            or info.st_size != entry.get("size")
        ):
            raise DeploymentError("rollback backup metadata does not match its manifest")
        if kind == "file":
            if not stat.S_ISREG(info.st_mode) or _sha256_file(path) != entry.get("sha256"):
                raise DeploymentError("rollback backup contents do not match its manifest")
        elif kind == "directory":
            if not stat.S_ISDIR(info.st_mode):
                raise DeploymentError("rollback backup contents do not match its manifest")
        elif kind == "symlink":
            if not stat.S_ISLNK(info.st_mode) or os.readlink(path) != entry.get("target"):
                raise DeploymentError("rollback backup contents do not match its manifest")
        else:
            raise DeploymentError("rollback backup manifest has an unknown entry type")
    actual_paths = {path.relative_to(backup).as_posix() for path in backup.rglob("*")}
    if actual_paths != expected_paths:
        raise DeploymentError("rollback backup contents do not match its manifest")
    return commit


def _rollback(backup: Path) -> dict[str, str]:
    if os.geteuid() != 0:
        raise DeploymentError("rollback requires root on the VPS")
    if backup.is_symlink() or backup.parent.resolve(strict=True) != BACKUP_ROOT.resolve(
        strict=True
    ):
        raise DeploymentError("rollback path is outside the approved backup root")
    _check_runtime()
    _check_no_runtime_processes()
    commit = _verify_rollback_manifest(backup)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    current = BACKUP_ROOT / f"gflow-v2-before-rollback-{stamp}"
    if current.exists():
        raise DeploymentError("rollback holding path already exists")
    TARGET_ROOT.rename(current)
    try:
        backup.rename(TARGET_ROOT)
        _run_runtime_checks(commit)
    except BaseException:
        if TARGET_ROOT.exists():
            TARGET_ROOT.rename(backup)
        current.rename(TARGET_ROOT)
        raise
    return {"restored_commit": commit, "pre_rollback_runtime": str(current)}


def _run_runtime_checks(commit: str) -> str:
    runner = shutil.which("runuser")
    if runner is None:
        raise DeploymentError("runuser is unavailable for service-user checks")
    env = {
        "HOME": "/home/gflow",
        "PATH": "/home/gflow/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "PYTHONPATH": f"{TARGET_ROOT / 'src'}:{TARGET_ROOT}",
        "RUNTIME_ENV": "production",
        "STORAGE_PROVIDER": "google-drive",
        "RUNTIME_STATE_ROOT": "/var/lib/gflow",
        "TEMP_ROOT": "/var/tmp/gflow",
        "LOG_ROOT": "/var/log/gflow",
        "GFLOW_APP_ROOT": str(TARGET_ROOT),
        "GFLOW_EXPECTED_APP_SHA": commit,
        "GFLOW_SERVICE_USER": "gflow",
    }
    doctor = [
        runner,
        "-u",
        "gflow",
        "--",
        "/usr/bin/env",
        *[f"{key}={value}" for key, value in env.items()],
        str(TARGET_ROOT / ".venv/bin/python"),
        str(TARGET_ROOT / "scripts/validation/v2_runtime_doctor.py"),
    ]
    doctor_output = _run(doctor)
    try:
        report = json.loads(doctor_output)
    except json.JSONDecodeError as exc:
        raise DeploymentError("runtime doctor did not return JSON") from exc
    if report.get("status") != "PASS":
        raise DeploymentError("runtime doctor reported a failure")
    smoke = "\n".join(
        (
            "import os",
            "import pwd",
            "from pathlib import Path",
            "from gflow_cli.v2_runtime import phase_e, phase_g, phase_g_run",
            "from gflow_cli.v2_runtime import production_orchestrator",
            "from scripts.operations import vps_flow_presubmit",
            "service_uid = pwd.getpwnam('gflow').pw_uid",
            "credential = Path('/etc/gflow-v2/google-authorized-user.json')",
            "credential_stat = credential.stat()",
            "assert credential.is_file() and not credential.is_symlink()",
            "assert os.access(credential, os.R_OK)",
            "assert credential_stat.st_uid == service_uid",
            "assert credential_stat.st_mode & 0o777 == 0o600",
            "profile = Path('/var/lib/gflow/browser-state/profile_gflow-prod')",
            "profile_stat = profile.stat()",
            "assert profile.is_dir() and not profile.is_symlink()",
            "assert profile_stat.st_uid == service_uid",
            "assert profile_stat.st_mode & 0o777 == 0o700",
        )
    )
    import_check = [
        runner,
        "-u",
        "gflow",
        "--",
        "/usr/bin/env",
        f"HOME={env['HOME']}",
        f"PATH={env['PATH']}",
        f"PYTHONPATH={env['PYTHONPATH']}",
        str(TARGET_ROOT / ".venv/bin/python"),
        "-c",
        smoke,
    ]
    _run(import_check)
    return doctor_output


def _promote(
    data: bytes, plan: ArchivePlan, commit: str, old_commit: str, venv: Path
) -> dict[str, str]:
    if os.geteuid() != 0:
        raise DeploymentError("apply requires root on the VPS")
    _check_no_runtime_processes()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_ROOT / f"gflow-v2-{stamp}-{old_commit[:12]}"
    stage = Path(tempfile.mkdtemp(prefix=".gflow-v2-stage-", dir="/opt"))
    backup_created = False
    deployed = False
    backup_manifest: Path | None = None
    try:
        _extract_archive(data, plan, stage, commit)
        shutil.copytree(venv, stage / ".venv", symlinks=True, copy_function=shutil.copy2)
        _sync_runtime_dependencies(stage)
        if not BACKUP_ROOT.exists():
            BACKUP_ROOT.mkdir(mode=0o750)
        if BACKUP_ROOT.is_symlink() or not BACKUP_ROOT.is_dir():
            raise DeploymentError("rollback root is not a real directory")
        backup_info = BACKUP_ROOT.stat()
        if backup_info.st_uid != 0 or backup_info.st_gid != 0 or backup_info.st_mode & 0o022:
            raise DeploymentError("rollback root ownership or permissions are unsafe")
        if TARGET_ROOT.stat().st_dev != BACKUP_ROOT.stat().st_dev:
            raise DeploymentError("runtime and rollback roots are not on one filesystem")
        free_bytes = shutil.disk_usage(TARGET_ROOT.parent).free
        venv_bytes = sum(path.lstat().st_size for path in venv.rglob("*") if not path.is_symlink())
        if free_bytes < venv_bytes + plan.total_bytes * 2:
            raise DeploymentError("free disk is insufficient for staging and rollback")
        if backup.exists():
            raise DeploymentError("timestamped rollback path already exists")
        TARGET_ROOT.rename(backup)
        backup_created = True
        manifest_path, manifest_sha = _write_rollback_manifest(backup)
        backup_manifest = manifest_path
        if (
            _sha256_file(manifest_path) != manifest_sha
            or _verify_rollback_manifest(backup) != old_commit
        ):
            raise DeploymentError("rollback backup is not ready for restoration")
        try:
            stage.rename(TARGET_ROOT)
            deployed = True
        except OSError:
            backup.rename(TARGET_ROOT)
            backup_created = False
            backup_manifest.unlink(missing_ok=True)
            raise
        if json.loads((TARGET_ROOT / ".gflow-v2-build.json").read_text(encoding="utf-8")) != {
            "commit_sha": commit
        }:
            raise DeploymentError("deployed commit manifest does not match source")
        deployed_manifest = json.loads((TARGET_ROOT / _MANIFEST_NAME).read_text(encoding="utf-8"))
        verify_deployed_files(TARGET_ROOT, deployed_manifest)
        doctor_output = _run_runtime_checks(commit)
        return {
            "backup_path": str(backup),
            "backup_manifest": str(manifest_path),
            "backup_manifest_sha256": manifest_sha,
            "old_runtime_commit": old_commit,
            "deployed_commit": commit,
            "archive_sha256": plan.archive_sha256,
            "runtime_doctor": doctor_output,
        }
    except BaseException:
        if deployed and TARGET_ROOT.exists() and backup_created:
            failed = BACKUP_ROOT / f"gflow-v2-failed-{stamp}-{commit[:12]}"
            if not failed.exists():
                TARGET_ROOT.rename(failed)
            backup.rename(TARGET_ROOT)
            if backup_manifest is not None:
                backup_manifest.unlink(missing_ok=True)
        elif backup_created and not TARGET_ROOT.exists():
            backup.rename(TARGET_ROOT)
            if backup_manifest is not None:
                backup_manifest.unlink(missing_ok=True)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--commit", help="exact 40-character source commit to preflight/deploy")
    parser.add_argument("--apply", action="store_true", help="promote after all preflight checks")
    modes.add_argument("--rollback", type=Path, help="restore one verified rollback directory")
    args = parser.parse_args()
    try:
        if args.rollback is not None:
            with _deployment_lock():
                print(json.dumps({"status": "ROLLBACK_PASS", **_rollback(args.rollback)}))
            return 0
        if args.commit is None:
            raise DeploymentError("--commit or --rollback is required")
        commit = str(args.commit).lower()
        source = SOURCE_ROOT
        if not _SHA_RE.fullmatch(commit):
            raise DeploymentError("expected source commit SHA is invalid")
        _check_protected_roots()
        data = _check_source(source, commit)
        plan = inspect_archive(data, commit)
        old_commit, venv = _check_runtime()
        _check_no_runtime_processes()
        venv_size = sum(path.lstat().st_size for path in venv.rglob("*") if not path.is_symlink())
        free_bytes = shutil.disk_usage(TARGET_ROOT.parent).free
        if free_bytes < venv_size + plan.total_bytes * 2:
            raise DeploymentError("free disk is insufficient for staging and rollback")
        _check_deployment_roots()
        if args.apply:
            with _deployment_lock():
                data = _check_source(source, commit)
                plan = inspect_archive(data, commit)
                old_commit, venv = _check_runtime()
                _check_no_runtime_processes()
                free_bytes = shutil.disk_usage(TARGET_ROOT.parent).free
                venv_size = sum(
                    path.lstat().st_size for path in venv.rglob("*") if not path.is_symlink()
                )
                if free_bytes < venv_size + plan.total_bytes * 2:
                    raise DeploymentError("free disk is insufficient for staging and rollback")
                result = _promote(data, plan, commit, old_commit, venv)
            print(json.dumps({"status": "PASS", **result}, sort_keys=True))
        else:
            print(
                json.dumps(
                    {
                        "status": "PREFLIGHT_PASS",
                        "mode": "DRY_RUN",
                        "source": str(source),
                        "source_commit": commit,
                        "runtime": str(TARGET_ROOT),
                        "old_runtime_commit": old_commit,
                        "archive_sha256": plan.archive_sha256,
                        "tracked_runtime_file_count": len(plan.file_hashes),
                        "tracked_runtime_bytes": plan.total_bytes,
                        "existing_venv_bytes": venv_size,
                        "free_bytes": free_bytes,
                        "preserved_external_roots": [str(path) for path in PROTECTED_ROOTS],
                        "excluded": [".git", "tests", "docs", "source virtualenv", "credentials"],
                    },
                    sort_keys=True,
                )
            )
    except DeploymentError as exc:
        print(json.dumps({"status": "HOLD", "reason": str(exc)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
