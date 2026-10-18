# V2 Google Workspace API Credential Bootstrap

## Purpose and status

The V2 D5 source uses a dedicated Google OAuth user credential. It does not use
the gflow Flow browser profile, the Google Drive connector, n8n credentials, or
any Chrome cookie/session data. The current repository has no V2 OAuth
credential installed on the VPS, so live Sheet reads and D5 remain held until
the one-time setup below is complete.

The credential is intended to support later bounded Sheets writes and Drive
upload/remote verification. Current code implements Sheets reads only; it has
no Sheet write operation.

## One-time setup

1. In a Google Cloud project owned by the data owner, enable the Google Sheets
   API and Google Drive API.
2. Configure a dedicated OAuth consent application and client for V2. Do not
   reuse n8n credentials or a browser session.
3. Request the narrow `https://www.googleapis.com/auth/drive.file` scope. The
   Sheets API supports this per-file scope; Drive access remains restricted to
   files granted to the app.
4. Explicitly grant the app the approved Master spreadsheet and the dedicated
   V2 output folder using a Picker-based file grant. Do not grant broad Drive
   access.
5. For unattended use, publish the consent app to Production and complete any
   Google verification required for its scopes. An external OAuth app left in
   Testing issues refresh tokens that expire after seven days for non-basic
   scopes.
6. Complete OAuth consent manually in a trusted browser. Do not automate
   password/MFA, reuse the Flow browser profile, or copy Flow cookies.
7. Install the resulting `authorized_user` credential at:

   ```text
   /etc/gflow-v2/google-authorized-user.json
   ```

   It must be owned by `gflow:gflow`, mode `0600`, and remain outside Git,
   runtime snapshot directories, Google Sheets, Drive, and logs. Set:

   ```text
   GFLOW_GOOGLE_WORKSPACE_CREDENTIALS=/etc/gflow-v2/google-authorized-user.json
   ```

   Do not put the credential JSON contents in an environment file, command
   line, ticket, or report.

## Rotation and failure handling

- Keep the file readable only by the Gflow service identity.
- Revoke and replace the credential after suspected exposure.
- Treat refresh/API failure as a D5 hold. Never fall back to browser cookies,
  n8n secrets, connector credentials, or human-authored snapshots.
- Emit stable error codes only; never log authorization headers, token
  responses, refresh tokens, API response bodies, or credential contents.
- Before D5, verify owner/mode and perform a read-only request against the exact
  approved spreadsheet.

## Current human action required

Set up and authorize the dedicated V2 OAuth app with `drive.file`, grant only
the approved Master spreadsheet and V2 output folder, then install its
`authorized_user` file at the path above with owner `gflow:gflow` and mode
`0600`. Until then, `GOOGLE_SHEETS_LIVE_SOURCE` is
`WAITING_FOR_HUMAN_AUTH_SETUP` and D5 must not open Flow.

Google's primary references: [Sheets API scope choices](https://developers.google.com/workspace/sheets/api/scopes),
[OAuth 2.0 refresh-token behavior](https://developers.google.com/identity/protocols/oauth2/),
and [Drive file-scope guidance](https://developers.google.com/workspace/drive/api/guides/api-specific-auth).
