"""Read-only live BDD proof for the pinned VPS Flow pricing inspector."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pytest_bdd import given, scenarios, then, when

from gflow_cli.v2_runtime.flow_pricing_inspector import (
    EXPECTED_EMAIL,
    EXPECTED_PROFILE_NAME,
    EXPECTED_PROFILE_PATH,
    EXPECTED_PROJECT_ID,
    EXPECTED_PROVIDER_ACCOUNT_ID,
)

scenarios("../features/vps_flow_pricing_inspection.feature")


@pytest.fixture
def world() -> dict[str, Any]:
    return {}


@given("the existing gflow-prod profile and exact production project")
def _pinned_profile_and_project(world: dict[str, Any]) -> None:
    # The shared e2e_profile_dir fixture intentionally drops GFLOW_CLI_HOME
    # and resolves the interactive user's default profile store. This V2
    # service profile lives at a separately configured, exact production path.
    assert os.environ.get("GFLOW_CLI_E2E_PROFILE") == EXPECTED_PROFILE_NAME
    assert EXPECTED_PROFILE_PATH.is_dir() and not EXPECTED_PROFILE_PATH.is_symlink()
    assert EXPECTED_PROFILE_PATH.resolve(strict=True) == EXPECTED_PROFILE_PATH.resolve(strict=False)
    world["profile"] = EXPECTED_PROFILE_PATH


@when("I run the read-only Flow pricing inspection")
def _inspect_flow_pricing(world: dict[str, Any]) -> None:
    script = Path("/opt/gflow-v2/scripts/operations/inspect_flow_pricing_read_only.py")
    assert script.is_file() and not script.is_symlink(), "deployed inspector script is unavailable"
    environment = os.environ.copy()
    environment["PYTHONUTF8"] = "1"
    environment["GFLOW_CLI_HOME"] = str(EXPECTED_PROFILE_PATH.parent)
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--provider-account-id",
            EXPECTED_PROVIDER_ACCOUNT_ID,
            "--project-id",
            EXPECTED_PROJECT_ID,
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=180,
    )
    assert completed.returncode in {0, 2}, f"inspector exited {completed.returncode}"
    report = json.loads(completed.stdout)
    assert re.fullmatch(r"[0-9a-f]{40}", report["SOFTWARE_COMMIT"])
    world["report"] = report
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))


@then("the report verifies the expected Flow account and project")
def _identity_is_verified(world: dict[str, Any]) -> None:
    report = world["report"]
    assert report["ACCOUNT_MATCH"] == "PASS", report["REASON_CODE"]
    assert report["PROJECT_MATCH"] == "PASS", report["REASON_CODE"]
    assert report["FLOW_ACCOUNT"] == EXPECTED_EMAIL
    assert report["FLOW_PROJECT_ID"] == EXPECTED_PROJECT_ID


@then("the report contains no provider mutation and no secret values")
def _no_provider_mutation_or_secrets(world: dict[str, Any]) -> None:
    report = world["report"]
    assert report["PROFILE_LOCK_RESPECTED"] == "PASS"
    assert report["PROVIDER_GENERATE_ACTIONS"] == 0
    assert report["NEW_ATTEMPTS_CREATED"] == 0
    assert report["NEW_RESERVATIONS_CREATED"] == 0
    assert report["APPROVAL_ACTIONS"] == 0
    assert report["PROVIDER_CREDITS_CONSUMED"] == 0
    assert report["FLOW_STATE_CHANGED"] == "NO"
    if report["CREDIT_COST_AUTHORITATIVE"] == "YES":
        assert report["CONFIG_MATCH"] == "PASS"
        assert report["DISPLAYED_CREDIT_COST"] is not None
    serialized = json.dumps(report).casefold()
    assert "cookie" not in serialized
    assert "token" not in serialized
    assert "prompt" not in serialized
