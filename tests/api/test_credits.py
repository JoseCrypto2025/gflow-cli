from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from gflow_cli.api.client import FlowApiClient
from gflow_cli.api.dto import CreditsInfo
from gflow_cli.api.transports import migrated_composer as mc
from gflow_cli.errors import AuthExpiredError, FlowAppError, WireFormatError

_REPLY = [37, 3, 8, 1, None, 37, [1793000000, 0]]


def _body(rpcid: str, payload: Any) -> str:
    frame = [["wrb.fr", rpcid, json.dumps(payload), None, None, None, "generic"]]
    return ")]}'\n\n10\n" + json.dumps(frame) + "\n"


class _Response:
    def __init__(self, rpcid: str, payload: Any, *, unreadable: bool = False) -> None:
        self.url = f"https://flow.google.com/data/batchexecute?rpcids={rpcid}"
        self._text = _body(rpcid, payload)
        self.unreadable = unreadable

    async def text(self) -> str:
        if self.unreadable:
            raise RuntimeError("sensitive body text")
        return self._text


class _Page:
    def __init__(self, *, replies: list[_Response], land_on: str = mc.MIGRATED_ROOT_URL) -> None:
        self.url = "about:blank"
        self.replies, self.land_on = replies, land_on
        self.gotos: list[str] = []
        self._handlers: list[Any] = []

    def on(self, event: str, handler: Any) -> None:
        self._handlers.append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        self._handlers.remove(handler)

    async def goto(self, url: str, **_: Any) -> None:
        self.gotos.append(url)
        self.url = self.land_on
        for response in self.replies:
            for handler in list(self._handlers):
                await handler(response)


def test_balance_is_index_zero_of_the_nzlxg_reply() -> None:
    assert CreditsInfo.from_nzlxg(_REPLY) == CreditsInfo(credits=37)
    assert CreditsInfo.from_nzlxg([0]) == CreditsInfo(credits=0)


@pytest.mark.parametrize("payload", [None, [], {}, {"credits": 5}, [True], ["5"], [-1]])
def test_reply_without_non_negative_integer_balance_is_rejected(payload: object) -> None:
    with pytest.raises(ValueError, match="nzlxg"):
        CreditsInfo.from_nzlxg(payload)


async def test_read_credits_ignores_unrelated_rpc_and_returns_nzlxg_balance() -> None:
    page = _Page(replies=[_Response("o30O0e", [1]), _Response(mc.CREDITS_RPC, _REPLY)])

    assert await mc.read_credits(page, timeout_s=1) == CreditsInfo(credits=37)  # type: ignore[arg-type]
    assert page.gotos == [mc.MIGRATED_ROOT_URL]


async def test_missing_nzlxg_is_flow_app_error_not_auth_error() -> None:
    page = _Page(replies=[_Response("o30O0e", [1])])

    with pytest.raises(FlowAppError) as caught:
        await mc.read_credits(page, timeout_s=0.05)  # type: ignore[arg-type]

    assert not isinstance(caught.value, AuthExpiredError)
    assert caught.value.route == f"batchexecute:{mc.CREDITS_RPC}"
    assert "auth login" not in caught.value.remediation_hint
    assert "SAPISID" not in caught.value.remediation_hint


async def test_total_timeout_includes_navigation() -> None:
    class _SlowPage(_Page):
        async def goto(self, url: str, **_: Any) -> None:
            await asyncio.sleep(1)

    page = _SlowPage(replies=[])
    with pytest.raises(FlowAppError):
        await mc.read_credits(page, timeout_s=0.05)  # type: ignore[arg-type]


async def test_signed_out_landing_is_auth_expired() -> None:
    page = _Page(replies=[], land_on="https://accounts.google.com/v3/signin/identifier")

    with pytest.raises(AuthExpiredError):
        await mc.read_credits(page, timeout_s=0.05)  # type: ignore[arg-type]


async def test_about_landing_is_not_misreported_as_missing_rpc() -> None:
    page = _Page(replies=[], land_on="https://flow.google.com/about")

    with pytest.raises(FlowAppError) as caught:
        await mc.read_credits(page, timeout_s=0.05)  # type: ignore[arg-type]

    assert "about" in str(caught.value).lower()


async def test_malformed_nzlxg_reply_is_wire_format_error_without_payload() -> None:
    page = _Page(replies=[_Response(mc.CREDITS_RPC, ["secret-shaped-value"])])

    with pytest.raises(WireFormatError) as caught:
        await mc.read_credits(page, timeout_s=1)  # type: ignore[arg-type]

    assert "secret-shaped-value" not in str(caught.value)


async def test_unreadable_body_fails_with_safe_typed_error() -> None:
    page = _Page(replies=[_Response(mc.CREDITS_RPC, None, unreadable=True)])

    with pytest.raises(WireFormatError) as caught:
        await mc.read_credits(page, timeout_s=0.05)  # type: ignore[arg-type]

    assert "sensitive body text" not in str(caught.value)


async def test_explicit_nzlxg_refusal_is_typed_and_body_free() -> None:
    refusal = [["wrb.fr", mc.CREDITS_RPC, None, None, None, [7, None, []], "generic"]]
    body = ")]}'\n\n10\n" + json.dumps(refusal) + "\n"

    class _RefusalPage(_Page):
        async def goto(self, url: str, **_: Any) -> None:
            self.gotos.append(url)
            self.url = mc.MIGRATED_ROOT_URL
            response = _Response("unrelated", None)
            response._text = body
            for handler in list(self._handlers):
                await handler(response)

    page = _RefusalPage(replies=[])
    with pytest.raises(WireFormatError) as caught:
        await mc.read_credits(page, timeout_s=1)  # type: ignore[arg-type]
    assert "7" in str(caught.value)
    assert body not in str(caught.value)


async def test_client_get_credits_checks_pooled_page_back_in_finally(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    client = FlowApiClient(profile_dir=tmp_path / "profile")
    page = object()
    read = AsyncMock(return_value=CreditsInfo(credits=5))
    checkin = Mock()
    monkeypatch.setattr(client, "_checkout_page", AsyncMock(return_value=page))
    monkeypatch.setattr(client, "_checkin_page", checkin)
    monkeypatch.setattr(mc, "read_credits", read)

    assert await client.get_credits() == CreditsInfo(credits=5)
    read.assert_awaited_once_with(page)
    checkin.assert_called_once_with(page)


async def test_client_checks_page_back_in_after_reader_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    client = FlowApiClient(profile_dir=tmp_path / "profile")
    page = object()
    monkeypatch.setattr(client, "_checkout_page", AsyncMock(return_value=page))
    checkin = Mock()
    monkeypatch.setattr(client, "_checkin_page", checkin)
    monkeypatch.setattr(mc, "read_credits", AsyncMock(side_effect=FlowAppError("miss")))

    with pytest.raises(FlowAppError):
        await client.get_credits()
    checkin.assert_called_once_with(page)


async def test_client_can_disable_raw_har_capture_for_credit_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from gflow_cli.config import Settings

    client = FlowApiClient(
        profile_dir=tmp_path / "profile",
        settings=Settings(har_path=tmp_path / "session.har"),
        har_capture=False,
    )

    assert "record_har_path" not in client._persistent_context_kwargs()
