"""Unit checks for the read-only V2 runtime doctor."""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.validation.v2_runtime_doctor import _application_sha, _check, _run


def test_check_record_contains_only_status_and_safe_detail() -> None:
    assert _check("python", True, "3.12.3") == {
        "name": "python",
        "status": "PASS",
        "detail": "3.12.3",
    }


def test_run_bounds_collected_command_output() -> None:
    code, output = _run([sys.executable, "-c", "print('x' * 500)"])

    assert code == 0
    assert len(output) == 300


def test_application_sha_requires_a_well_formed_expected_value(tmp_path: Path) -> None:
    ok, detail = _application_sha(tmp_path, "")

    assert not ok
    assert detail == "expected SHA is missing or invalid"


def test_application_sha_rejects_an_untrusted_manifest(tmp_path: Path) -> None:
    (tmp_path / ".gflow-v2-build.json").write_text('{"commit_sha":"latest"}', encoding="utf-8")

    ok, detail = _application_sha(tmp_path, "a" * 40)

    assert not ok
    assert detail == "installed application SHA does not match expected SHA"
