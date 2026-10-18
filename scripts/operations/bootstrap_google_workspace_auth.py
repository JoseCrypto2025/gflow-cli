"""Human-consented, least-privilege Google Workspace credential bootstrap."""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import re
import secrets
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx

from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    GOOGLE_DRIVE_FILE_SCOPE,
    google_api_get_with_retry,
)

OUTPUT_FOLDER_ID = "1IXhplXVv4ukNhb2EWnAqV2nL2e1V1x0L"
OUTPUT_FOLDER_NAME = "Job_Artifacts"
TARGET_PATH = "/etc/gflow-v2/google-authorized-user.json"
EXPECTED_SHEET_TITLE = "AI_VIDEO_AUTOMATION_MASTER_V2"
EXPECTED_SCHEMA_COUNT = 34
_TOKEN_URI = "https://oauth2.googleapis.com/token"
_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
_ALLOWED_CLIENT_AUTH_URIS = frozenset(
    {
        _AUTH_URI,
        "https://accounts.google.com/o/oauth2/auth",
    }
)
_STAGED_PATH_RE = re.compile(r"^/etc/gflow-v2/\.google-authorized-user\.staged-[0-9a-f]{32}\.json$")
_REMOTE_ROOT = "/etc/gflow-v2"
_CANONICAL_PATH = f"{_REMOTE_ROOT}/google-authorized-user.json"
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|authorization[_-]?code|"
    r"client[_-]?secret|password|credential(?:s)?[_-]?json)\b\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+")


def _redact_remote_text(value: bytes | str) -> str:
    """Bound remote diagnostics and redact credential-bearing values."""
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    # A serialized authorized-user object is never useful as diagnostic output.
    if re.search(r'(?i)"(?:refresh_token|client_secret|token_uri)"\s*:', text):
        text = "[credential JSON redacted]"
    text = _SENSITIVE_VALUE_RE.sub(r"\1[REDACTED]", text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    return text[:4000]


def _stage_script(stage_path: str) -> str:
    """Create a unique service-owned candidate without touching the canonical file."""
    return f"""set -eu
d={shlex.quote(_REMOTE_ROOT)}
stage={shlex.quote(stage_path)}
if test ! -e "$d"; then install -d -o root -g gflow -m 0750 "$d"; fi
test -d "$d" && test ! -L "$d"
test "$(stat -c '%U:%G:%a' "$d")" = 'root:gflow:750'
test ! -e "$stage" && test ! -L "$stage"
t="$d/.oauth-bootstrap.$$.tmp"
trap 'rm -f -- "$t"' EXIT
umask 077
set -C
cat > "$t"
set +C
chown gflow:gflow "$t"
chmod 0600 "$t"
mv -n -- "$t" "$stage"
test "$(stat -c '%U:%G:%a' "$stage")" = 'gflow:gflow:600'
trap - EXIT
rm -f -- "$t"
printf 'STAGED_OWNER_MODE=gflow:gflow:600\\n'
"""


def _promote_script(stage_path: str, expected_sha256: str) -> str:
    """Promote a verified candidate while retaining rollback until local acknowledgement."""
    backup_path = f"{stage_path}.previous"
    return f"""set -eu
d={shlex.quote(_REMOTE_ROOT)}
stage={shlex.quote(stage_path)}
target={shlex.quote(_CANONICAL_PATH)}
backup={shlex.quote(backup_path)}
expected={shlex.quote(expected_sha256)}
exec 9>/run/lock/gflow-v2-oauth-bootstrap.lock
flock -x 9
test -d "$d" && test ! -L "$d"
test "$(stat -c '%U:%G:%a' "$d")" = 'root:gflow:750'
test -f "$stage" && test ! -L "$stage"
test "$(stat -c '%U:%G:%a' "$stage")" = 'gflow:gflow:600'
for pending in "$d"/.google-authorized-user.staged-*.json.previous; do
  if test -e "$pending" && test "$pending" != "$backup"; then exit 8; fi
done
had_previous=0
promoted=0
cleanup() {{
  if test "$promoted" -eq 1; then
    if test "$had_previous" -eq 1; then mv -fT -- "$backup" "$target"; else rm -f -- "$target"; fi
  fi
  rm -f -- "$stage" "$backup"
}}
trap cleanup EXIT
if test -e "$target" || test -L "$target"; then
  test -f "$target" && test ! -L "$target"
  test "$(stat -c '%U:%G:%a' "$target")" = 'gflow:gflow:600'
  test ! -e "$backup" && test ! -L "$backup"
  cp -- "$target" "$backup"
  chown gflow:gflow "$backup"
  chmod 0600 "$backup"
  had_previous=1
fi
mv -fT -- "$stage" "$target"
promoted=1
test "$(stat -c '%U:%G:%a' "$target")" = 'gflow:gflow:600'
test "$(sha256sum "$target" | cut -d ' ' -f 1)" = "$expected"
promoted=0
trap - EXIT
printf 'PROMOTION_PREPARED=PASS OWNER_MODE=gflow:gflow:600 SHA256=%s\\n' "$expected"
"""


def _cleanup_script(stage_path: str) -> str:
    return f"""set -eu
stage={shlex.quote(stage_path)}
if test -e "$stage" || test -L "$stage"; then
  test -f "$stage" && test ! -L "$stage"
  rm -f -- "$stage"
fi
printf 'STAGE_CLEANUP=PASS\\n'
"""


class BootstrapError(ValueError):
    """A safe operator-facing bootstrap failure without response or token data."""


class StagedVerificationRateLimitExhaustedError(BootstrapError):
    """The remote verifier exhausted its bounded Google API HTTP 429 retries."""


class StagedCredentialDeferredError(BootstrapError):
    """An unchanged, securely staged credential is retained for later verification."""

    def __init__(self, stage_path: str, credential_sha256: str) -> None:
        super().__init__("Staged verification deferred after Google API rate limiting")
        self.stage_path = stage_path
        self.credential_sha256 = credential_sha256


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
    if (
        not isinstance(auth_uri, str)
        or auth_uri not in _ALLOWED_CLIENT_AUTH_URIS
        or token_uri != _TOKEN_URI
    ):
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
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    *,
    include_picker: bool = True,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": GOOGLE_DRIVE_FILE_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if include_picker:
        params.update(
            {
                "trigger_onepick": "true",
                "allow_multiple": "true",
                "allow_folder_selection": "true",
                "file_ids": f"{APPROVED_SPREADSHEET_ID},{OUTPUT_FOLDER_ID}",
            }
        )
    query = urlencode(params)
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


def _validate_pickerless_callback(query: Mapping[str, list[str]], *, expected_state: str) -> str:
    states = query.get("state", [])
    if len(states) != 1 or not secrets.compare_digest(states[0], expected_state):
        raise BootstrapError("OAuth callback state validation failed")
    if query.get("error"):
        raise BootstrapError("Google OAuth consent was cancelled")
    codes = query.get("code", [])
    if len(codes) != 1 or not codes[0]:
        raise BootstrapError("OAuth callback is missing the authorization code")
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

        def log_message(self, format: str, *_args: object) -> None:
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
        sheet = google_api_get_with_retry(
            client,
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}",
            params={"fields": "spreadsheetId,properties(title),sheets.properties(sheetId,title)"},
            headers=headers,
        )
        if not sheet.is_success:
            sheet.raise_for_status()
        sheet_data: object = sheet.json()
        folder = google_api_get_with_retry(
            client,
            f"https://www.googleapis.com/drive/v3/files/{folder_id}",
            params={"fields": "id,name,mimeType,capabilities(canAddChildren)"},
            headers=headers,
        )
        if not folder.is_success:
            folder.raise_for_status()
        folder_data: object = folder.json()
    except Exception as exc:  # noqa: BLE001 — response body and headers are sensitive
        if isinstance(exc, BootstrapError):
            raise
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
        raise BootstrapError(
            "Approved Master spreadsheet identity or schema-tab count did not match"
        )
    if (
        folder_obj.get("id") != folder_id
        or folder_obj.get("mimeType") != "application/vnd.google-apps.folder"
        or not isinstance(folder_caps, dict)
        or folder_caps.get("canAddChildren") is not True
    ):
        raise BootstrapError("Approved V2 output folder is not writable through this grant")


def _raise_google_api_error(response: httpx.Response, resource_label: str) -> None:
    try:
        payload: object = response.json()
    except ValueError:
        payload = None
    detail = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            parts = [
                str(error[key])
                for key in ("code", "status", "message")
                if isinstance(error.get(key), (str, int))
            ]
            details = error.get("errors")
            if isinstance(details, list) and details and isinstance(details[0], dict):
                reason = details[0].get("reason")
                if isinstance(reason, str):
                    parts.append(f"reason={reason}")
            detail = "; ".join(parts)
        elif isinstance(error, str):
            detail = error
    safe_detail = _redact_remote_text(detail) if detail else "no safe error detail returned"
    raise BootstrapError(
        f"Google API access failed for {resource_label}: HTTP {response.status_code}; {safe_detail}"
    )


def _verify_previous_file_grants(
    client: httpx.Client,
    *,
    access_token: str,
    repository_root: Path,
) -> dict[str, str]:
    """Read only the two approved Drive IDs and validate the existing 34-table snapshot."""
    headers = {"Authorization": f"Bearer {access_token}"}

    def drive_file(file_id: str, label: str) -> dict[str, Any]:
        response = google_api_get_with_retry(
            client,
            "https://www.googleapis.com/drive/v3/files/" + quote(file_id, safe=""),
            params={"fields": "id,name,mimeType,isAppAuthorized,capabilities(canAddChildren)"},
            headers=headers,
        )
        if not response.is_success:
            _raise_google_api_error(response, label)
        payload: object = response.json()
        if not isinstance(payload, dict):
            raise BootstrapError(f"Google Drive returned invalid metadata for {label}")
        return cast(dict[str, Any], payload)

    sheet_file = drive_file(APPROVED_SPREADSHEET_ID, "approved Master Sheet Drive file")
    if (
        sheet_file.get("id") != APPROVED_SPREADSHEET_ID
        or sheet_file.get("name") != EXPECTED_SHEET_TITLE
        or sheet_file.get("mimeType") != "application/vnd.google-apps.spreadsheet"
    ):
        raise BootstrapError("Approved Master Sheet Drive metadata did not match")
    sheet_app_authorized = sheet_file.get("isAppAuthorized")
    if sheet_app_authorized is False:
        raise BootstrapError("Approved Master Sheet isAppAuthorized=false")

    folder = drive_file(OUTPUT_FOLDER_ID, "approved Job_Artifacts folder")
    if (
        folder.get("id") != OUTPUT_FOLDER_ID
        or folder.get("name") != OUTPUT_FOLDER_NAME
        or folder.get("mimeType") != "application/vnd.google-apps.folder"
    ):
        raise BootstrapError("Approved Job_Artifacts folder metadata did not match")
    capabilities = folder.get("capabilities")
    can_add_children = (
        capabilities.get("canAddChildren") if isinstance(capabilities, dict) else None
    )
    if can_add_children is not True:
        raise BootstrapError("Approved Job_Artifacts folder lacks canAddChildren")
    folder_app_authorized = folder.get("isAppAuthorized")
    if folder_app_authorized is False:
        raise BootstrapError("Approved Job_Artifacts folder isAppAuthorized=false")

    spreadsheet_response = google_api_get_with_retry(
        client,
        "https://sheets.googleapis.com/v4/spreadsheets/" + quote(APPROVED_SPREADSHEET_ID, safe=""),
        params={"fields": "spreadsheetId,properties(title),sheets.properties(sheetId,title)"},
        headers=headers,
    )
    if not spreadsheet_response.is_success:
        _raise_google_api_error(spreadsheet_response, "approved Master Sheet Sheets API")
    spreadsheet: object = spreadsheet_response.json()
    if not isinstance(spreadsheet, dict):
        raise BootstrapError("Google Sheets returned invalid Master Sheet metadata")
    sheet = cast(dict[str, Any], spreadsheet)
    props = sheet.get("properties")
    tabs = sheet.get("sheets")
    if (
        sheet.get("spreadsheetId") != APPROVED_SPREADSHEET_ID
        or not isinstance(props, dict)
        or props.get("title") != EXPECTED_SHEET_TITLE
        or not isinstance(tabs, list)
        or len(tabs) != EXPECTED_SCHEMA_COUNT
    ):
        raise BootstrapError("Approved Master Sheet identity or 34-tab count did not match")

    return {
        "master_sheet_drive_access": "PASS",
        "master_sheet_sheets_access": "PASS",
        "master_sheet_is_app_authorized": ("YES" if sheet_app_authorized is True else "UNKNOWN"),
        "schema_34_verify": "DEFERRED_TO_STAGED_VERIFY",
        "job_artifacts_access": "PASS",
        "job_artifacts_mime_type": cast(str, folder["mimeType"]),
        "job_artifacts_is_app_authorized": ("YES" if folder_app_authorized is True else "UNKNOWN"),
        "job_artifacts_required_capability": "PASS",
    }


def _authorized_user_json(token: Mapping[str, Any], client_config: Mapping[str, str]) -> bytes:
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


def _validate_ssh_host(host: str) -> None:
    if (
        not host
        or host.startswith("-")
        or not all(char.isalnum() or char in "._@-" for char in host)
    ):
        raise BootstrapError("SSH host alias is invalid")


def _run_ssh(host: str, command: str, *, input_data: bytes | None, timeout: int) -> bytes:
    _validate_ssh_host(host)
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "--",
                host,
                command,
            ],
            input=input_data,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = _redact_remote_text(exc.stdout or b"").strip() or "(empty)"
        stderr = _redact_remote_text(exc.stderr or b"").strip() or "(empty)"
        raise BootstrapError(
            "Secure Google Workspace remote operation timed out; "
            f"stdout: {stdout}; stderr: {stderr}"
        ) from exc
    except OSError as exc:
        raise BootstrapError("Secure Google Workspace remote operation failed") from exc
    if result.returncode != 0:
        stdout = _redact_remote_text(result.stdout).strip() or "(empty)"
        stderr = _redact_remote_text(result.stderr).strip() or "(empty)"
        if result.returncode == 75 and stdout == "REMOTE_VERIFY=DEFERRED_RATE_LIMIT":
            raise StagedVerificationRateLimitExhaustedError(
                "Remote staged verifier exhausted Google API HTTP 429 retries"
            )
        raise BootstrapError(
            "Secure Google Workspace remote operation failed "
            f"(exit code {result.returncode}); stdout: {stdout}; stderr: {stderr}"
        )
    return result.stdout


def _validate_stage_path(stage_path: str) -> None:
    if not _STAGED_PATH_RE.fullmatch(stage_path):
        raise BootstrapError("Staged credential path is invalid")


def _stage_remote(host: str, stage_path: str, credential: bytes) -> None:
    _validate_stage_path(stage_path)
    stdout = _run_ssh(
        host,
        "sudo -n bash -c " + shlex.quote(_stage_script(stage_path)),
        input_data=credential,
        timeout=45,
    )
    if stdout.decode("utf-8", errors="replace").strip() != "STAGED_OWNER_MODE=gflow:gflow:600":
        raise BootstrapError("Staged credential owner/mode verification failed")


def verify_google_workspace_credential(host: str, folder_id: str, credential_path: str) -> None:
    if credential_path != _CANONICAL_PATH:
        _validate_stage_path(credential_path)
    if folder_id != OUTPUT_FOLDER_ID:
        raise BootstrapError("Only the approved V2 output folder is permitted")
    remote_python = f"""
import httpx
from pathlib import Path
from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_SPREADSHEET_ID,
    GoogleSheetsRateLimitExhaustedError,
    GoogleSheetsApiTransport,
    GoogleSheetsSnapshotSource,
    GoogleWorkspaceCredentialProvider,
    google_api_get_with_retry,
)
root = Path('/opt/gflow-v2')
provider = GoogleWorkspaceCredentialProvider(
    Path('{credential_path}'), repository_root=root
)
transport = GoogleSheetsApiTransport(provider)
try:
    try:
        snapshot = GoogleSheetsSnapshotSource(transport, root / 'schemas').read()
        sheet = transport.read_metadata()
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            response = google_api_get_with_retry(
                client,
                'https://www.googleapis.com/drive/v3/files/{folder_id}',
                params={{'fields': 'id,name,mimeType,capabilities(canAddChildren)'}},
                headers={{'Authorization': 'Bearer ' + provider.access_token()}},
            )
            if not response.is_success:
                response.raise_for_status()
            folder = response.json()
    except GoogleSheetsRateLimitExhaustedError:
        print('REMOTE_VERIFY=DEFERRED_RATE_LIMIT', flush=True)
        raise SystemExit(75)
finally:
    transport.close()
if (
    snapshot.spreadsheet_id != APPROVED_SPREADSHEET_ID
    or len(snapshot.tables) != {EXPECTED_SCHEMA_COUNT}
):
    raise SystemExit(2)
if (
    sheet.get('spreadsheetId') != APPROVED_SPREADSHEET_ID
    or sheet.get('properties', {{}}).get('title') != '{EXPECTED_SHEET_TITLE}'
):
    raise SystemExit(4)
if (
    folder.get('id') != '{folder_id}'
    or folder.get('name') != '{OUTPUT_FOLDER_NAME}'
    or folder.get('mimeType') != 'application/vnd.google-apps.folder'
    or folder.get('capabilities', {{}}).get('canAddChildren') is not True
):
    raise SystemExit(3)
print('REMOTE_VERIFY=PASS SCHEMAS={EXPECTED_SCHEMA_COUNT} OUTPUT_FOLDER=PASS')
"""
    command = (
        "cd /tmp && sudo -n -u gflow env GFLOW_CLI_HOME=/var/lib/gflow/browser-state "
        "/opt/gflow-v2/.venv/bin/python -c " + shlex.quote(remote_python)
    )
    stdout = _run_ssh(host, command, input_data=None, timeout=240)
    if stdout.decode("utf-8", errors="replace").strip() != (
        f"REMOTE_VERIFY=PASS SCHEMAS={EXPECTED_SCHEMA_COUNT} OUTPUT_FOLDER=PASS"
    ):
        raise BootstrapError("V2 service-user could not verify the Sheet and output folder")


# Backward-compatible private name for existing callers/tests; all production
# paths below invoke the one public verifier entry point.
_verify_remote_install = verify_google_workspace_credential


def _remote_credential_metadata_script(credential_path: str) -> str:
    if credential_path == _CANONICAL_PATH:
        pass
    else:
        _validate_stage_path(credential_path)
    return f"""set -eu
file={shlex.quote(credential_path)}
test -f "$file" && test ! -L "$file"
readable=NO
if sudo -n -u gflow test -r "$file"; then readable=YES; fi
file_meta=$(stat -c '%U|%G|%a|%s' "$file")
parent_mode=$(stat -c '%a' "$(dirname "$file")")
sha=$(sha256sum "$file" | cut -d ' ' -f 1)
printf 'CREDENTIAL_META|%s|%s|%s|%s|%s|%s|%s\\n' \\
  "$sha" "${{file_meta%%|*}}" "$(printf '%s' "$file_meta" | cut -d '|' -f 2)" \\
  "$(printf '%s' "$file_meta" | cut -d '|' -f 3)" \\
  "$(printf '%s' "$file_meta" | cut -d '|' -f 4)" "$parent_mode" "$readable"
"""


def _credential_metadata(host: str, credential_path: str) -> dict[str, str]:
    script = _remote_credential_metadata_script(credential_path)
    stdout = _run_ssh(
        host,
        "sudo -n bash -c " + shlex.quote(script),
        input_data=None,
        timeout=45,
    )
    fields = stdout.decode("utf-8", errors="replace").strip().split("|")
    if len(fields) != 8 or fields[0] != "CREDENTIAL_META":
        raise BootstrapError("Remote credential metadata response is invalid")
    keys = ("sha256", "owner", "group", "mode", "size", "parent_mode", "readable_by_gflow")
    metadata: dict[str, str] = {key: value for key, value in zip(keys, fields[1:], strict=True)}
    if not re.fullmatch(r"[0-9a-f]{64}", metadata["sha256"]):
        raise BootstrapError("Remote credential SHA-256 metadata is invalid")
    if metadata["readable_by_gflow"] not in {"YES", "NO"}:
        raise BootstrapError("Remote credential readability metadata is invalid")
    return metadata


def _require_promoted_parity(
    host: str, expected_sha256: str, staged_metadata: Mapping[str, str]
) -> dict[str, str]:
    canonical = _credential_metadata(host, _CANONICAL_PATH)
    if canonical["sha256"] != expected_sha256 or canonical["sha256"] != staged_metadata["sha256"]:
        raise BootstrapError(
            "Canonical promotion SHA-256 mismatch "
            f"(expected {expected_sha256}, actual {canonical['sha256']})"
        )
    if canonical["readable_by_gflow"] != "YES":
        raise BootstrapError(
            "Canonical promotion permission defect: credential is not readable by gflow "
            f"(owner={canonical['owner']} group={canonical['group']} mode={canonical['mode']} "
            f"parent_mode={canonical['parent_mode']})"
        )
    if (canonical["owner"], canonical["group"], canonical["mode"]) != ("gflow", "gflow", "600"):
        raise BootstrapError(
            "Canonical promotion owner/mode defect "
            f"(owner={canonical['owner']} group={canonical['group']} mode={canonical['mode']})"
        )
    if canonical["parent_mode"] != "750":
        raise BootstrapError(
            "Canonical promotion parent directory mode defect "
            f"(expected=750 actual={canonical['parent_mode']})"
        )
    return canonical


def _promote_remote(host: str, stage_path: str, expected_sha256: str) -> None:
    _validate_stage_path(stage_path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise BootstrapError("Candidate credential digest is invalid")
    stdout = _run_ssh(
        host,
        "sudo -n bash -c " + shlex.quote(_promote_script(stage_path, expected_sha256)),
        input_data=None,
        timeout=45,
    )
    expected = f"PROMOTION_PREPARED=PASS OWNER_MODE=gflow:gflow:600 SHA256={expected_sha256}"
    if stdout.decode("utf-8", errors="replace").strip() != expected:
        raise BootstrapError("Remote credential promotion acknowledgement is unresolved")


def _promotion_status_script(stage_path: str, expected_sha256: str) -> str:
    return f"""set -eu
target={shlex.quote(_CANONICAL_PATH)}
expected={shlex.quote(expected_sha256)}
if test -f "$target" && test ! -L "$target" \\
  && test "$(stat -c '%U:%G:%a' "$target")" = 'gflow:gflow:600' \\
  && test "$(sha256sum "$target" | cut -d ' ' -f 1)" = "$expected"; then
  printf 'PROMOTION_MATCH=TRUE\\n'
else
  printf 'PROMOTION_MATCH=FALSE\\n'
fi
"""


def _promotion_status(host: str, stage_path: str, expected_sha256: str) -> bool:
    _validate_stage_path(stage_path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise BootstrapError("Candidate credential digest is invalid")
    stdout = _run_ssh(
        host,
        "sudo -n bash -c " + shlex.quote(_promotion_status_script(stage_path, expected_sha256)),
        input_data=None,
        timeout=45,
    )
    value = stdout.decode("utf-8", errors="replace").strip()
    if value not in {"PROMOTION_MATCH=TRUE", "PROMOTION_MATCH=FALSE"}:
        raise BootstrapError("Remote credential promotion state is unresolved")
    return value == "PROMOTION_MATCH=TRUE"


def _finalize_script(stage_path: str, expected_sha256: str) -> str:
    backup_path = f"{stage_path}.previous"
    return f"""set -eu
target={shlex.quote(_CANONICAL_PATH)}
backup={shlex.quote(backup_path)}
expected={shlex.quote(expected_sha256)}
test -f "$target" && test ! -L "$target"
test "$(stat -c '%U:%G:%a' "$target")" = 'gflow:gflow:600'
test "$(sha256sum "$target" | cut -d ' ' -f 1)" = "$expected"
rm -f -- "$backup"
printf 'PROMOTION_FINALIZED=PASS\\n'
"""


def _finalize_remote(host: str, stage_path: str, expected_sha256: str) -> None:
    _validate_stage_path(stage_path)
    stdout = _run_ssh(
        host,
        "sudo -n bash -c " + shlex.quote(_finalize_script(stage_path, expected_sha256)),
        input_data=None,
        timeout=45,
    )
    if stdout.decode("utf-8", errors="replace").strip() != "PROMOTION_FINALIZED=PASS":
        raise BootstrapError("Remote credential promotion finalization is unresolved")


def _restore_script(stage_path: str, expected_sha256: str) -> str:
    backup_path = f"{stage_path}.previous"
    failed_candidate_path = f"{stage_path}.failed-candidate"
    return f"""set -eu
target={shlex.quote(_CANONICAL_PATH)}
backup={shlex.quote(backup_path)}
failed={shlex.quote(failed_candidate_path)}
expected={shlex.quote(expected_sha256)}
if test -f "$target" && test ! -L "$target" \\
  && test "$(sha256sum "$target" | cut -d ' ' -f 1)" = "$expected"; then
  test ! -e "$failed" && test ! -L "$failed"
  cp -- "$target" "$failed"
  chown gflow:gflow "$failed"
  chmod 0600 "$failed"
fi
if test -e "$backup"; then
  test -f "$backup" && test ! -L "$backup"
  test "$(stat -c '%U:%G:%a' "$backup")" = 'gflow:gflow:600'
  mv -fT -- "$backup" "$target"
  printf 'CANONICAL_RESTORED=PASS\\n'
elif test -f "$target" && test ! -L "$target" \\
  && test "$(sha256sum "$target" | cut -d ' ' -f 1)" = "$expected"; then
  rm -f -- "$target"
  printf 'CANONICAL_REMOVED=PASS\\n'
else
  printf 'CANONICAL_UNCHANGED=PASS\\n'
fi
"""


def _restore_remote(host: str, stage_path: str, expected_sha256: str) -> None:
    _validate_stage_path(stage_path)
    stdout = _run_ssh(
        host,
        "sudo -n bash -c " + shlex.quote(_restore_script(stage_path, expected_sha256)),
        input_data=None,
        timeout=45,
    )
    if stdout.decode("utf-8", errors="replace").strip() not in {
        "CANONICAL_RESTORED=PASS",
        "CANONICAL_REMOVED=PASS",
        "CANONICAL_UNCHANGED=PASS",
    }:
        raise BootstrapError("Canonical credential rollback could not be verified")


def _cleanup_remote(host: str, stage_path: str) -> None:
    _validate_stage_path(stage_path)
    stdout = _run_ssh(
        host,
        "sudo -n bash -c " + shlex.quote(_cleanup_script(stage_path)),
        input_data=None,
        timeout=45,
    )
    if stdout.decode("utf-8", errors="replace").strip() != "STAGE_CLEANUP=PASS":
        raise BootstrapError("Staged credential cleanup could not be verified")


def _install_verified_remote(
    host: str,
    folder_id: str,
    credential: bytes,
    *,
    stop_after_staged_verify: bool = False,
) -> tuple[str, str]:
    """Stage, service-verify, then promote; cleanup the candidate on every HOLD path."""
    stage_path = f"{_REMOTE_ROOT}/.google-authorized-user.staged-{secrets.token_hex(16)}.json"
    credential_sha256 = hashlib.sha256(credential).hexdigest()
    promotion_attempted = False
    promoted = False
    canonical_verified = False
    staged_metadata: dict[str, str] | None = None
    canonical_metadata: dict[str, str] | None = None
    try:
        _stage_remote(host, stage_path, credential)
        verify_google_workspace_credential(host, folder_id, stage_path)
        staged_metadata = _credential_metadata(host, stage_path)
        _validate_verified_candidate(staged_metadata, credential_sha256)
        if stop_after_staged_verify:
            # Successful stop mode intentionally retains this verified credential.
            # Returning here bypasses all promotion and failure-cleanup handling.
            return stage_path, credential_sha256
        promotion_attempted = True
        try:
            _promote_remote(host, stage_path, credential_sha256)
            promoted = True
        except BootstrapError:
            # An SSH disconnect can lose only the acknowledgement after remote rename.
            promoted = _promotion_status(host, stage_path, credential_sha256)
            if not promoted:
                raise
        if not promoted:
            raise BootstrapError("Remote credential promotion was not confirmed")
        canonical_metadata = _require_promoted_parity(host, credential_sha256, staged_metadata)
        try:
            verify_google_workspace_credential(host, folder_id, _CANONICAL_PATH)
        except BootstrapError as exc:
            safe_meta = " ".join(
                f"{key}={canonical_metadata[key]}"
                for key in (
                    "sha256",
                    "size",
                    "owner",
                    "group",
                    "mode",
                    "parent_mode",
                    "readable_by_gflow",
                )
            )
            raise BootstrapError(
                f"Canonical credential verification failed: {exc}; {safe_meta}; "
                f"failed candidate retained at {stage_path}.failed-candidate"
            ) from exc
        canonical_verified = True
        _finalize_remote(host, stage_path, credential_sha256)
    except StagedVerificationRateLimitExhaustedError as exc:
        try:
            staged_metadata = _credential_metadata(host, stage_path)
            _validate_verified_candidate(staged_metadata, credential_sha256)
        except BootstrapError as metadata_error:
            try:
                _cleanup_remote(host, stage_path)
            except BootstrapError as cleanup_error:
                raise BootstrapError(
                    "OAuth bootstrap is on HOLD; rate-limited stage cleanup must be reconciled"
                ) from cleanup_error
            raise BootstrapError(
                "Rate-limited staged credential failed secure metadata validation and was removed"
            ) from metadata_error
        raise StagedCredentialDeferredError(stage_path, credential_sha256) from exc
    except Exception as exc:
        # If SSH lost the promotion acknowledgement or the follow-up status
        # probe, ``promoted`` may still be false even though the remote rename
        # completed.  The restore operation is digest guarded: it restores the
        # backup when present, removes only our candidate, and otherwise leaves
        # an unrelated canonical credential untouched.
        if promotion_attempted and not canonical_verified:
            try:
                _restore_remote(host, stage_path, credential_sha256)
            except BootstrapError as restore_exc:
                raise BootstrapError(
                    "OAuth bootstrap is on HOLD; canonical credential rollback must be reconciled"
                ) from restore_exc
        try:
            _cleanup_remote(host, stage_path)
        except BootstrapError as cleanup_exc:
            raise BootstrapError(
                "OAuth bootstrap is on HOLD; staged credential cleanup must be reconciled"
            ) from cleanup_exc
        if isinstance(exc, BootstrapError):
            raise
        raise BootstrapError("Google Workspace credential bootstrap failed safely") from exc
    return stage_path, credential_sha256


def _validate_verified_candidate(metadata: Mapping[str, str], expected_sha256: str) -> None:
    if metadata["sha256"] != expected_sha256:
        raise BootstrapError(
            f"Verified staged credential SHA-256 mismatch (expected {expected_sha256}, "
            f"actual {metadata['sha256']})"
        )
    if metadata["readable_by_gflow"] != "YES":
        raise BootstrapError(
            "Verified staged credential is not readable by gflow "
            f"(owner={metadata['owner']} group={metadata['group']} mode={metadata['mode']} "
            f"parent_mode={metadata['parent_mode']})"
        )
    if (metadata["owner"], metadata["group"], metadata["mode"]) != ("gflow", "gflow", "600"):
        raise BootstrapError(
            "Verified staged credential owner/mode defect "
            f"(owner={metadata['owner']} group={metadata['group']} mode={metadata['mode']})"
        )
    if metadata["parent_mode"] != "750":
        raise BootstrapError(
            "Verified staged credential parent directory mode defect "
            f"(expected=750 actual={metadata['parent_mode']})"
        )


def _resume_verified_stage(
    host: str,
    folder_id: str,
    staged_path: str,
    expected_sha256: str,
) -> None:
    """Promote only an explicitly named, unchanged, fully re-verified stage."""
    _validate_stage_path(staged_path)
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise BootstrapError("Expected staged SHA-256 is required and must be lowercase hex")
    if folder_id != OUTPUT_FOLDER_ID:
        raise BootstrapError("Only the approved V2 output folder is permitted")
    promoted = False
    canonical_verified = False
    try:
        staged_metadata = _credential_metadata(host, staged_path)
        _validate_verified_candidate(staged_metadata, expected_sha256)
        verify_google_workspace_credential(host, folder_id, staged_path)
        try:
            _promote_remote(host, staged_path, expected_sha256)
            promoted = True
        except BootstrapError:
            promoted = _promotion_status(host, staged_path, expected_sha256)
            if not promoted:
                raise
        canonical_metadata = _require_promoted_parity(host, expected_sha256, staged_metadata)
        try:
            verify_google_workspace_credential(host, folder_id, _CANONICAL_PATH)
        except BootstrapError as exc:
            safe_meta = " ".join(
                f"{key}={canonical_metadata[key]}"
                for key in (
                    "sha256",
                    "size",
                    "owner",
                    "group",
                    "mode",
                    "parent_mode",
                    "readable_by_gflow",
                )
            )
            raise BootstrapError(
                f"Canonical credential verification failed: {exc}; {safe_meta}; "
                f"failed candidate retained at {staged_path}.failed-candidate"
            ) from exc
        canonical_verified = True
        _finalize_remote(host, staged_path, expected_sha256)
    except Exception as exc:
        if promoted and not canonical_verified:
            try:
                _restore_remote(host, staged_path, expected_sha256)
            except BootstrapError as restore_exc:
                raise BootstrapError(
                    "Resume promotion is on HOLD; canonical rollback must be reconciled"
                ) from restore_exc
        if isinstance(exc, BootstrapError):
            raise
        raise BootstrapError("Verified staged credential promotion failed safely") from exc


def _run(
    oauth_client_path: Path | None,
    *,
    repository_root: Path,
    spreadsheet_id: str = APPROVED_SPREADSHEET_ID,
    folder_id: str = OUTPUT_FOLDER_ID,
    ssh_host: str = "gflow-vps",
    output: Callable[[str], None] = print,
    stop_after_staged_verify: bool = False,
    resume_staged_path: str | None = None,
    expected_staged_sha256: str | None = None,
    pickerless_reuse_test: bool = False,
) -> None:
    if resume_staged_path is not None or expected_staged_sha256 is not None:
        if resume_staged_path is None or expected_staged_sha256 is None:
            raise BootstrapError(
                "Resume requires both --resume-staged-path and --expected-staged-sha256"
            )
        if oauth_client_path is not None or stop_after_staged_verify:
            raise BootstrapError(
                "Resume cannot be combined with OAuth client input or stage-only mode"
            )
        _resume_verified_stage(ssh_host, folder_id, resume_staged_path, expected_staged_sha256)
        output("D5_RESUME_PROMOTION=FINALIZED")
        return
    if pickerless_reuse_test and not stop_after_staged_verify:
        raise BootstrapError("Pickerless previous-grant test requires stage-only mode")
    if oauth_client_path is None:
        raise BootstrapError("--oauth-client-json is required unless resuming a staged credential")
    if spreadsheet_id != APPROVED_SPREADSHEET_ID or folder_id != OUTPUT_FOLDER_ID:
        raise BootstrapError("Only the approved V2 spreadsheet and output folder are permitted")
    client_config = _safe_client_config(oauth_client_path, repository_root)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    server, callback_result = _create_callback_server()
    host, port = cast(tuple[str, int], server.server_address[:2])
    redirect_uri = f"http://{host}:{port}/oauth2callback"
    url = _authorization_url(
        client_config["client_id"],
        redirect_uri,
        state,
        challenge,
        include_picker=not pickerless_reuse_test,
    )
    output("CALLBACK_LISTENER_ACTIVE=YES")
    output(
        "Open this URL and complete Google consent:"
        if pickerless_reuse_test
        else "Open this URL in a browser and manually complete Google consent and Picker:"
    )
    output(url)
    try:
        query = _receive_callback(server, callback_result)
    finally:
        server.server_close()
    code = (
        _validate_pickerless_callback(query, expected_state=state)
        if pickerless_reuse_test
        else _validate_callback(
            query,
            expected_state=state,
            expected_ids={spreadsheet_id, folder_id},
        )
    )
    grant_report: dict[str, str] = {}
    try:
        with httpx.Client(timeout=20.0, follow_redirects=False) as client:
            token = _exchange_code(
                client,
                code=code,
                client_config=client_config,
                redirect_uri=redirect_uri,
                code_verifier=verifier,
            )
            if pickerless_reuse_test:
                grant_report = _verify_previous_file_grants(
                    client,
                    access_token=cast(str, token["access_token"]),
                    repository_root=repository_root,
                )
            else:
                _verify_grants(
                    client,
                    access_token=cast(str, token["access_token"]),
                    spreadsheet_id=spreadsheet_id,
                    folder_id=folder_id,
                )
        stage_path, credential_sha256 = _install_verified_remote(
            ssh_host,
            folder_id,
            _authorized_user_json(token, client_config),
            stop_after_staged_verify=stop_after_staged_verify,
        )
        if pickerless_reuse_test:
            grant_report["schema_34_verify"] = "PASS"
    except BootstrapError:
        raise
    except Exception as exc:  # noqa: BLE001 — suppress all credential-bearing details
        raise BootstrapError("Google Workspace credential bootstrap failed") from exc
    if stop_after_staged_verify:
        if pickerless_reuse_test:
            output("AUTH_MODE=PICKERLESS")
            output("PREVIOUS_FILE_GRANTS_REUSABLE=YES")
            for key, value in grant_report.items():
                output(f"{key.upper()}={value}")
        output("D5_STAGE_STATUS=VERIFIED")
        output(f"STAGED_CREDENTIAL_PATH={stage_path}")
        output(f"STAGED_CREDENTIAL_SHA256={credential_sha256}")
        output("CANONICAL_CREDENTIAL_CHANGED=NO")
        output("PROMOTION_RUN=NO")
        output("FINALIZATION_RUN=NO")
        output("BOOTSTRAP_STATE=STAGED_VERIFIED_NOT_PROMOTED")
        return
    output(f"Credential installed and verified at {TARGET_PATH} (owner gflow:gflow, mode 0600).")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Bootstrap the dedicated V2 Google Workspace OAuth credential."
    )
    parser.add_argument("--oauth-client-json", type=Path)
    parser.add_argument("--ssh-host", default="gflow-vps")
    parser.add_argument("--resume-staged-path")
    parser.add_argument("--expected-staged-sha256")
    parser.add_argument(
        "--pickerless-reuse-test",
        action="store_true",
        help=(
            "reauthorize with only drive.file, directly test the two approved IDs, "
            "and require --stop-after-staged-verify"
        ),
    )
    parser.add_argument(
        "--stop-after-staged-verify",
        action="store_true",
        help=(
            "retain the service-verified staged credential and stop before canonical "
            "promotion or finalization"
        ),
    )
    args = parser.parse_args()
    try:
        _run(
            args.oauth_client_json,
            repository_root=Path(__file__).resolve().parents[2],
            ssh_host=args.ssh_host,
            stop_after_staged_verify=args.stop_after_staged_verify,
            resume_staged_path=args.resume_staged_path,
            expected_staged_sha256=args.expected_staged_sha256,
            pickerless_reuse_test=args.pickerless_reuse_test,
        )
    except StagedCredentialDeferredError as exc:
        print(
            json.dumps(
                {
                    "state": "HOLD",
                    "reason_code": "GOOGLE_SHEETS_RATE_LIMIT_DEFERRED",
                    "detail": str(exc),
                    "STAGED_VERIFY": "DEFERRED_RATE_LIMIT",
                    "STAGED_CREDENTIAL_PATH": exc.stage_path,
                    "STAGED_CREDENTIAL_SHA256": exc.credential_sha256,
                    "STAGED_OWNER": "gflow",
                    "STAGED_GROUP": "gflow",
                    "STAGED_MODE": "0600",
                    "PROMOTION": "NOT_RUN",
                    "FINALIZATION": "NOT_RUN",
                    "OAUTH_RERUN_REQUIRED": "NO",
                    "RESUME_COMMAND": (
                        "uv run python scripts/operations/bootstrap_google_workspace_auth.py "
                        f"--resume-staged-path {exc.stage_path} "
                        f"--expected-staged-sha256 {exc.credential_sha256} "
                        f"--ssh-host {args.ssh_host}"
                    ),
                },
                separators=(",", ":"),
            )
        )
        return 2
    except BootstrapError as exc:
        print(
            json.dumps(
                {
                    "state": "HOLD",
                    "reason_code": "GOOGLE_OAUTH_BOOTSTRAP_FAILED",
                    "detail": str(exc),
                }
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
