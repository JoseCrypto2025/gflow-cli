# V2 Google Workspace OAuth and Picker bootstrap

## Purpose and boundaries

The D5 source uses a dedicated Google OAuth user credential. It does not use
the gflow Flow browser profile, Flow cookies, Chrome profile databases, n8n
credentials, Google Drive connector credentials, or a human-authored snapshot.
The credential supports bounded reads of the approved Master Sheet, future
bounded Sheet writes, and upload/verification in one existing output folder.

The bootstrap is an operator-only one-time tool. The human completes Google
sign-in, consent, and Picker selection; the tool exchanges the authorization
code, verifies the selected resources, and transfers the credential over
strict-host-key-checked SSH stdin. It never automates password or MFA entry and
does not write credential content to a local file, command line, environment,
log, or report.

## Google Cloud configuration

Use a dedicated Cloud project and OAuth Desktop application client. Do not
reuse n8n, Flow, or connector credentials.

Enable these APIs in that project:

1. Google Sheets API.
2. Google Drive API.
3. Google Picker API.

Configure the OAuth audience for the Google account that owns or can use the
approved resources. A consumer Google account requires **External**. Add the
account as a test user while testing. Set publishing status to **Production**
before unattended use; an External app left in Testing issues refresh tokens
that expire after seven days for non-basic scopes.

Request only:

```text
https://www.googleapis.com/auth/drive.file
```

Google classifies drive.file as non-sensitive, per-file access supported by
Sheets. The desktop/mobile Picker authorization flow permits only drive.file;
do not combine another scope into that Picker consent. Google says an app that
uses only non-sensitive scopes is not required to complete OAuth app
verification. Publishing status and verification status are separate: a
Production publishing state does not itself mean the app is verified, and
separate brand or configuration requirements may still appear in Cloud Console.
For this personal, limited-user use, do not assume full app verification is
required; check the exact Console configuration before consent. Sensitive/
restricted-scope verification and the restricted-scope security assessment do
not apply to drive.file alone.

Production publishing avoids the seven-day Testing expiry, but it does not
guarantee a refresh token can never expire. Revocation, six months of
inactivity, token-count limits, account policies, and other documented causes
can invalidate a refresh token.

## Existing resources and grant contract

The script is pinned to these existing resources and refuses different IDs:

| Resource | ID | Required verification |
|---|---|---|
| Master spreadsheet | `1wtoIBfeHNdGYK6cyhDfKPG6W3TnnfL4QDvCAtJBX27Q` | Exact title `AI_VIDEO_AUTOMATION_MASTER_V2`; 34 tabs |
| V2 output folder | `1IXhplXVv4ukNhb2EWnAqV2nL2e1V1x0L` | Existing `Job_Artifacts` folder; Drive reports `canAddChildren=true` |

The output folder already exists and was observed empty during this recovery.
Do not create another folder or put test files in it.

The human must select exactly the spreadsheet and folder above in the Google
Picker. The authorization URL uses `trigger_onepick=true`,
`prompt=consent`, `allow_multiple=true`, `allow_folder_selection=true`,
and filters the Picker to those two IDs. The tool checks the exact returned ID
set and verifies the Sheet and writable folder through the APIs before sending
credential bytes to the VPS. The candidate is then staged; the canonical
credential is not touched until the service-user verification succeeds.

The OAuth consent screen itself does not return an identity claim under the
Picker's single `drive.file` scope. During the manual account chooser, use the
same intended Google account verified for `gflow-prod`; the script never
claims that Picker IDs prove the Flow account identity.

## Human bootstrap procedure

1. In Google Cloud Console, confirm the dedicated project has Sheets, Drive,
   and Picker APIs enabled; the OAuth audience and publishing status are
   appropriate for unattended use; and the required account is allowed to
   consent.
2. Create or download an OAuth **Desktop app** client JSON. Keep it outside
   `D:\Gflow-CLI`; never commit or attach it to a ticket.
3. After the reviewed V2 code has been deployed, run on the authorized
   Windows workstation:

   ```powershell
   cd D:\Gflow-CLI
   uv run python scripts/operations/bootstrap_google_workspace_auth.py `
     --oauth-client-json "C:\path\outside-repository\oauth-client.json" `
     --stop-after-staged-verify `
     --ssh-host gflow-vps
   ```

4. Open the one-time URL printed by the tool in a browser on this workstation;
   do not paste or share the URL. Manually choose the
   same Google account that owns the verified `gflow-prod` identity, consent
   to `drive.file`, then select the Master spreadsheet and existing
   `Job_Artifacts` folder in Picker. Do not enter passwords or MFA into the
   terminal or the bootstrap process.
5. The loopback callback listens only on `127.0.0.1`. The tool checks OAuth
   state and PKCE, requires exactly the two approved Picker IDs and the one
   reviewed scope, and verifies the Sheet identity and writable folder before
   streaming credential bytes over SSH stdin.
6. The remote installer creates a unique staged file on the same filesystem
   as the canonical path, owned by `gflow:gflow`, mode `0600`. It runs the
   deployed V2 source as `gflow` against that staged file, checks all 34 local
   schemas and the exact spreadsheet title, then checks the exact existing
   `Job_Artifacts` folder identity, type, and `canAddChildren` capability. The
   schema snapshot uses one ordered `values.batchGet` request with 34 explicit,
   bounded `TAB!A1:AZ1001` ranges; the existing header, overflow, row, secret,
   key, foreign-key, and semantic checks still run for every table. The
   pickerless pre-stage check verifies file grants and Sheet identity, then
   leaves the one full schema snapshot to staged-credential verification.
   Safe Google API GETs retry HTTP 429 and 5xx responses sequentially, up to
   seven attempts, honoring a valid `Retry-After` value within a 90-second
   total wait budget; fallback delays use bounded jitter around 1, 2, 4, 8,
   16, and 32 seconds. HTTP 401/403, writes, and OAuth token exchange are not
   retried.
7. The controlled D5 run uses `--stop-after-staged-verify`. After staged
   verification, it prints `D5_STAGE_STATUS=VERIFIED`, the staged path and
   SHA-256, and confirms canonical credentials were not changed. The staged
   file is retained as `gflow:gflow`, mode `0600`; record its path and SHA
   securely, then stop for explicit promotion approval.
8. After approval, resume without OAuth using both the exact staged path and
   its reviewed SHA-256:

   ```powershell
   uv run python scripts/operations/bootstrap_google_workspace_auth.py `
     --resume-staged-path "/etc/gflow-v2/.google-authorized-user.staged-<32-hex>.json" `
     --expected-staged-sha256 "<64-lowercase-hex>" `
     --ssh-host gflow-vps
   ```

   Resume rechecks the stage digest, ownership, mode, parent directory, and
   readability as `gflow`, then uses the same Sheet/schema/folder verifier as
   the staged pass. It refuses a missing, changed, unsafe, or arbitrary file.
   It does not load OAuth client configuration or start consent.
9. Promotion is serialized with a file lock. Before and after promotion the
   tool compares SHA-256 and safe metadata, and requires the canonical file to
   be readable by `gflow` before making Google API calls. Remote failures retain
   bounded stdout/stderr after secret redaction. If canonical API verification
   fails, metadata and sanitized diagnostics are reported, rollback is
   digest-guarded, and the failed candidate is retained at mode `0600` for
   diagnosis when safe. Never print or copy that file's contents. Inspect its
   state before any subsequent operation.

The local bootstrap script runs on the Windows workstation; the remote install
contract is Linux-specific and targets only the configured `gflow-vps` host.
It is an operator script, not a CLI or MCP generation surface.

The only environment setting is a path:

```text
GFLOW_GOOGLE_WORKSPACE_CREDENTIALS=/etc/gflow-v2/google-authorized-user.json
```

Never place credential JSON in an environment file or pass it on a command
line. The local OAuth client configuration and authorized-user data must remain
outside Git. The generated authorized-user file contains refresh-token
material and is treated as a credential.

## Failure and rotation

- Missing/wrong Picker IDs, wrong Sheet title/tab count, inaccessible output
  folder, scope drift, state mismatch, timeout, API failure, unsafe remote
  directory, or stage/promotion permission mismatch blocks installation.
- OAuth/API errors retain bounded diagnostic text after redacting credential
  JSON, authorization codes, access/refresh tokens, bearer values, and client
  secrets; review output before sharing it.
- If remote staged verification fails, the candidate is removed and the prior
  canonical credential remains untouched. D5 remains held. Do not attempt Flow
  or Generate.
- If staged verification exhausts retries on HTTP 429, the bootstrap reports
  `STAGED_VERIFY=DEFERRED_RATE_LIMIT` and retains the unpromoted staged
  credential only after checking that its SHA-256 is unchanged, its owner and
  group are `gflow:gflow`, its mode is `0600`, its parent mode is `0750`, and
  `gflow` can read it. The report includes the stage path and digest, states
  `OAUTH_RERUN_REQUIRED=NO`, and gives the existing `--resume-staged-path` /
  `--expected-staged-sha256` command to retry verification later. Resume always
  reruns complete staged verification before promotion. No promotion or
  finalization occurs on the deferred run. Other verification failures retain
  the existing candidate cleanup and canonical rollback behavior.
- Revoke and replace the OAuth grant after suspected exposure. Keep the V2
  credential readable only by `gflow`.
- The D5 live source re-reads and validates all schema tables before opening
  Flow. It assigns a UTC RFC3339 `read_at` only after successful reads and
  validation.

## Timestamp defect recovery

The malformed `UTCZ` values did not come from a V2 timestamp producer. The
operator-side conversion kept the displayed ` UTC` suffix while changing the
date/time separator to `T` and appending `Z`. The affected values mapped
unambiguously to the same UTC instants; historical prompt metadata was
normalized only after the old prompt became `SUPERSEDED`. V2 writers now use a
shared aware-datetime formatter, and the schema validator accepts only
canonical UTC RFC3339 timestamps ending in one `Z`.

## Current status

As of 2026-10-19, the canonical credential exists at
`/etc/gflow-v2/google-authorized-user.json`, owned by `gflow:gflow` with mode
`0600`, and canonical Google API verification passes. Fresh live verification
confirmed Master Sheet access, all 34 schema validations, and `Job_Artifacts`
access. The deployed build manifest reports version `0.85.0` at commit
`42787383505b43394b3657b5138d137a259c3185`. The earlier F5 attempt stopped
before Flow because `SPV_000006` version 6 had a stale `Source_Context_SHA256`.
After the immutable prompt lifecycle produced and approved `SPV_000007`, the
2026-10-19 live F5 presubmit passed against fresh Phase E. OAuth does not need
to be rerun. No Generate action or D6 generation has occurred.

## Official Google references

- [Google Picker for desktop and mobile apps](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker)
- [Google Sheets API OAuth scopes](https://developers.google.com/workspace/sheets/api/scopes)
- [Google Drive API scope guidance](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [OAuth 2.0 refresh-token behavior](https://developers.google.com/identity/protocols/oauth2/)
- [OAuth app audience and publishing status](https://support.google.com/cloud/answer/15549945?hl=en)
