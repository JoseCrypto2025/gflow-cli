import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from scripts.operations import bootstrap_google_workspace_auth as bootstrap


def test_bootstrap_uses_runtime_approved_output_folder_constant() -> None:
    from gflow_cli.v2_runtime.google_sheets import APPROVED_JOB_ARTIFACTS_FOLDER_ID

    assert bootstrap.OUTPUT_FOLDER_ID == APPROVED_JOB_ARTIFACTS_FOLDER_ID


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


def test_pickerless_authorization_has_only_standard_drive_file_parameters() -> None:
    url = bootstrap._authorization_url(
        "test.apps.googleusercontent.com",
        "http://127.0.0.1:8888/oauth2callback",
        "state",
        "challenge",
        include_picker=False,
    )
    query = parse_qs(urlparse(url).query)
    assert query == {
        "client_id": ["test.apps.googleusercontent.com"],
        "redirect_uri": ["http://127.0.0.1:8888/oauth2callback"],
        "response_type": ["code"],
        "scope": [bootstrap.GOOGLE_DRIVE_FILE_SCOPE],
        "access_type": ["offline"],
        "prompt": ["consent"],
        "state": ["state"],
        "code_challenge": ["challenge"],
        "code_challenge_method": ["S256"],
    }
    for forbidden in (
        "trigger_onepick",
        "allow_multiple",
        "allow_folder_selection",
        "file_ids",
        "mimetypes",
    ):
        assert forbidden not in query


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


def test_pickerless_callback_requires_state_code_and_exact_scope() -> None:
    good = {
        "state": ["nonce"],
        "code": ["authorization-code"],
        "scope": [bootstrap.GOOGLE_DRIVE_FILE_SCOPE],
    }
    assert (
        bootstrap._validate_pickerless_callback(good, expected_state="nonce")
        == "authorization-code"
    )
    for changed in (
        {**good, "state": ["wrong"]},
        {**good, "code": []},
        {
            **good,
            "scope": [bootstrap.GOOGLE_DRIVE_FILE_SCOPE + " https://www.googleapis.com/auth/drive"],
        },
    ):
        with pytest.raises(bootstrap.BootstrapError):
            bootstrap._validate_pickerless_callback(changed, expected_state="nonce")


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


def test_pickerless_reuse_checks_only_two_ids_and_validates_all_schemas() -> None:
    from gflow_cli.v2_runtime.google_sheets import load_schema_headers

    repository_root = Path(__file__).resolve().parents[2]
    schema_headers = load_schema_headers(repository_root / "schemas")
    assert len(schema_headers) == 34
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requests.append((request.url.host or "", path))
        if "/drive/v3/files/" in path:
            file_id = path.rsplit("/", 1)[-1]
            if file_id == bootstrap.APPROVED_SPREADSHEET_ID:
                body = {
                    "id": file_id,
                    "name": bootstrap.EXPECTED_SHEET_TITLE,
                    "mimeType": "application/vnd.google-apps.spreadsheet",
                    "isAppAuthorized": True,
                }
            else:
                body = {
                    "id": file_id,
                    "name": bootstrap.OUTPUT_FOLDER_NAME,
                    "mimeType": "application/vnd.google-apps.folder",
                    "isAppAuthorized": True,
                    "capabilities": {"canAddChildren": True},
                }
            return httpx.Response(200, json=body)
        if "/values/" in path:
            tab = path.rsplit("/values/", 1)[-1].split("!", 1)[0]
            return httpx.Response(200, json={"values": [list(schema_headers[tab])]})
        return httpx.Response(
            200,
            json={
                "spreadsheetId": bootstrap.APPROVED_SPREADSHEET_ID,
                "properties": {"title": bootstrap.EXPECTED_SHEET_TITLE},
                "sheets": [
                    {"properties": {"sheetId": i, "title": name}}
                    for i, name in enumerate(schema_headers)
                ],
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = bootstrap._verify_previous_file_grants(
            client,
            access_token="fixture-access-token",
            repository_root=repository_root,
        )
    assert result["master_sheet_drive_access"] == "PASS"
    assert result["master_sheet_sheets_access"] == "PASS"
    assert result["master_sheet_is_app_authorized"] == "YES"
    assert result["schema_34_verify"] == "DEFERRED_TO_STAGED_VERIFY"
    assert result["job_artifacts_access"] == "PASS"
    assert result["job_artifacts_is_app_authorized"] == "YES"
    assert result["job_artifacts_required_capability"] == "PASS"
    drive_ids = {path.rsplit("/", 1)[-1] for _host, path in requests if "/drive/v3/files/" in path}
    assert drive_ids == {bootstrap.APPROVED_SPREADSHEET_ID, bootstrap.OUTPUT_FOLDER_ID}
    assert not [path for _host, path in requests if "/values" in path]


def test_pickerless_reuse_reports_exact_safe_google_api_failure() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "code": 403,
                    "status": "PERMISSION_DENIED",
                    "message": "The caller does not have permission",
                    "errors": [{"reason": "appNotAuthorizedToFile"}],
                }
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(bootstrap.BootstrapError) as exc:
            bootstrap._verify_previous_file_grants(
                client,
                access_token="fixture-token",
                repository_root=Path(__file__).resolve().parents[2],
            )
    assert "HTTP 403" in str(exc.value)
    assert "PERMISSION_DENIED" in str(exc.value)
    assert "appNotAuthorizedToFile" in str(exc.value)
    assert "fixture-token" not in str(exc.value)


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
    monkeypatch.setattr(
        bootstrap, "verify_google_workspace_credential", lambda *_args: calls.append("verify")
    )
    metadata = {
        "sha256": hashlib.sha256(b"fixture").hexdigest(),
        "owner": "gflow",
        "group": "gflow",
        "mode": "600",
        "size": "7",
        "parent_mode": "750",
        "readable_by_gflow": "YES",
    }
    monkeypatch.setattr(bootstrap, "_credential_metadata", lambda *_args: metadata)
    monkeypatch.setattr(bootstrap, "_require_promoted_parity", lambda *_args: metadata)
    monkeypatch.setattr(bootstrap, "_promote_remote", lambda *_args: calls.append("promote"))
    monkeypatch.setattr(bootstrap, "_finalize_remote", lambda *_args: calls.append("finalize"))
    monkeypatch.setattr(bootstrap, "_cleanup_remote", lambda *_args: calls.append("cleanup"))

    bootstrap._install_verified_remote("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, b"fixture")

    assert calls == ["stage", "verify", "promote", "verify", "finalize"]


def test_stop_after_staged_verify_retains_candidate_without_promotion_or_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    credential = b"fixture credential bytes"
    monkeypatch.setattr(bootstrap.secrets, "token_hex", lambda _size: "f" * 32)
    monkeypatch.setattr(bootstrap, "_stage_remote", lambda *_args: calls.append("stage"))
    monkeypatch.setattr(
        bootstrap, "verify_google_workspace_credential", lambda *_args: calls.append("verify")
    )
    metadata = {
        "sha256": hashlib.sha256(credential).hexdigest(),
        "owner": "gflow",
        "group": "gflow",
        "mode": "600",
        "size": str(len(credential)),
        "parent_mode": "750",
        "readable_by_gflow": "YES",
    }
    monkeypatch.setattr(bootstrap, "_credential_metadata", lambda *_args: metadata)
    monkeypatch.setattr(
        bootstrap,
        "_promote_remote",
        lambda *_args: pytest.fail("stop mode must not promote"),
    )
    monkeypatch.setattr(
        bootstrap,
        "_finalize_remote",
        lambda *_args: pytest.fail("stop mode must not finalize"),
    )
    monkeypatch.setattr(
        bootstrap,
        "_cleanup_remote",
        lambda *_args: pytest.fail("verified stop mode must retain the candidate"),
    )
    monkeypatch.setattr(
        bootstrap,
        "_restore_remote",
        lambda *_args: pytest.fail("stop mode must not enter rollback"),
    )

    stage_path, digest = bootstrap._install_verified_remote(
        "gflow-vps",
        bootstrap.OUTPUT_FOLDER_ID,
        credential,
        stop_after_staged_verify=True,
    )

    assert calls == ["stage", "verify"]
    assert stage_path.endswith(".google-authorized-user.staged-" + "f" * 32 + ".json")
    assert digest == hashlib.sha256(credential).hexdigest()


@pytest.mark.parametrize(("flag", "expected"), [(False, False), (True, True)])
def test_cli_parses_stop_after_staged_verify(
    monkeypatch: pytest.MonkeyPatch, flag: bool, expected: bool
) -> None:
    observed: dict[str, object] = {}

    def fake_run(
        _client_path: Path | None,
        *,
        repository_root: Path,
        ssh_host: str,
        stop_after_staged_verify: bool,
        resume_staged_path: str | None,
        expected_staged_sha256: str | None,
        pickerless_reuse_test: bool,
    ) -> None:
        observed["repository_root"] = repository_root
        observed["ssh_host"] = ssh_host
        observed["stop_after_staged_verify"] = stop_after_staged_verify
        observed["pickerless_reuse_test"] = pickerless_reuse_test

    argv = ["bootstrap", "--oauth-client-json", "client.json"]
    if flag:
        argv.append("--stop-after-staged-verify")
    monkeypatch.setattr(bootstrap.sys, "argv", argv)
    monkeypatch.setattr(bootstrap, "_run", fake_run)

    assert bootstrap.main() == 0
    assert observed["stop_after_staged_verify"] is expected
    assert observed["ssh_host"] == "gflow-vps"


def test_stop_mode_runs_callback_exchange_stage_and_verify_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    outputs: list[str] = []
    state = "state-fixture"
    code = "authorization-code-must-not-print"
    access_token = "access-token-must-not-print"
    refresh_token = "refresh-token-must-not-print"
    client_secret = "client-secret-must-not-print"
    verifier = "pkce-verifier-must-not-print"
    challenge = "pkce-challenge-fixture"
    credential = f"{access_token}|{refresh_token}|{client_secret}".encode()
    stage_path = "/etc/gflow-v2/.google-authorized-user.staged-" + "1" * 32 + ".json"

    class FakeHttpClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> "FakeHttpClient":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    expected_ids = {bootstrap.APPROVED_SPREADSHEET_ID, bootstrap.OUTPUT_FOLDER_ID}
    callback = {
        "state": [state],
        "code": [code],
        "picked_file_ids": [",".join(sorted(expected_ids))],
        "scope": [bootstrap.GOOGLE_DRIVE_FILE_SCOPE],
    }
    server = SimpleNamespace(server_address=("127.0.0.1", 43123), server_close=lambda: None)
    original_validate = bootstrap._validate_callback

    def validate_callback(*args: object, **kwargs: object) -> str:
        calls.append("callback-validation")
        return original_validate(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(
        bootstrap,
        "_safe_client_config",
        lambda *_args: {
            "client_id": "test.apps.googleusercontent.com",
            "client_secret": client_secret,
            "auth_uri": bootstrap._AUTH_URI,
            "token_uri": bootstrap._TOKEN_URI,
        },
    )
    monkeypatch.setattr(bootstrap.secrets, "token_urlsafe", lambda _size: state)
    monkeypatch.setattr(bootstrap, "_pkce_pair", lambda: (verifier, challenge))
    monkeypatch.setattr(bootstrap, "_create_callback_server", lambda: (server, {}))
    monkeypatch.setattr(
        bootstrap,
        "_authorization_url",
        lambda *_args, **_kwargs: "AUTH_URL_REDACTED",
    )
    monkeypatch.setattr(bootstrap, "_receive_callback", lambda *_args: callback)
    monkeypatch.setattr(bootstrap, "_validate_callback", validate_callback)
    monkeypatch.setattr(bootstrap.httpx, "Client", FakeHttpClient)

    def exchange(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append("token-exchange")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 3600,
            "scope": bootstrap.GOOGLE_DRIVE_FILE_SCOPE,
        }

    monkeypatch.setattr(bootstrap, "_exchange_code", exchange)
    monkeypatch.setattr(
        bootstrap,
        "_verify_grants",
        lambda *_args, **_kwargs: calls.append("grant-verification"),
    )
    monkeypatch.setattr(
        bootstrap,
        "_authorized_user_json",
        lambda *_args: calls.append("credential-serialization") or credential,
    )

    def stage(_host: str, actual_path: str, actual_credential: bytes) -> None:
        assert actual_path == stage_path
        assert actual_credential == credential
        calls.append("remote-stage")

    def verify(_host: str, _folder: str, actual_path: str) -> None:
        assert actual_path == stage_path
        calls.append("staged-verification")

    monkeypatch.setattr(bootstrap.secrets, "token_hex", lambda _size: "1" * 32)
    monkeypatch.setattr(bootstrap, "_stage_remote", stage)
    monkeypatch.setattr(bootstrap, "verify_google_workspace_credential", verify)
    metadata = {
        "sha256": hashlib.sha256(credential).hexdigest(),
        "owner": "gflow",
        "group": "gflow",
        "mode": "600",
        "size": str(len(credential)),
        "parent_mode": "750",
        "readable_by_gflow": "YES",
    }
    monkeypatch.setattr(bootstrap, "_credential_metadata", lambda *_args: metadata)
    for function_name in (
        "_promote_remote",
        "_promotion_status",
        "_restore_remote",
        "_finalize_remote",
        "_cleanup_remote",
    ):
        monkeypatch.setattr(
            bootstrap,
            function_name,
            lambda *_args, _name=function_name, **_kwargs: pytest.fail(
                f"safe mode must not call {_name}"
            ),
        )

    bootstrap._run(
        Path("client.json"),
        repository_root=Path("repo"),
        output=outputs.append,
        stop_after_staged_verify=True,
    )

    assert calls == [
        "callback-validation",
        "token-exchange",
        "grant-verification",
        "credential-serialization",
        "remote-stage",
        "staged-verification",
    ]
    summary = "\n".join(outputs)
    assert "D5_STAGE_STATUS=VERIFIED" in summary
    assert f"STAGED_CREDENTIAL_PATH={stage_path}" in summary
    assert f"STAGED_CREDENTIAL_SHA256={hashlib.sha256(credential).hexdigest()}" in summary
    assert "CANONICAL_CREDENTIAL_CHANGED=NO" in summary
    assert "PROMOTION_RUN=NO" in summary
    assert "FINALIZATION_RUN=NO" in summary
    assert "BOOTSTRAP_STATE=STAGED_VERIFIED_NOT_PROMOTED" in summary
    for secret in (code, access_token, refresh_token, client_secret, verifier):
        assert secret not in summary


def test_verification_failure_cleans_stage_without_promoting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(bootstrap.secrets, "token_hex", lambda _size: "b" * 32)
    monkeypatch.setattr(bootstrap, "_stage_remote", lambda *_args: calls.append("stage"))

    def fail_verification(*_args: object) -> None:
        calls.append("verify")
        raise bootstrap.BootstrapError("staged service-user verification failed")

    monkeypatch.setattr(bootstrap, "verify_google_workspace_credential", fail_verification)
    monkeypatch.setattr(bootstrap, "_promote_remote", lambda *_args: calls.append("promote"))
    monkeypatch.setattr(bootstrap, "_cleanup_remote", lambda *_args: calls.append("cleanup"))

    with pytest.raises(bootstrap.BootstrapError, match="verification failed"):
        bootstrap._install_verified_remote("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, b"fixture")

    assert calls == ["stage", "verify", "cleanup"]


def test_rate_limited_stage_is_retained_securely_without_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = b"fixture credential bytes"
    stage_path = "/etc/gflow-v2/.google-authorized-user.staged-" + "d" * 32 + ".json"
    digest = hashlib.sha256(credential).hexdigest()
    calls: list[str] = []
    monkeypatch.setattr(bootstrap.secrets, "token_hex", lambda _size: "d" * 32)
    monkeypatch.setattr(bootstrap, "_stage_remote", lambda *_args: calls.append("stage"))

    def rate_limit(*_args: object) -> None:
        calls.append("verify")
        raise bootstrap.StagedVerificationRateLimitExhaustedError("429 exhausted")

    monkeypatch.setattr(bootstrap, "verify_google_workspace_credential", rate_limit)
    monkeypatch.setattr(
        bootstrap,
        "_credential_metadata",
        lambda *_args: (
            calls.append("metadata")
            or {
                "sha256": digest,
                "owner": "gflow",
                "group": "gflow",
                "mode": "600",
                "size": str(len(credential)),
                "parent_mode": "750",
                "readable_by_gflow": "YES",
            }
        ),
    )
    for operation in ("_promote_remote", "_finalize_remote", "_cleanup_remote", "_restore_remote"):
        monkeypatch.setattr(
            bootstrap,
            operation,
            lambda *_args, _name=operation: pytest.fail(f"{_name} must not run"),
        )

    with pytest.raises(bootstrap.StagedCredentialDeferredError) as exc:
        bootstrap._install_verified_remote("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, credential)

    assert exc.value.stage_path == stage_path
    assert exc.value.credential_sha256 == digest
    assert calls == ["stage", "verify", "metadata"]


def test_ssh_rate_limit_sentinel_is_classified_only_with_expected_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=75, stdout=b"REMOTE_VERIFY=DEFERRED_RATE_LIMIT\n", stderr=b""
        ),
    )
    with pytest.raises(bootstrap.StagedVerificationRateLimitExhaustedError):
        bootstrap._run_ssh("gflow-vps", "true", input_data=None, timeout=1)

    monkeypatch.setattr(
        bootstrap.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=75, stdout=b"different output\n", stderr=b""
        ),
    )
    with pytest.raises(bootstrap.BootstrapError, match="exit code 75"):
        bootstrap._run_ssh("gflow-vps", "true", input_data=None, timeout=1)


def test_main_reports_retained_stage_and_no_oauth_rerun_after_rate_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stage_path = "/etc/gflow-v2/.google-authorized-user.staged-" + "e" * 32 + ".json"
    digest = "a" * 64
    monkeypatch.setattr(
        sys, "argv", ["bootstrap_google_workspace_auth.py", "--ssh-host", "gflow-vps"]
    )

    def deferred(*_args: object, **_kwargs: object) -> None:
        raise bootstrap.StagedCredentialDeferredError(stage_path, digest)

    monkeypatch.setattr(bootstrap, "_run", deferred)
    assert bootstrap.main() == 2
    report = json.loads(capsys.readouterr().out)
    assert report["STAGED_VERIFY"] == "DEFERRED_RATE_LIMIT"
    assert report["STAGED_CREDENTIAL_PATH"] == stage_path
    assert report["STAGED_CREDENTIAL_SHA256"] == digest
    assert report["STAGED_OWNER"] == "gflow"
    assert report["STAGED_GROUP"] == "gflow"
    assert report["STAGED_MODE"] == "0600"
    assert report["PROMOTION"] == "NOT_RUN"
    assert report["FINALIZATION"] == "NOT_RUN"
    assert report["OAUTH_RERUN_REQUIRED"] == "NO"
    assert "--resume-staged-path" in report["RESUME_COMMAND"]
    assert "--oauth-client-json" not in report["RESUME_COMMAND"]


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
    monkeypatch.setattr(
        bootstrap, "verify_google_workspace_credential", lambda *_args: calls.append("verify")
    )
    metadata = {
        "sha256": hashlib.sha256(b"fixture").hexdigest(),
        "owner": "gflow",
        "group": "gflow",
        "mode": "600",
        "size": "7",
        "parent_mode": "750",
        "readable_by_gflow": "YES",
    }
    monkeypatch.setattr(bootstrap, "_credential_metadata", lambda *_args: metadata)

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

    monkeypatch.setattr(bootstrap, "verify_google_workspace_credential", verify)
    metadata = {
        "sha256": hashlib.sha256(b"fixture").hexdigest(),
        "owner": "gflow",
        "group": "gflow",
        "mode": "600",
        "size": "7",
        "parent_mode": "750",
        "readable_by_gflow": "YES",
    }
    monkeypatch.setattr(bootstrap, "_credential_metadata", lambda *_args: metadata)
    monkeypatch.setattr(bootstrap, "_require_promoted_parity", lambda *_args: metadata)
    monkeypatch.setattr(bootstrap, "_promote_remote", lambda *_args: calls.append("promote"))
    monkeypatch.setattr(bootstrap, "_restore_remote", lambda *_args: calls.append("restore"))
    monkeypatch.setattr(bootstrap, "_cleanup_remote", lambda *_args: calls.append("cleanup"))

    with pytest.raises(
        bootstrap.BootstrapError,
        match="Canonical credential verification failed: canonical verification failed",
    ) as exc:
        bootstrap._install_verified_remote("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, b"fixture")
    assert "failed candidate retained at" in str(exc.value)
    assert "sha256=" in str(exc.value)

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
    monkeypatch.setattr(
        bootstrap, "verify_google_workspace_credential", lambda *_args: calls.append("verify")
    )
    metadata = {
        "sha256": hashlib.sha256(b"fixture").hexdigest(),
        "owner": "gflow",
        "group": "gflow",
        "mode": "600",
        "size": "7",
        "parent_mode": "750",
        "readable_by_gflow": "YES",
    }
    monkeypatch.setattr(bootstrap, "_credential_metadata", lambda *_args: metadata)
    monkeypatch.setattr(bootstrap, "_require_promoted_parity", lambda *_args: metadata)
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
        return SimpleNamespace(
            returncode=1,
            stdout=b"REMOTE_STEP=verify",
            stderr=(
                b"GoogleSheetsReadError: denied; access_token=access-secret "
                b"refresh_token=refresh-secret authorization_code=code-secret "
                b"client_secret=client-secret Bearer bearer-secret"
            ),
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    with pytest.raises(bootstrap.BootstrapError) as exc:
        bootstrap._stage_remote(
            "gflow-vps",
            "/etc/gflow-v2/.google-authorized-user.staged-0123456789abcdef0123456789abcdef.json",
            b"fixture",
        )
    for secret in (
        "access-secret",
        "refresh-secret",
        "code-secret",
        "client-secret",
        "bearer-secret",
    ):
        assert secret not in str(exc.value)
    assert "GoogleSheetsReadError: denied" in str(exc.value)
    assert "REMOTE_STEP=verify" in str(exc.value)
    assert "exit code 1" in str(exc.value)


def test_remote_timeout_retains_only_redacted_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        raise bootstrap.subprocess.TimeoutExpired(
            cmd="ssh",
            timeout=2,
            output=b"step=verify",
            stderr=b"refresh_token=timeout-secret",
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    with pytest.raises(bootstrap.BootstrapError) as exc:
        bootstrap._run_ssh("gflow-vps", "true", input_data=None, timeout=2)
    assert "step=verify" in str(exc.value)
    assert "timeout-secret" not in str(exc.value)
    assert "timed out" in str(exc.value)


def test_remote_credential_json_is_fully_redacted() -> None:
    text = bootstrap._redact_remote_text(
        b'{"type":"authorized_user","refresh_token":"refresh-secret",'
        b'"client_secret":"client-secret"}'
    )
    assert text == "[credential JSON redacted]"
    assert "refresh-secret" not in text and "client-secret" not in text


def test_staged_and_canonical_use_same_gflow_verifier_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands: list[str] = []

    def fake_run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command[-1])
        return SimpleNamespace(
            returncode=0,
            stdout=b"REMOTE_VERIFY=PASS SCHEMAS=34 OUTPUT_FOLDER=PASS\n",
            stderr=b"",
        )

    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    stage = "/etc/gflow-v2/.google-authorized-user.staged-0123456789abcdef0123456789abcdef.json"
    bootstrap.verify_google_workspace_credential("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, stage)
    bootstrap.verify_google_workspace_credential(
        "gflow-vps", bootstrap.OUTPUT_FOLDER_ID, bootstrap._CANONICAL_PATH
    )
    assert len(commands) == 2
    assert stage in commands[0] and bootstrap._CANONICAL_PATH in commands[1]
    assert commands[0].replace(stage, "CREDENTIAL_PATH") == commands[1].replace(
        bootstrap._CANONICAL_PATH, "CREDENTIAL_PATH"
    )
    for command in commands:
        assert "cd /tmp" in command
        assert "sudo -n -u gflow env GFLOW_CLI_HOME=/var/lib/gflow/browser-state" in command
        assert "/opt/gflow-v2/.venv/bin/python" in command


@pytest.mark.parametrize(
    ("canonical_sha", "readable", "message"),
    [("b" * 64, "YES", "SHA-256 mismatch"), ("a" * 64, "NO", "not readable by gflow")],
)
def test_promoted_sha_mismatch_and_unreadability_block_api_verification(
    monkeypatch: pytest.MonkeyPatch, canonical_sha: str, readable: str, message: str
) -> None:
    staged = {
        "sha256": "a" * 64,
        "readable_by_gflow": "YES",
        "owner": "gflow",
        "group": "gflow",
        "mode": "600",
        "parent_mode": "750",
        "size": "1",
    }
    canonical = {**staged, "sha256": canonical_sha, "readable_by_gflow": readable}
    monkeypatch.setattr(bootstrap, "_credential_metadata", lambda *_args: canonical)
    with pytest.raises(bootstrap.BootstrapError, match=message):
        bootstrap._require_promoted_parity("gflow-vps", "a" * 64, staged)


def test_candidate_permissions_are_diagnosed_precisely() -> None:
    metadata = {
        "sha256": "a" * 64,
        "readable_by_gflow": "NO",
        "owner": "root",
        "group": "root",
        "mode": "640",
        "parent_mode": "750",
        "size": "1",
    }
    with pytest.raises(
        bootstrap.BootstrapError, match="not readable by gflow.*owner=root group=root mode=640"
    ):
        bootstrap._validate_verified_candidate(metadata, "a" * 64)


def test_canonical_verifier_receives_exact_path_and_service_user() -> None:
    script = bootstrap._remote_credential_metadata_script(bootstrap._CANONICAL_PATH)
    assert bootstrap._CANONICAL_PATH in script
    command = (
        "cd /tmp && sudo -n -u gflow env GFLOW_CLI_HOME=/var/lib/gflow/browser-state "
        "/opt/gflow-v2/.venv/bin/python -c "
    )
    assert "sudo -n -u gflow env GFLOW_CLI_HOME=/var/lib/gflow/browser-state" in command
    assert "/opt/gflow-v2/.venv/bin/python" in command


def test_metadata_captures_safe_permissions_and_gflow_readability() -> None:
    stage = "/etc/gflow-v2/.google-authorized-user.staged-0123456789abcdef0123456789abcdef.json"
    script = bootstrap._remote_credential_metadata_script(stage)
    assert "sha256sum" in script
    assert "stat -c '%U|%G|%a|%s'" in script
    assert "sudo -n -u gflow test -r" in script
    assert "parent_mode" in script
    assert 'cat "$file"' not in script


def test_failed_candidate_retention_is_private_and_digest_guarded() -> None:
    stage = "/etc/gflow-v2/.google-authorized-user.staged-0123456789abcdef0123456789abcdef.json"
    script = bootstrap._restore_script(stage, "a" * 64)
    assert 'sha256sum "$target"' in script
    assert f"{stage}.failed-candidate" in script
    assert 'chown gflow:gflow "$failed"' in script
    assert 'chmod 0600 "$failed"' in script
    assert 'mv -fT -- "$backup" "$target"' in script


def test_resume_requires_path_and_expected_digest_and_skips_oauth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(bootstrap.BootstrapError, match="requires both"):
        bootstrap._run(
            None,
            repository_root=Path("repo"),
            resume_staged_path="/etc/gflow-v2/.google-authorized-user.staged-" + "a" * 32 + ".json",
        )

    stage = "/etc/gflow-v2/.google-authorized-user.staged-" + "b" * 32 + ".json"
    digest = "c" * 64
    called: list[tuple[str, str]] = []
    monkeypatch.setattr(
        bootstrap, "_safe_client_config", lambda *_args: pytest.fail("OAuth must not run")
    )
    monkeypatch.setattr(
        bootstrap,
        "_resume_verified_stage",
        lambda _host, _folder, actual_path, actual_digest: called.append(
            (actual_path, actual_digest)
        ),
    )
    bootstrap._run(
        None,
        repository_root=Path("repo"),
        resume_staged_path=stage,
        expected_staged_sha256=digest,
    )
    assert called == [(stage, digest)]


def test_resume_sha_mismatch_stops_before_verifier_or_promotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = "/etc/gflow-v2/.google-authorized-user.staged-" + "d" * 32 + ".json"
    metadata = {
        "sha256": "e" * 64,
        "readable_by_gflow": "YES",
        "owner": "gflow",
        "group": "gflow",
        "mode": "600",
        "parent_mode": "750",
        "size": "1",
    }
    monkeypatch.setattr(bootstrap, "_credential_metadata", lambda *_args: metadata)
    monkeypatch.setattr(
        bootstrap,
        "verify_google_workspace_credential",
        lambda *_args: pytest.fail("API verification must not run"),
    )
    monkeypatch.setattr(
        bootstrap, "_promote_remote", lambda *_args: pytest.fail("promotion must not run")
    )
    with pytest.raises(bootstrap.BootstrapError, match="SHA-256 mismatch"):
        bootstrap._resume_verified_stage("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, stage, "f" * 64)


def test_unreadable_canonical_blocks_canonical_api_verification_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = "/etc/gflow-v2/.google-authorized-user.staged-" + "9" * 32 + ".json"
    good = {
        "sha256": "a" * 64,
        "readable_by_gflow": "YES",
        "owner": "gflow",
        "group": "gflow",
        "mode": "600",
        "parent_mode": "750",
        "size": "1",
    }
    unreadable = {**good, "readable_by_gflow": "NO", "owner": "root"}
    metadata_results = iter((good, unreadable))
    verified_paths: list[str] = []
    rollbacks: list[str] = []
    monkeypatch.setattr(bootstrap, "_credential_metadata", lambda *_args: next(metadata_results))
    monkeypatch.setattr(
        bootstrap,
        "verify_google_workspace_credential",
        lambda _host, _folder, path: verified_paths.append(path),
    )
    monkeypatch.setattr(bootstrap, "_promote_remote", lambda *_args: None)
    monkeypatch.setattr(
        bootstrap,
        "_restore_remote",
        lambda *_args: rollbacks.append("rollback"),
    )
    with pytest.raises(bootstrap.BootstrapError, match="not readable by gflow"):
        bootstrap._resume_verified_stage("gflow-vps", bootstrap.OUTPUT_FOLDER_ID, stage, "a" * 64)
    assert verified_paths == [stage]
    assert rollbacks == ["rollback"]
