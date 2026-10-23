from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gflow_cli.api.dto import CreditsInfo
from gflow_cli.errors import AuthExpiredError, FlowAppError
from gflow_cli.profile_store import ProfileMeta


class _FakeClient:
    response: CreditsInfo | Exception = CreditsInfo(credits=5)
    opened: list[Path] = []
    har_capture_values: list[bool] = []

    def __init__(
        self, profile_dir: Path, *, headless: bool = False, har_capture: bool = True
    ) -> None:
        self.profile_dir = profile_dir
        self.headless = headless
        self.har_capture_values.append(har_capture)

    async def __aenter__(self) -> _FakeClient:
        self.opened.append(self.profile_dir)
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def get_credits(self) -> CreditsInfo:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _meta(name: str, *, default: bool = False) -> ProfileMeta:
    return ProfileMeta(
        name=name,
        profile_dir=Path(f"/profiles/{name}"),
        cookies_present=True,
        last_used_at=None,
        is_default=default,
        google_account=f"{name}@example.com",
    )


async def test_inspect_profile_returns_compatible_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    from gflow_cli.services import credits

    _FakeClient.response = CreditsInfo(credits=5)
    _FakeClient.opened = []
    _FakeClient.har_capture_values = []
    monkeypatch.setattr(credits, "FlowApiClient", _FakeClient)
    monkeypatch.setattr(credits.profile_store, "resolve_profile", lambda value: "one")
    monkeypatch.setattr(
        credits.profile_store, "list_profiles", lambda: [_meta("one", default=True)]
    )

    result = await credits.inspect_profile(None)

    assert result == {
        "status": "ok",
        "profile": "one",
        "is_default": True,
        "email": "one@example.com",
        "authenticated": True,
        "credits": 5,
        "subscription_credits": None,
        "user_paygate_tier": None,
        "service_tier": None,
        "sku": None,
    }
    assert _FakeClient.opened == [Path("/profiles/one")]
    assert _FakeClient.har_capture_values == [False]


async def test_service_uses_browser_reader_directly_without_http_fast_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gflow_cli.services import credits

    _FakeClient.response = CreditsInfo(credits=9)
    monkeypatch.setattr(credits, "FlowApiClient", _FakeClient)
    monkeypatch.setattr(credits.profile_store, "resolve_profile", lambda value: "one")
    monkeypatch.setattr(credits.profile_store, "list_profiles", lambda: [_meta("one")])

    result = await credits.inspect_profile(None)

    assert result["credits"] == 9
    assert not hasattr(credits, "fetch_credits_http")


async def test_inspect_all_preserves_partial_results_without_exposing_error_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gflow_cli.services import credits

    _FakeClient.response = FlowAppError(detail="sensitive response body")
    monkeypatch.setattr(credits, "FlowApiClient", _FakeClient)
    monkeypatch.setattr(
        credits.profile_store, "list_profiles", lambda: [_meta("one"), _meta("two")]
    )

    result = await credits.inspect_all_profiles()

    assert result["status"] == "partial"
    assert result["total_credits"] == 0
    assert result["count"] == 2
    assert all(item["authenticated"] is False for item in result["profiles"])
    assert "sensitive response body" not in str(result)


async def test_refresh_renews_only_an_expired_flow_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gflow_cli.services import credits

    calls = 0
    renewals: list[Path] = []

    class ExpiringClient(_FakeClient):
        async def get_credits(self) -> CreditsInfo:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise AuthExpiredError(status=401)
            return CreditsInfo(credits=7)

    async def renew(profile_dir: Path, *, source: str = "renew") -> Any:
        renewals.append(profile_dir)
        assert source == "credits_refresh"
        return SimpleNamespace(renewed=True, result="authenticated")

    monkeypatch.setattr(credits, "FlowApiClient", ExpiringClient)
    monkeypatch.setattr(credits, "renew_profile", renew)
    monkeypatch.setattr(credits.profile_store, "resolve_profile", lambda value: "one")
    monkeypatch.setattr(credits.profile_store, "list_profiles", lambda: [_meta("one")])

    result = await credits.inspect_profile(None, refresh=True)

    assert calls == 2
    assert renewals == [Path("/profiles/one")]
    assert result["credits"] == 7


async def test_without_refresh_expired_session_is_not_renewed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gflow_cli.services import credits

    _FakeClient.response = AuthExpiredError(status=401)
    renewals: list[Path] = []

    async def renew(profile_dir: Path, *, source: str = "renew") -> None:
        renewals.append(profile_dir)

    monkeypatch.setattr(credits, "FlowApiClient", _FakeClient)
    monkeypatch.setattr(credits, "renew_profile", renew)
    monkeypatch.setattr(credits.profile_store, "resolve_profile", lambda value: "one")
    monkeypatch.setattr(credits.profile_store, "list_profiles", lambda: [_meta("one")])

    with pytest.raises(AuthExpiredError):
        await credits.inspect_profile(None)
    assert renewals == []


async def test_refresh_does_not_renew_missing_rpc_or_malformed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gflow_cli.services import credits

    _FakeClient.response = FlowAppError(detail="no balance reply")
    renewals: list[Path] = []

    async def renew(profile_dir: Path, *, source: str = "renew") -> None:
        renewals.append(profile_dir)

    monkeypatch.setattr(credits, "FlowApiClient", _FakeClient)
    monkeypatch.setattr(credits, "renew_profile", renew)
    monkeypatch.setattr(credits.profile_store, "resolve_profile", lambda value: "one")
    monkeypatch.setattr(credits.profile_store, "list_profiles", lambda: [_meta("one")])

    with pytest.raises(FlowAppError):
        await credits.inspect_profile(None, refresh=True)
    assert renewals == []
