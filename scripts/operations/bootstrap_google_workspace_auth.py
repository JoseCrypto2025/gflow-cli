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
_ALLOWED_CLIENT_AUTH_URIS = frozenset(
    {
        _AUTH_URI,
        "https://accounts.google.com/o/oauth2/auth",
    }
)
_STAGED_PATH_RE = re.compile(r"^/etc/gflow-v2/\.google-authorized-user\.staged-[0-9a-f]{32}\.json$")
_REMOTE_ROOT = "/etc/gflow-v2"
_CANONICAL_PATH = f"{_REMOTE_ROOT}/google-authorized-user.json"


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


def _authorization_url(client_id: str, redirect_uri: str, state: str, code_challenge: str) -> str:
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
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BootstrapError("Secure Google Workspace remote operation failed") from exc
    if result.returncode != 0:
        raise BootstrapError("Secure Google Workspace remote operation failed")
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


def _verify_remote_install(host: str, folder_id: str, credential_path: str) -> None:
    if credential_path != _CANONICAL_PATH:
        _validate_stage_path(credential_path)
    if folder_id != OUTPUT_FOLDER_ID:
        raise BootstrapError("Only the approved V2 output folder is permitted")
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
    Path('{credential_path}'), repository_root=root
)
transport = GoogleSheetsApiTransport(provider)
try:
    snapshot = GoogleSheetsSnapshotSource(transport, root / 'schemas').read()
finally:
    transport.close()
if (
    snapshot.spreadsheet_id != APPROVED_SPREADSHEET_ID
    or len(snapshot.tables) != {EXPECTED_SCHEMA_COUNT}
):
    raise SystemExit(2)
with httpx.Client(timeout=20.0, follow_redirects=False) as client:
    sheet_response = client.get(
        'https://sheets.googleapis.com/v4/spreadsheets/' + APPROVED_SPREADSHEET_ID,
        params={{'fields': 'spreadsheetId,properties(title)'}},
        headers={{'Authorization': 'Bearer ' + provider.access_token()}},
    )
    sheet_response.raise_for_status()
    sheet = sheet_response.json()
    response = client.get(
        'https://www.googleapis.com/drive/v3/files/{folder_id}',
        params={{'fields': 'id,name,mimeType,capabilities(canAddChildren)'}},
        headers={{'Authorization': 'Bearer ' + provider.access_token()}},
    )
    response.raise_for_status()
    folder = response.json()
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
    return f"""set -eu
target={shlex.quote(_CANONICAL_PATH)}
backup={shlex.quote(backup_path)}
expected={shlex.quote(expected_sha256)}
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


def _install_verified_remote(host: str, folder_id: str, credential: bytes) -> None:
    """Stage, service-verify, then promote; cleanup the candidate on every HOLD path."""
    stage_path = f"{_REMOTE_ROOT}/.google-authorized-user.staged-{secrets.token_hex(16)}.json"
    credential_sha256 = hashlib.sha256(credential).hexdigest()
    promotion_attempted = False
    promoted = False
    canonical_verified = False
    try:
        _stage_remote(host, stage_path, credential)
        _verify_remote_install(host, folder_id, stage_path)
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
        try:
            _verify_remote_install(host, folder_id, _CANONICAL_PATH)
            canonical_verified = True
            _finalize_remote(host, stage_path, credential_sha256)
        except BootstrapError as exc:
            raise BootstrapError(
                "Credential promotion is on HOLD; canonical rollback must be reconciled"
            ) from exc
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
        _install_verified_remote(ssh_host, folder_id, _authorized_user_json(token, client_config))
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
        _run(
            args.oauth_client_json,
            repository_root=Path(__file__).resolve().parents[2],
            ssh_host=args.ssh_host,
        )
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
