from __future__ import annotations

from pathlib import Path

import pytest

from scripts.operations import inspect_flow_pricing_read_only as inspector


def test_inspector_defaults_to_existing_production_profile_root(monkeypatch) -> None:
    monkeypatch.delenv("GFLOW_CLI_HOME", raising=False)

    inspector._ensure_prod_profile_home()

    assert Path(inspector.os.environ["GFLOW_CLI_HOME"]) == Path("/var/lib/gflow/browser-state")


def test_inspector_refuses_a_different_profile_root(monkeypatch) -> None:
    monkeypatch.setenv("GFLOW_CLI_HOME", "/tmp/other-profile-root")

    with pytest.raises(ValueError, match="profile root differs"):
        inspector._ensure_prod_profile_home()
