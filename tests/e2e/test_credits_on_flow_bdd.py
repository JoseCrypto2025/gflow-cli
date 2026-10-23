"""Read-only BDD proof that the CLI balance matches the migrated Flow app."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner
from pytest_bdd import given, scenarios, then, when

from gflow_cli.cli import main

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_auth]
scenarios("../features/credits_on_flow.feature")

_AVATAR = '.header-user-button:has(img[src*="googleusercontent"])'
_MENU_BALANCE = "flow-account-panel span.credits-count"


@pytest.fixture
def ctx() -> dict[str, Any]:
    return {}


@given("a live profile that reaches the Flow app")
def _profile(e2e_profile_dir: Path, ctx: dict[str, Any]) -> None:
    ctx["dir"] = e2e_profile_dir
    ctx["profile"] = e2e_profile_dir.name.removeprefix("profile_")


@when('I run "gflow credits user --json" for that profile')
def _run(ctx: dict[str, Any]) -> None:
    ctx["result"] = CliRunner().invoke(
        main, ["credits", "user", "--profile", ctx["profile"], "--json"]
    )


@then("the command exits 0")
def _exit_zero(ctx: dict[str, Any]) -> None:
    result = ctx["result"]
    assert result.exit_code == 0, result.output


@then("the JSON reports a non-negative integer balance for that profile")
def _balance(ctx: dict[str, Any]) -> None:
    output = ctx["result"].output
    lines = output.splitlines(keepends=True)
    first = next(index for index, line in enumerate(lines) if line.strip() == "{")
    start = sum(len(line) for line in lines[:first])
    payload, _ = json.JSONDecoder().raw_decode(output[start:])
    assert payload["status"] == "ok", payload
    assert payload["profile"] == ctx["profile"]
    assert isinstance(payload["credits"], int) and not isinstance(payload["credits"], bool)
    assert payload["credits"] >= 0
    ctx["credits"] = payload["credits"]


async def _menu_balance(profile_dir: Path) -> int:
    from gflow_cli.api.client import FlowApiClient
    from gflow_cli.api.transports.migrated_composer import MIGRATED_ROOT_URL

    async with FlowApiClient(profile_dir=profile_dir, har_capture=False) as client:
        page = await client._checkout_page()  # noqa: SLF001 - read-only live comparison
        try:
            await page.goto(MIGRATED_ROOT_URL, wait_until="domcontentloaded", timeout=60_000)
            await page.locator(_AVATAR).first.click(timeout=30_000)
            text = await page.locator(_MENU_BALANCE).first.inner_text(timeout=15_000)
            await page.keyboard.press("Escape")
        finally:
            client._checkin_page(page)  # noqa: SLF001
    digits = re.sub(r"\D", "", text)
    assert digits, "the account menu's balance element held no number"
    return int(digits)


@then("the balance equals the number Flow's account menu shows")
def _matches_menu(ctx: dict[str, Any]) -> None:
    assert asyncio.run(_menu_balance(ctx["dir"])) == ctx["credits"]
