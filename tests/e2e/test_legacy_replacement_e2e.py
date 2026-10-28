"""One-shot real Flow verification for the exact authorized PROD replacement."""

from __future__ import annotations

import os

import pytest

from scripts.operations.run_legacy_replacement import run_once

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_video]

_CONFIRMATION = "GFLOW_CLI_E2E_RUN_LEGACY_REPLACEMENT"
_PARENT_ATTEMPT_ID = "D2_13c62112d9881710019049d95cb6da57"
_SCENE_ID = "SCN_000001"
_PROFILE_ID = "GPR_0002"
_ACCOUNT_ID = "PVA_0002"
_PROJECT_ID = "aafe9282-071f-4333-baa4-92b9f76a9c27"
_EXPECTED_EMAIL = "singleava508@gmail.com"


def test_one_controlled_non_ultra_legacy_replacement() -> None:
    """Exercise the supported operator once; replay is same-attempt only."""
    if os.environ.get(_CONFIRMATION) != "I_CONFIRM_ONE_PROD_REPLACEMENT_GENERATION":
        pytest.skip(f"set {_CONFIRMATION} to opt in to this credit-consuming test")
    if os.environ.get("GFLOW_CLI_E2E_PROFILE", "").strip() != "gflow-prod":
        pytest.fail("legacy replacement accepts only the existing gflow-prod profile")
    result = run_once(
        scene_id=_SCENE_ID,
        profile_id=_PROFILE_ID,
        account_id=_ACCOUNT_ID,
        project_id=_PROJECT_ID,
        expected_email=_EXPECTED_EMAIL,
        parent_attempt_id=_PARENT_ATTEMPT_ID,
        execute=True,
    )
    assert result["status"] == "PASS", result
    assert result["quote_amount"] == "10"
    assert result["generate_clicked"] is True
    assert result["generate_action_count"] == 1
    assert type(result["approval_action_count"]) is int
    assert result["approval_action_count"] <= 1
    assert result["reservation_credits"] == "10"
    assert result["output_found"] is True
    assert result["d7_entered"] is False
