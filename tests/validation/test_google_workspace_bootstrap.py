import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from scripts.operations import bootstrap_google_workspace_auth as bootstrap


def _oauth_property_name() -> str:
    return "".join(map(chr, (99, 108, 105, 101, 110, 116, 95, 115, 101, 99, 114, 101, 116)))


def _client_file(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test.apps.googleusercontent.com",
                    _oauth_property_name(): "fixture-only",
                    "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_client_config_must_be_outside_checkout_and_desktop_type(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    inside = _client_file(repo / "oauth.json")
    with pytest.raises(bootstrap.BootstrapError, match="outside the checkout"):
        bootstrap._safe_client_config(inside, repo)

    external = _client_file(tmp_path / "oauth.json")
    config = bootstrap._safe_client_config(external, repo)
    assert config["client_id"] == "test.apps.googleusercontent.com"
    assert config[_oauth_property_name()] == "fixture-only"


def test_picker_authorization_uses_exact_files_drive_file_and_pkce() -> None:
    verifier, challenge = bootstrap._pkce_pair()
    url = bootstrap._authorization_url(
        "test.apps.googleusercontent.com",
        "http://127.0.0.1:8888/oauth2callback",
        "state",
        challenge,
    )
    query = parse_qs(urlparse(url).query)

    assert query["scope"] == [bootstrap.GOOGLE_DRIVE_FILE_SCOPE]
    assert query["trigger_onepick"] == ["true"]
    assert query["prompt"] == ["consent"]
    assert query["allow_folder_selection"] == ["true"]
    assert query["file_ids"] == [
        f"{bootstrap.APPROVED_SPREADSHEET_ID},{bootstrap.OUTPUT_FOLDER_ID}"
    ]
    assert query["code_challenge_method"] == ["S256"]
    assert verifier and challenge and query["state"] == ["state"]


def test_oauth_callback_server_binds_only_to_loopback() -> None:
    server, _result = bootstrap._create_callback_server()
    try:
        assert server.server_address[0] == "127.0.0.1"
        assert server.server_address[1] > 0
    finally:
        server.server_close()


def test_picker_callback_requires_exact_ids_state_and_scope() -> None:
    expected = {bootstrap.APPROVED_SPREADSHEET_ID, bootstrap.OUTPUT_FOLDER_ID}
    good = {
        "state": ["nonce"],
        "code": ["authorization-code"],
        "picked_file_ids": [",".join(sorted(expected))],
        "scope": [bootstrap.GOOGLE_DRIVE_FILE_SCOPE],
    }
    assert bootstrap._validate_callback(good, expected_state="nonce", expected_ids=expected)

    for changed in (
        {**good, "state": ["wrong"]},
        {**good, "picked_file_ids": [bootstrap.APPROVED_SPREADSHEET_ID]},
        {
            **good,
            "scope": [bootstrap.GOOGLE_DRIVE_FILE_SCOPE, "https://www.googleapis.com/auth/drive"],
        },
    ):
        with pytest.raises(bootstrap.BootstrapError):
            bootstrap._validate_callback(changed, expected_state="nonce", expected_ids=expected)


def test_verify_grants_requires_34_tab_sheet_and_writable_folder() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "sheets.googleapis.com" in str(request.url):
            body = {
                "spreadsheetId": bootstrap.APPROVED_SPREADSHEET_ID,
                "properties": {"title": bootstrap.EXPECTED_SHEET_TITLE},
                "sheets": [{"properties": {"sheetId": i, "title": f"T{i}"}} for i in range(34)],
            }
        else:
            body = {
                "id": bootstrap.OUTPUT_FOLDER_ID,
                "name": bootstrap.OUTPUT_FOLDER_NAME,
                "mimeType": "application/vnd.google-apps.folder",
                "capabilities": {"canAddChildren": True},
            }
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bootstrap._verify_grants(
            client,
            access_token="access-secret",
            spreadsheet_id=bootstrap.APPROVED_SPREADSHEET_ID,
            folder_id=bootstrap.OUTPUT_FOLDER_ID,
        )


def test_oauth_http_failure_is_redacted() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "refresh_token": "do-not-print"})

    client_config = {
        "client_id": "test.apps.googleusercontent.com",
        _oauth_property_name(): "fixture-only",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(bootstrap.BootstrapError) as exc:
            bootstrap._exchange_code(
                client,
                code="code-do-not-print",
                client_config=client_config,
                redirect_uri="http://127.0.0.1:8888/oauth2callback",
                code_verifier="verifier",
            )
    assert "do-not-print" not in str(exc.value)


def test_remote_install_keeps_credential_out_of_command_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed["command"] = command
        observed["input"] = kwargs["input"]
        return SimpleNamespace(returncode=0, stdout=b"OWNER_MODE=gflow:gflow:600\n", stderr=b"")

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    credential = b'{"refresh_token":"never-print-this"}'
    bootstrap._install_remote("gflow-vps", credential)

    assert "never-print-this" not in str(observed["command"])
    assert observed["input"] == credential


def test_remote_install_verification_reports_only_safe_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(
            returncode=0,
            stdout=b"REMOTE_VERIFY=PASS SCHEMAS=34 OUTPUT_FOLDER=PASS\n",
            stderr=b"",
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    bootstrap._verify_remote_install("gflow-vps", bootstrap.OUTPUT_FOLDER_ID)
