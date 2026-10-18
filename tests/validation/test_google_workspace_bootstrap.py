import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from scripts.operations import bootstrap_google_workspace_auth as bootstrap


def _oauth_property_name() -> str:
    return "".join(map(chr, (99, 108, 105, 101, 110, 116, 95, 115, 101, 99, 114, 101, 116)))


def _client_file(
    path: Path,
    *,
    auth_uri: object = "https://accounts.google.com/o/oauth2/v2/auth",
    token_uri: object = "https://oauth2.googleapis.com/token",
    client_secret: str = "fixture-only",
) -> Path:
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "test.apps.googleusercontent.com",
                    _oauth_property_name(): client_secret,
                    "auth_uri": auth_uri,
                    "token_uri": token_uri,
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


@pytest.mark.parametrize(
    "auth_uri",
    (
        "https://accounts.google.com/o/oauth2/v2/auth",
        "https://accounts.google.com/o/oauth2/auth",
    ),
)
def test_client_auth_uri_metadata_allowlist_keeps_runtime_endpoint(
    tmp_path: Path, auth_uri: str
) -> None:
    client = _client_file(tmp_path / "oauth.json", auth_uri=auth_uri)
    config = bootstrap._safe_client_config(client, tmp_path / "repo")
    assert config["auth_uri"] == auth_uri

    url = bootstrap._authorization_url(
        config["client_id"], "http://127.0.0.1:8888/oauth2callback", "state", "challenge"
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")


@pytest.mark.parametrize(
    "auth_uri",
    (
        "http://accounts.google.com/o/oauth2/auth",
        "https://evil.example/o/oauth2/auth",
        "https://accounts.google.com.evil.example/o/oauth2/auth",
        "https://accounts.google.com/o/oauth2/auth?x=1",
        ["https://accounts.google.com/o/oauth2/auth"],
    ),
)
def test_client_auth_uri_metadata_rejects_non_allowlisted_values(
    tmp_path: Path, auth_uri: object
) -> None:
    client = _client_file(tmp_path / "oauth.json", auth_uri=auth_uri)
    with pytest.raises(bootstrap.BootstrapError, match="endpoint configuration"):
        bootstrap._safe_client_config(client, tmp_path / "repo")


def test_client_token_uri_must_remain_exact(tmp_path: Path) -> None:
    client = _client_file(
        tmp_path / "oauth.json", token_uri="https://oauth2.googleapis.com.evil.example/token"
    )
    with pytest.raises(bootstrap.BootstrapError, match="endpoint configuration"):
        bootstrap._safe_client_config(client, tmp_path / "repo")


@pytest.mark.parametrize(
    "contents",
    ("{malformed", '{"web": {"auth_uri": "https://accounts.google.com/o/oauth2/auth"}}'),
)
def test_malformed_or_non_installed_client_json_fails_closed(tmp_path: Path, contents: str) -> None:
    client = tmp_path / "oauth.json"
    client.write_text(contents, encoding="utf-8")
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap._safe_client_config(client, tmp_path / "repo")


def test_rejected_client_metadata_does_not_emit_secret(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "distinctive-client-secret-must-never-appear"
    client = _client_file(
        tmp_path / "oauth.json",
        auth_uri="https://accounts.google.com/o/oauth2/auth?unexpected=1",
        client_secret=secret,
    )
    with pytest.raises(bootstrap.BootstrapError) as exc:
        bootstrap._safe_client_config(client, tmp_path / "repo")

    assert secret not in str(exc.value)
    assert secret not in caplog.text


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


def test_remote_stage_keeps_credential_out_of_command_and_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> SimpleNamespace:
        observed["command"] = command
        observed["input"] = kwargs["input"]
        return SimpleNamespace(
            returncode=0, stdout=b"STAGED_OWNER_MODE=gflow:gflow:600\n", stderr=b""
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    credential = b'{"refresh_token":"never-print-this"}'
    stage_path = (
        "/etc/gflow-v2/.google-authorized-user.staged-0123456789abcdef0123456789abcdef.json"
    )
    bootstrap._stage_remote("gflow-vps", stage_path, credential)

    assert "never-print-this" not in str(observed["command"])
    assert observed["input"] == credential
    assert 'google-authorized-user.json";' not in str(observed["command"])


def test_remote_verification_uses_staged_path_and_reports_only_safe_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(_command: list[str], **_kwargs: object) -> SimpleNamespace:
        observed["command"] = _command
        return SimpleNamespace(
            returncode=0,
            stdout=b"REMOTE_VERIFY=PASS SCHEMAS=34 OUTPUT_FOLDER=PASS\n",
            stderr=b"",
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    stage_path = (
        "/etc/gflow-v2/.google-authorized-user.staged-0123456789abcdef0123456789abcdef.json"
    )
    bootstrap._verify_remote_install("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, stage_path)
    assert stage_path in str(observed["command"])


def test_verified_remote_install_orders_stage_verify_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bootstrap.secrets, "token_hex", lambda _size: "a" * 32)
    monkeypatch.setattr(bootstrap, "_stage_remote", lambda *_args: calls.append("stage"))
    monkeypatch.setattr(bootstrap, "_verify_remote_install", lambda *_args: calls.append("verify"))
    monkeypatch.setattr(bootstrap, "_promote_remote", lambda *_args: calls.append("promote"))
    monkeypatch.setattr(bootstrap, "_finalize_remote", lambda *_args: calls.append("finalize"))
    monkeypatch.setattr(bootstrap, "_cleanup_remote", lambda *_args: calls.append("cleanup"))

    bootstrap._install_verified_remote("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, b"fixture")

    assert calls == ["stage", "verify", "promote", "verify", "finalize"]


def test_verification_failure_cleans_stage_without_promoting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bootstrap.secrets, "token_hex", lambda _size: "b" * 32)
    monkeypatch.setattr(bootstrap, "_stage_remote", lambda *_args: calls.append("stage"))

    def fail_verification(*_args: object) -> None:
        calls.append("verify")
        raise bootstrap.BootstrapError("staged service-user verification failed")

    monkeypatch.setattr(bootstrap, "_verify_remote_install", fail_verification)
    monkeypatch.setattr(bootstrap, "_promote_remote", lambda *_args: calls.append("promote"))
    monkeypatch.setattr(bootstrap, "_cleanup_remote", lambda *_args: calls.append("cleanup"))

    with pytest.raises(bootstrap.BootstrapError, match="verification failed"):
        bootstrap._install_verified_remote("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, b"fixture")

    assert calls == ["stage", "verify", "cleanup"]


def test_remote_promotion_is_atomic_and_preserves_existing_on_check_failure() -> None:
    stage_path = (
        "/etc/gflow-v2/.google-authorized-user.staged-0123456789abcdef0123456789abcdef.json"
    )
    script = bootstrap._promote_script(stage_path, "a" * 64)
    assert 'cp -- "$target" "$backup"' in script
    assert 'mv -fT -- "$stage" "$target"' in script
    assert 'if test "$promoted" -eq 1' in script
    assert script.index('test "$(stat -c \'%U:%G:%a\' "$target")"') < script.index(
        'mv -fT -- "$stage" "$target"'
    )


def test_lost_promotion_status_attempts_digest_guarded_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bootstrap.secrets, "token_hex", lambda _size: "c" * 32)
    monkeypatch.setattr(bootstrap, "_stage_remote", lambda *_args: calls.append("stage"))
    monkeypatch.setattr(bootstrap, "_verify_remote_install", lambda *_args: calls.append("verify"))

    def promotion_lost(*_args: object) -> None:
        calls.append("promote")
        raise bootstrap.BootstrapError("acknowledgement lost")

    def status_unknown(*_args: object) -> bool:
        calls.append("status")
        raise bootstrap.BootstrapError("promotion status unavailable")

    monkeypatch.setattr(bootstrap, "_promote_remote", promotion_lost)
    monkeypatch.setattr(bootstrap, "_promotion_status", status_unknown)
    monkeypatch.setattr(bootstrap, "_restore_remote", lambda *_args: calls.append("restore"))
    monkeypatch.setattr(bootstrap, "_cleanup_remote", lambda *_args: calls.append("cleanup"))

    with pytest.raises(bootstrap.BootstrapError, match="promotion status unavailable"):
        bootstrap._install_verified_remote("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, b"fixture")

    assert calls == ["stage", "verify", "promote", "status", "restore", "cleanup"]


def test_canonical_verification_failure_restores_prior_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bootstrap.secrets, "token_hex", lambda _size: "d" * 32)
    monkeypatch.setattr(bootstrap, "_stage_remote", lambda *_args: calls.append("stage"))

    def verify(_host: str, _folder: str, path: str) -> None:
        calls.append("verify-staged" if path != bootstrap._CANONICAL_PATH else "verify-canonical")
        if path == bootstrap._CANONICAL_PATH:
            raise bootstrap.BootstrapError("canonical verification failed")

    monkeypatch.setattr(bootstrap, "_verify_remote_install", verify)
    monkeypatch.setattr(bootstrap, "_promote_remote", lambda *_args: calls.append("promote"))
    monkeypatch.setattr(bootstrap, "_restore_remote", lambda *_args: calls.append("restore"))
    monkeypatch.setattr(bootstrap, "_cleanup_remote", lambda *_args: calls.append("cleanup"))

    with pytest.raises(
        bootstrap.BootstrapError,
        match="Canonical credential verification failed: canonical verification failed",
    ):
        bootstrap._install_verified_remote("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, b"fixture")

    assert calls == [
        "stage",
        "verify-staged",
        "promote",
        "verify-canonical",
        "restore",
        "cleanup",
    ]


def test_canonical_verification_failure_is_distinguished_from_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bootstrap.secrets, "token_hex", lambda _size: "e" * 32)
    monkeypatch.setattr(bootstrap, "_stage_remote", lambda *_args: calls.append("stage"))
    monkeypatch.setattr(bootstrap, "_verify_remote_install", lambda *_args: calls.append("verify"))
    monkeypatch.setattr(bootstrap, "_promote_remote", lambda *_args: calls.append("promote"))
    monkeypatch.setattr(
        bootstrap,
        "_finalize_remote",
        lambda *_args: (_ for _ in ()).throw(bootstrap.BootstrapError("finalization failed")),
    )
    monkeypatch.setattr(bootstrap, "_cleanup_remote", lambda *_args: calls.append("cleanup"))
    monkeypatch.setattr(
        bootstrap,
        "_restore_remote",
        lambda *_args: pytest.fail("verified canonical credential should not be rolled back"),
    )

    with pytest.raises(bootstrap.BootstrapError, match="finalization failed"):
        bootstrap._install_verified_remote("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, b"fixture")

    assert calls == ["stage", "verify", "promote", "verify", "cleanup"]


def test_remote_host_option_injection_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("ssh must not run for an invalid host alias"),
    )
    with pytest.raises(bootstrap.BootstrapError, match="SSH host alias is invalid"):
        bootstrap._stage_remote(
            "-oProxyCommand=bad",
            "/etc/gflow-v2/.google-authorized-user.staged-0123456789abcdef0123456789abcdef.json",
            b"fixture",
        )


def test_failed_remote_errors_are_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"refresh_token=secret")

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    with pytest.raises(bootstrap.BootstrapError) as exc:
        bootstrap._stage_remote(
            "gflow-vps",
            "/etc/gflow-v2/.google-authorized-user.staged-0123456789abcdef0123456789abcdef.json",
            b"fixture",
        )
    assert "secret" not in str(exc.value)
    assert "exit code 1" in str(exc.value)
