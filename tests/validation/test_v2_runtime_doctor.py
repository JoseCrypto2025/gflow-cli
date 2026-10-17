"""Unit checks for the read-only V2 runtime doctor."""

from __future__ import annotations

import sys

from scripts.validation.v2_runtime_doctor import _check, _run


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
