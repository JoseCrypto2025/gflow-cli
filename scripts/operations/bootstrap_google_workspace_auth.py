"""Human-consented, least-privilege Google Workspace credential bootstrap."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from gflow_cli.v2_runtime.google_sheets import APPROVED_SPREADSHEET_ID, GOOGLE_DRIVE_FILE_SCOPE

OUTPUT_FOLDER_ID = "1IXhplXVv4ukNhb2EWnAqV2nL2e1V1x0L"
OUTPUT_FOLDER_NAME = "Job_Artifacts"
TARGET_PATH = "/etc/gflow-v2/google-authorized-user.json"
EXPECTED_SHEET_TITLE = "AI_VIDEO_AUTOMATION_MASTER_V2"
EXPECTED_SCHEMA_COUNT = 34
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_REMOTE_INSTALL = (
    "set -eu; d=/etc/gflow-v2; "
    "if test ! -d \"$d\"; then install -d -o root -g gflow -m 0750 \"$d\"; fi; "
    "t=\"$d/.google-authorized-user.$$.tmp\"; trap 'rm -f \"$t\"' EXIT; "
    "umask 077; cat > \"$t\"; chown gflow:gflow \"$t\"; chmod 0600 \"$t\"; "
    "mv -f \"$t\" \"/etc/gflow-v2/google-authorized-user.json\"; "
    "trap - EXIT; stat -c 'OWNER_MODE=%U:%G:%a' \"/etc/gflow-v2/google-authorized-user.json\""
)


class BootstrapError(ValueError):
    """A safe operator-facing bootstrap failure without response or token data."""


def _safe_client_config(path: Path, repository_root: Path) -> dict[str, str]:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise BootstrapError("OAuth client configuration must be an external regular file")
    if path.resolve().is_relative_to(repository_root.resolve()):
        raise BootstrapError("OAuth client configuration must be outside the checkout")
    try:
        document: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BootstrapError("OAuth client configuration is unreadable") from exc
    if not isinstance(document, dict):
        raise BootstrapError("OAuth client configuration has an invalid shape")
    root = cast(dict[str, Any], document)
    installed = root.get("installed")
    if not isinstance(installed, dict):
        raise BootstrapError("OAuth client must be a Google Desktop app client")
    client = cast(dict[str, Any], installed)
    client_id, client_secret = client.get("client_id"), client.get("client_secret")
    if not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com"):
        raise BootstrapError("OAuth Desktop client ID is invalid")
    if not isinstance(client_secret, str) or not client_secret:
        raise BootstrapError("OAuth Desktop client configuration is incomplete")
    auth_uri = client.get("auth_uri", _AUTH_URI)
    token_uri = client.get("token_uri", _TOKEN_URI)
    if auth_uri != _AUTH_URI or token_uri != _TOKEN_URI:
        raise BootstrapError("OAuth endpoint configuration is not Google-approved")
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": auth_uri,
        "token_uri": token_uri,
    }


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge.rstrip(b"=").decode("ascii")


def _authorization_url(
    client_id: str, redirect_uri: str, state: str, code_challenge: str
) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_DRIVE_FILE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "trigger_onepick": "true",
            "allow_multiple": "true",
            "allow_folder_selection": "true",
            "file_ids": f"{APPROVED_SPREADSHEET_ID},{OUTPUT_FOLDER_ID}",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    return f"{_AUTH_URI}?{query}"


def _validate_callback(
    query: Mapping[str, list[str]], *, expected_state: str, expected_ids: set[str]
) -> str:
    states = query.get("state", [])
    if len(states) != 1 or not secrets.compare_digest(states[0], expected_state):
        raise BootstrapError("OAuth callback state validation failed")
    if query.get("error"):
        raise BootstrapError("Google OAuth consent or Picker was cancelled")
    codes, picked = query.get("code", []), query.get("picked_file_ids", [])
    if len(codes) != 1 or not codes[0] or len(picked) != 1:
        raise BootstrapError("Picker callback is incomplete")
    granted = {item for item in picked[0].split(",") if item}
    if granted != expected_ids:
        raise BootstrapError("Picker must grant exactly the approved spreadsheet and output folder")
    scopes = set(" ".join(query.get("scope", [])).split())
    if scopes != {GOOGLE_DRIVE_FILE_SCOPE}:
        raise BootstrapError("Google returned an unexpected OAuth scope set")
    return codes[0]


def _create_callback_server() -> tuple[http.server.HTTPServer, dict[str, object]]:
    result: dict[str, object] = {"query": None, "error": ""}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 — stdlib handler API
            parsed = urlparse(self.path)
            if parsed.path != "/oauth2callback" or len(self.path) > 8192:
                result["error"] = "OAuth callback path or size is invalid"
            else:
                result["query"] = parse_qs(parsed.query, keep_blank_values=True)
            self.send_response(200 if not result["error"] else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(
                b"Authorization response received. You may return to the terminal."
                if not result["error"]
                else b"Authorization callback rejected."
            )

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    server.timeout = 1
    return server, result


def _receive_callback(
    server: http.server.HTTPServer,
    result: dict[str, object],
    *,
    timeout_seconds: int = 600,
) -> dict[str, list[str]]:
    deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < deadline and result["query"] is None and not result["error"]:
            server.handle_request()
    finally:
        server.server_close()
    if result["error"]:
        raise BootstrapError(cast(str, result["error"]))
    query = result["query"]
    if not isinstance(query, dict):
        raise BootstrapError("OAuth consent timed out before callback")
    return cast(dict[str, list[str]], query)


def _exchange_code(
    client: httpx.Client,
    *,
    code: str,
    client_config: Mapping[str, str],
    redirect_uri: str,
    code_verifier: str,
) -> dict[str, Any]:
    try:
        response = client.post(
            _TOKEN_URI,
            data={
                "code": code,
                "client_id": client_config["client_id"],
                "client_secret": client_config["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
        response.raise_for_status()
        value: object = response.json()
    except Exception as exc:  # noqa: BLE001 — OAuth response may contain credentials
        raise BootstrapError("Google authorization-code exchange failed") from exc
    if not isinstance(value, dict):
        raise BootstrapError("Google token response has an invalid shape")
    token = cast(dict[str, Any], value)
    if (
        not isinstance(token.get("access_token"), str)
        or not isinstance(token.get("refresh_token"), str)
        or not isinstance(token.get("expires_in"), int)
        or token.get("scope") != GOOGLE_DRIVE_FILE_SCOPE
    ):
        raise BootstrapError("Google did not return the required offline drive.file grant")
    return token


def _verify_grants(
    client: httpx.Client, *, access_token: str, spreadsheet_id: str, folder_id: str
) -> None:
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        sheet = client.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
            params={"fields": "spreadsheetId,properties(title),sheets.properties(sheetId,title)"},
            headers=headers,
        )
        sheet.raise_for_status()
        sheet_data: object = sheet.json()
        folder = client.get(
            f"https://www.googleapis.com/drive/v3/files/{folder_id}",
            params={"fields": "id,name,mimeType,capabilities(canAddChildren)"},
            headers=headers,
        )
        folder.raise_for_status()
        folder_data: object = folder.json()
    except Exception as exc:  # noqa: BLE001 — response body and headers are sensitive
        raise BootstrapError("Google could not verify the approved file grants") from exc
    if not isinstance(sheet_data, dict) or not isinstance(folder_data, dict):
        raise BootstrapError("Google file verification response is invalid")
    sheet_obj = cast(dict[str, Any], sheet_data)
    folder_obj = cast(dict[str, Any], folder_data)
    sheet_props = sheet_obj.get("properties", {})
    sheets = sheet_obj.get("sheets", [])
    folder_caps = folder_obj.get("capabilities", {})
    if (
        sheet_obj.get("spreadsheetId") != spreadsheet_id
        or not isinstance(sheet_props, dict)
        or sheet_props.get("title") != EXPECTED_SHEET_TITLE
        or not isinstance(sheets, list)
        or len(sheets) != EXPECTED_SCHEMA_COUNT
    ):
        raise BootstrapError("Approved Master spreadsheet identity or schema-tab count did not match")
    if (
        folder_obj.get("id") != folder_id
        or folder_obj.get("mimeType") != "application/vnd.google-apps.folder"
        or not isinstance(folder_caps, dict)
        or folder_caps.get("canAddChildren") is not True
    ):
        raise BootstrapError("Approved V2 output folder is not writable through this grant")


def _authorized_user_json(
    token: Mapping[str, Any], client_config: Mapping[str, str]
) -> bytes:
    from google.oauth2.credentials import Credentials

    expiry = datetime.now(UTC) + timedelta(seconds=cast(int, token["expires_in"]))
    credential = Credentials(
        token=cast(str, token["access_token"]),
        refresh_token=cast(str, token["refresh_token"]),
        token_uri=client_config["token_uri"],
        client_id=client_config["client_id"],
        client_secret=client_config["client_secret"],
        scopes=[GOOGLE_DRIVE_FILE_SCOPE],
        expiry=expiry,
    )
    return credential.to_json().encode("utf-8")


def _install_remote(host: str, credential: bytes) -> None:
    if not host or not all(char.isalnum() or char in "._@-" for char in host):
        raise BootstrapError("SSH host alias is invalid")
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                host,
                "sudo -n sh -c " + shlex.quote(_REMOTE_INSTALL),
            ],
            input=credential,
            capture_output=True,
            timeout=45,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError("Secure credential installation failed") from exc
    if result.returncode != 0 or result.stdout.decode("utf-8", errors="replace").strip() != (
        "OWNER_MODE=gflow:gflow:600"
    ):
        raise BootstrapError("Remote credential owner/mode verification failed")


def _verify_remote_install(host: str, folder_id: str) -> None:
    remote_python = f"""
import httpx
from pathlib import Path
from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    GoogleSheetsApiTransport,
    GoogleSheetsSnapshotSource,
    GoogleWorkspaceCredentialProvider,
)
root = Path('/opt/gflow-v2')
provider = GoogleWorkspaceCredentialProvider(
    Path('{TARGET_PATH}'), repository_root=root
)
transport = GoogleSheetsApiTransport(provider)
try:
    snapshot = GoogleSheetsSnapshotSource(transport, root / 'schemas').read()
finally:
    transport.close()
if snapshot.spreadsheet_id != APPROVED_SPREADSHEET_ID or len(snapshot.tables) != {EXPECTED_SCHEMA_COUNT}:
    raise SystemExit(2)
with httpx.Client(timeout=20.0, follow_redirects=False) as client:
    response = client.get(
        'https://www.googleapis.com/drive/v3/files/{folder_id}',
        params={{'fields': 'id,name,mimeType,capabilities(canAddChildren)'}},
        headers={{'Authorization': 'Bearer ' + provider.access_token()}},
    )
    response.raise_for_status()
    folder = response.json()
if folder.get('id') != '{folder_id}' or folder.get('mimeType') != 'application/vnd.google-apps.folder' or not folder.get('capabilities', {{}}).get('canAddChildren'):
    raise SystemExit(3)
print('REMOTE_VERIFY=PASS SCHEMAS={EXPECTED_SCHEMA_COUNT} OUTPUT_FOLDER=PASS')
"""
    command = (
        "cd /tmp && sudo -n -u gflow env GFLOW_CLI_HOME=/var/lib/gflow/browser-state "
        "/opt/gflow-v2/.venv/bin/python -c " + shlex.quote(remote_python)
    )
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                host,
                command,
            ],
            capture_output=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError("V2 service-user Google API verification failed") from exc
    if result.returncode != 0 or result.stdout.decode("utf-8", errors="replace").strip() != (
        f"REMOTE_VERIFY=PASS SCHEMAS={EXPECTED_SCHEMA_COUNT} OUTPUT_FOLDER=PASS"
    ):
        raise BootstrapError("V2 service-user could not verify the Sheet and output folder")


def _run(
    oauth_client_path: Path,
    *,
    repository_root: Path,
    spreadsheet_id: str = APPROVED_SPREADSHEET_ID,
    folder_id: str = OUTPUT_FOLDER_ID,
    ssh_host: str = "gflow-vps",
    output: Callable[[str], None] = print,
) -> None:
    if spreadsheet_id != APPROVED_SPREADSHEET_ID or folder_id != OUTPUT_FOLDER_ID:
        raise BootstrapError("Only the approved V2 spreadsheet and output folder are permitted")
    client_config = _safe_client_config(oauth_client_path, repository_root)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    server, callback_result = _create_callback_server()
    host, port = cast(tuple[str, int], server.server_address[:2])
    redirect_uri = f"http://{host}:{port}/oauth2callback"
    url = _authorization_url(client_config["client_id"], redirect_uri, state, challenge)
    output("Open this URL in a browser and manually complete Google consent and Picker:")
    output(url)
    try:
        query = _receive_callback(server, callback_result)
    finally:
        server.server_close()
    code = _validate_callback(
        query,
        expected_state=state,
        expected_ids={spreadsheet_id, folder_id},
    )
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            token = _exchange_code(
                client,
                code=code,
                client_config=client_config,
                redirect_uri=redirect_uri,
                code_verifier=verifier,
            )
            _verify_grants(
                client,
                access_token=cast(str, token["access_token"]),
                spreadsheet_id=spreadsheet_id,
                folder_id=folder_id,
            )
        _install_remote(ssh_host, _authorized_user_json(token, client_config))
        _verify_remote_install(ssh_host, folder_id)
    except BootstrapError:
        raise
    except Exception as exc:  # noqa: BLE001 — suppress all credential-bearing details
        raise BootstrapError("Google Workspace credential bootstrap failed") from exc
    output(f"Credential installed and verified at {TARGET_PATH} (owner gflow:gflow, mode 0600).")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap the dedicated V2 Google Workspace OAuth credential."
    )
    parser.add_argument("--oauth-client-json", type=Path, required=True)
    parser.add_argument("--ssh-host", default="gflow-vps")
    args = parser.parse_args()
    try:
        _run(args.oauth_client_json, repository_root=Path(__file__).resolve().parents[2], ssh_host=args.ssh_host)
    except BootstrapError as exc:
        print(json.dumps({"state": "HOLD", "reason_code": "GOOGLE_OAUTH_BOOTSTRAP_FAILED", "detail": str(exc)}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
