"""Read-only readiness checks for the isolated V2 Linux runtime."""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import socket
import stat as stat_module
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run(command: list[str], *, timeout: int = 10) -> tuple[int, str]:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, type(exc).__name__
    return result.returncode, (result.stdout + result.stderr).strip()[:300]


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "status": "PASS" if ok else "FAIL", "detail": detail}


def run_doctor() -> dict[str, Any]:
    """Inspect the deployment/runtime without reading auth profile contents."""
    checks: list[dict[str, Any]] = []
    root = Path(os.environ.get("GFLOW_APP_ROOT", Path(__file__).resolve().parents[2])).resolve()
    app_src = root / "src"
    if app_src.is_dir() and str(app_src) not in sys.path:
        sys.path.insert(0, str(app_src))
    code, sha = _run(["git", "-C", str(root), "rev-parse", "HEAD"])
    if code != 0:
        try:
            manifest = json.loads((root / ".gflow-v2-build.json").read_text(encoding="utf-8"))
            sha = str(manifest["commit_sha"])
            code = 0
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            sha = "unavailable"
    checks.append(_check("application_sha", code == 0, sha if code == 0 else "unavailable"))

    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    checks.append(_check("python", sys.version_info >= (3, 11), py))
    uv = shutil.which("uv")
    uv_version = _run([uv, "--version"])[1] if uv else "uv not found"
    checks.append(_check("uv", uv is not None, uv_version))

    try:
        import gflow_cli  # noqa: F401

        package_version = importlib.metadata.version("gflow-cli")
        checks.append(_check("gflow_import", True, f"gflow-cli {package_version}"))
    except ImportError:
        checks.append(_check("gflow_import", False, "import failed"))
    try:
        version = importlib.metadata.version("playwright")
        parts = tuple(int(part) for part in version.split(".")[:2])
        checks.append(_check("playwright", (1, 61) <= parts < (1, 62), version))
        from playwright.async_api import async_playwright  # noqa: F401

        checks.append(_check("playwright_import", True, "imported"))
    except (ImportError, importlib.metadata.PackageNotFoundError, ValueError):
        checks.append(_check("playwright", False, "missing or unsupported"))
        checks.append(_check("playwright_import", False, "import failed"))

    for name, binary, args in (
        (
            "chrome",
            shutil.which("google-chrome") or shutil.which("google-chrome-stable"),
            ["--version"],
        ),
        ("ffmpeg", shutil.which("ffmpeg"), ["-version"]),
        ("ffprobe", shutil.which("ffprobe"), ["-version"]),
    ):
        code, detail = _run([binary, *args]) if binary else (1, "not found")
        checks.append(_check(name, code == 0, detail.splitlines()[0] if detail else "no version"))
    xvfb_run = shutil.which("xvfb-run")
    xvfb_code, xvfb_detail = (
        _run([xvfb_run, "-a", "true"]) if xvfb_run else (1, "xvfb-run not found")
    )
    checks.append(
        _check("xvfb", xvfb_code == 0, "xvfb-run -a true" if xvfb_code == 0 else xvfb_detail)
    )

    env = os.environ
    try:
        from gflow_cli.v2_runtime.config import load_runtime_settings

        settings = load_runtime_settings(env)
        checks.append(_check("runtime_configuration", True, settings.environment))
    except Exception as exc:
        checks.append(_check("runtime_configuration", False, type(exc).__name__))
    roots = {
        "runtime_state": Path(env.get("RUNTIME_STATE_ROOT", "/var/lib/gflow")),
        "temp": Path(env.get("TEMP_ROOT", "/var/tmp/gflow")),
        "logs": Path(env.get("LOG_ROOT", "/var/log/gflow")),
    }
    required_owner = env.get("GFLOW_SERVICE_USER", "gflow")
    for name, path in roots.items():
        try:
            stat = path.stat()
            owner = __import__("pwd").getpwuid(stat.st_uid).pw_name
            safe_mode = (stat.st_mode & 0o022) == 0
            checks.append(
                _check(
                    f"{name}_root",
                    path.is_dir() and owner == required_owner and safe_mode,
                    f"exists={path.is_dir()}, owner={owner}, mode={stat.st_mode & 0o777:o}",
                )
            )
        except OSError:
            checks.append(_check(f"{name}_root", False, "missing or unreadable"))
    browser_root = roots["runtime_state"] / "browser-state"
    recovery_root = roots["runtime_state"] / "recovery"
    for name, path in (
        ("browser_state_path", browser_root),
        ("recovery_state_path", recovery_root),
    ):
        try:
            dir_stat = path.stat()
            owner = __import__("pwd").getpwuid(dir_stat.st_uid).pw_name
            restricted = stat_module.S_IMODE(dir_stat.st_mode) == 0o700
            parent_ok = path.is_dir() and owner == required_owner and restricted
            detail = f"exists=true, owner={owner}, mode={stat_module.S_IMODE(dir_stat.st_mode):o}"
        except OSError:
            parent_ok = False
            detail = "missing or unreadable"
        checks.append(
            _check(
                name,
                parent_ok,
                detail,
            )
        )

    try:
        min_free = float(env.get("MIN_FREE_DISK_GB", "10"))
    except ValueError:
        min_free = float("inf")
    free_gb = shutil.disk_usage(
        roots["temp"] if roots["temp"].exists() else Path("/var/tmp")
    ).free / (1024**3)
    checks.append(
        _check("disk", free_gb >= min_free, f"free_gb={free_gb:.2f}, minimum_gb={min_free:.2f}")
    )
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 18766))
        port_ok, port_detail = True, "127.0.0.1:18766 available"
    except OSError:
        port_ok, port_detail = False, "127.0.0.1:18766 already bound"
    checks.append(_check("planned_loopback_port", port_ok, port_detail))
    return {
        "status": "PASS" if all(item["status"] == "PASS" for item in checks) else "FAIL",
        "checks": checks,
    }


def main() -> int:
    report = run_doctor()
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
