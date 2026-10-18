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

Google documents this as a non-sensitive, per-file scope supported by Sheets.
The desktop/mobile Picker authorization flow permits only `drive.file`; do not
combine another scope into that Picker consent. For a public External app,
Google's current scope guidance says basic app verification applies to
non-sensitive scopes. Sensitive/restricted-scope verification and the
restricted-scope security assessment do not apply to `drive.file` alone.

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
set and then verifies the Sheet and writable folder through their APIs before
installing anything.

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
     --ssh-host gflow-vps
   ```

4. Open the one-time URL printed by the tool in a browser, manually choose the
   same Google account that owns the verified `gflow-prod` identity, consent
   to `drive.file`, then select the Master spreadsheet and existing
   `Job_Artifacts` folder in Picker. Do not enter passwords or MFA into the
   terminal or the bootstrap process.
5. The loopback callback listens only on `127.0.0.1`. The tool checks OAuth
   state and PKCE, requires exactly the two approved Picker IDs and the one
   reviewed scope, verifies the Sheet has 34 tabs and the folder can accept
   children, then streams credential bytes over SSH stdin.
6. The remote installer atomically writes
   `/etc/gflow-v2/google-authorized-user.json`, owned by `gflow:gflow`,
   mode `0600`. The tool then runs the deployed V2 read-only source as the
   `gflow` service user and verifies all local schemas plus output-folder
   access. It prints only the resulting status.

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
  folder, scope drift, state mismatch, timeout, or API failure blocks install.
- OAuth/API errors are reduced to stable safe status text; raw response bodies,
  authorization codes, access/refresh tokens, and client secrets are not logged.
- If remote verification fails after installation, D5 remains held. Do not
  attempt Flow or Generate.
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

As of this recovery, the VPS credential file is missing. Bootstrap cannot
complete until the human has configured the Cloud project and completed
manual OAuth consent plus Picker grants. D5 remains held before Flow until the
service-user source verification passes. D5 is non-generating; D6 is not
authorized here.

## Official Google references

- [Google Picker for desktop and mobile apps](https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker)
- [Google Sheets API OAuth scopes](https://developers.google.com/workspace/sheets/api/scopes)
- [Google Drive API scope guidance](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [OAuth 2.0 refresh-token behavior](https://developers.google.com/identity/protocols/oauth2/)
- [OAuth app audience and publishing status](https://support.google.com/cloud/answer/15549945?hl=en)
