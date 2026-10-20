from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gflow_cli.v2_runtime.prompt_version_lifecycle import PromptVersionLifecycleError
from scripts.operations import reconcile_prompt_version as operator


def _profile(profile_id: str, environment: str, *, status: str = "ACTIVE") -> dict[str, str]:
    return {
        "Generation_Profile_ID": profile_id,
        "Environment": environment,
        "Status": status,
    }


def _stub_snapshot_and_phase_e(
    monkeypatch: pytest.MonkeyPatch, profiles: list[dict[str, str]]
) -> dict[str, Any]:
    snapshot = SimpleNamespace(tables={"GENERATION_PROFILE_MASTER": profiles})
    monkeypatch.setattr(operator, "_read_snapshot", lambda *_: snapshot)
    captured: dict[str, Any] = {}

    def fake_preflight(catalog, scene_id: str, profile_id: str, environment: str):
        captured.update(
            catalog=catalog,
            scene_id=scene_id,
            profile_id=profile_id,
            environment=environment,
        )
        return SimpleNamespace(status="PREFLIGHT_PASS")

    monkeypatch.setattr(operator, "preflight_scene", fake_preflight)
    return captured


@pytest.mark.parametrize(
    ("profile_id", "environment"),
    [("GPR_0001", "TEST"), ("GPR_0002", "PROD")],
)
def test_fresh_phase_e_uses_unique_active_profile_environment(
    monkeypatch: pytest.MonkeyPatch, profile_id: str, environment: str
) -> None:
    captured = _stub_snapshot_and_phase_e(monkeypatch, [_profile(profile_id, environment)])

    result = operator._fresh_phase_e(
        Path("credential.json"), Path("schemas"), "SCN_000001", profile_id
    )

    assert result.status == "PREFLIGHT_PASS"
    assert captured["scene_id"] == "SCN_000001"
    assert captured["profile_id"] == profile_id
    assert captured["environment"] == environment


@pytest.mark.parametrize(
    ("profiles", "profile_id", "expected_code"),
    [
        ([], "GPR_0001", "PROFILE_NOT_FOUND"),
        (
            [_profile("GPR_0001", "TEST"), _profile("GPR_0001", "TEST")],
            "GPR_0001",
            "PROFILE_DUPLICATE",
        ),
        ([_profile("GPR_0001", "TEST", status="HOLD")], "GPR_0001", "PROFILE_INACTIVE"),
        ([_profile("GPR_0001", "   ")], "GPR_0001", "PROFILE_ENVIRONMENT_MISSING"),
        ([_profile("GPR_0001", "STAGING")], "GPR_0001", "PROFILE_ENVIRONMENT_UNSUPPORTED"),
    ],
)
def test_fresh_phase_e_fails_closed_for_invalid_profile_environment(
    monkeypatch: pytest.MonkeyPatch,
    profiles: list[dict[str, str]],
    profile_id: str,
    expected_code: str,
) -> None:
    captured = _stub_snapshot_and_phase_e(monkeypatch, profiles)

    with pytest.raises(PromptVersionLifecycleError) as raised:
        operator._fresh_phase_e(Path("credential.json"), Path("schemas"), "SCN_000001", profile_id)

    assert raised.value.code == expected_code
    assert "environment" not in captured


@pytest.mark.parametrize(
    ("profile_id", "environment", "forced_environment"),
    [("GPR_0001", "TEST", "PROD"), ("GPR_0002", "PROD", "TEST")],
)
def test_caller_cannot_override_profile_environment(
    monkeypatch: pytest.MonkeyPatch,
    profile_id: str,
    environment: str,
    forced_environment: str,
) -> None:
    _stub_snapshot_and_phase_e(monkeypatch, [_profile(profile_id, environment)])

    with pytest.raises(TypeError):
        operator._fresh_phase_e(
            Path("credential.json"),
            Path("schemas"),
            "SCN_000001",
            profile_id,
            environment=forced_environment,
        )
