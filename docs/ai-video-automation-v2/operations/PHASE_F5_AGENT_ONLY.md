# Phase F.5 Agent-only Flow Operations

## Account and project boundaries

Use the existing AVA Google identity for both environments. Local TEST and
future VPS production must use separate gflow auth profiles, Flow projects, and
`PROVIDER_ACCOUNT_MASTER` rows:

| Environment | gflow profile | Flow project | Auth profile key |
| --- | --- | --- | --- |
| TEST | `gflow-v2-test` | `AI Video Automation V2 - TEST` | `GFLOW_V2_TEST_AUTH` |
| PROD | `gflow-prod` | `AI Video Automation V2 - PROD` | `GFLOW_PROD_AUTH` |

The Google identity does not determine the environment. The configured profile
and project ID must both match the requested `Provider_Account_ID`. Never copy
cookies, browser storage, or profile files between environments. Do not create
a second Google account to separate TEST and PROD.

## Agent-only behavior

The TEST account currently has the migrated Agent-only composer. The classic
settings trigger is hidden and no agent-mode chip exists. This matches upstream
issue #799 and is a distinct surface, not selector drift. The concrete F5 port
reuses the repository profile resolver, Chrome strategy marker, launch
arguments, auth verifier, and `ProfileLease`.

The Agent settings panel currently exposes video model, aspect ratio, output
count, and the account-level confirmation preference. Duration, resolution,
audio, and quality controls are not present in this view. Phase E therefore
requires a structured, capability-valid duration and the Flow adapter puts that
value into a dynamic Agent instruction. F5 verifies the composed text, not an
Agent duration control or the eventual media length; Phase G must validate the
generated duration. Explicit resolution, audio, or quality settings hold
because this surface has no verified contract for them. The supported first
mode is text-to-video.
Reference inputs, Agent instructions, and negative-prompt entry are outside
this F5 contract.

### F5 state evidence

Capture `BASELINE` before the transaction, verify the expected settings and
provider UI prompt as `PREPARED`, then inspect restored persistent defaults as
`RESTORED`. Baseline aspect, prompt, or Generate readiness differences are
not presubmit failures; F5 is responsible for preparing the request and
checking that prepared readback. A pass requires exact prepared values,
Generate present and enabled without a click, restored settings equal to the
baseline, and a `CLEAN` recovery journal. Report the approved creative prompt
SHA separately from the provider UI prompt SHA.

Before any persistent setting change, F5 captures the observed defaults and
atomically writes a credential-free recovery record under
`RUNTIME_STATE_ROOT/recovery/agent-settings/`. The journal enters `DIRTY`
before a setting is changed, then `RESTORING` before restore, and `CLEAN` only
after exact UI readback. An unclean or malformed journal blocks new work. The
per-account/project async lane and repository `ProfileLease` serialize access
to the same persistent profile. A recovery failure is a hard hold requiring
operator inspection; do not retry preparation until restore is verified.

The journal stores only account/project IDs, a prompt-version correlation ID,
semantic setting values, state, and UTC timestamps. It must never contain
passwords, tokens, API keys, cookies, browser storage, or session data.

## Runtime and browser roots

Resolve auth profiles through `gflow_cli.auth.profile_dir()` and the repository
`GFLOW_CLI_HOME` configuration. Do not hard-code workstation paths. V2 runtime
state, temporary files, and logs use `RUNTIME_STATE_ROOT`, `TEMP_ROOT`, and
`LOG_ROOT`; production requires explicit absolute POSIX paths. A future VPS
profile uses headed Google Chrome under Xvfb where the host has no display, with
the same repository browser strategy marker and profile-compatibility checks.
Do not add external CDP attachment or stealth/bypass logic.

Keep temporary media in `TEMP_ROOT`, apply the configured retention policy, and
check free disk before jobs. Logs and recovery records must exclude credentials
and browser session contents. Run one job per account/project lane. Renew an
expired session through the repository auth workflow; never automate password
or MFA entry.

## Deployment progression

The release gates are sequential:

1. **D1** Local F.5 Agent-only presubmit.
2. **D2** Local Phase G single real text-to-video test.
3. **D3** VPS V2 installation.
4. **D4** VPS AVA / `gflow-prod` auth bootstrap.
5. **D5** VPS non-generating presubmit.
6. **D6** VPS one controlled paid text-to-video test.
7. **D7** VPS full Scene Job lifecycle.
8. **D8** n8n shadow integration.
9. **D9** Unattended production.

Local F5 success alone does not authorize deployment or unattended
orchestration. This phase performs no VPS, n8n, relay, or BotTrade changes.

## Phase G risk and submission evidence

Upstream issue #906 reports unusual-activity refusals on the migrated host.
A manually authenticated Flow session does not prove that an automated
submission will be accepted. Do not add stealth or bypass behavior. A future
Phase G test must fail closed on a refusal and must not blindly retry.

Before any later authorized Generate action, capture the baseline project
operations, render cards, and assets. A click, spinner, disabled button, or
generic `generation_started` event is not submission acknowledgement. Require
new attempt-specific provider evidence. If acceptance is ambiguous, hold as
`UNKNOWN_SUBMISSION_STATE`; do not retry. Attribute output using an exact new
attempt/provider identity, never only card order, title, or filename.

## Current boundary

The Phase F kill switch remains active. The F5 port has no submission method.
Phase F.5 may inspect Generate presence/enabled state, but it must never click,
keyboard-submit, invoke a generation RPC, approve credits, download output, or
create a production/scene job execution attempt.

## Overnight checkpoint — 2026-10-18

- D2 is `HOLD` at `UNKNOWN_SUBMISSION_STATE` after one Generate click without
  attributable acceptance evidence. Do not retry and do not claim D2 PASS.
  If complete forensics cannot reconstruct its provider/billing outcome, the
  old attempt may only be terminally closed as `FORENSICALLY_CLOSED_UNKNOWN`
  through the dry-run-first, SHA-guarded operator documented in
  `architecture/PHASE_G_D2_CONTROLLED_EXECUTION.md`. That closure preserves
  its 50-credit ledger reservation and keeps D6 blocked; it does not mean
  no-charge or generation failure. A fresh distinct D2 attempt must pass fully
  before D6 can proceed.
- D3 VPS installation is verified for the exact D2-tested application SHA.
- D4 is verified: the isolated `gflow-prod` profile is authenticated as the
  intended Google account; Master data resolves PROD account `PVA_0002` and
  project `aafe9282-071f-4333-baa4-92b9f76a9c27`.
- D5 OAuth and canonical Workspace verification are DONE. A fresh 34-schema
  snapshot and `Job_Artifacts` access pass. That checkpoint's F5 attempt
  stopped before Flow because its then-current approved prompt differed from
  fresh Phase E; later prompt lifecycle work reconciled the current approval.
  On 2026-10-19, a fresh production run selected `SPV_000007`, passed Phase E,
  prepared the Agent-only UI, restored baseline settings, and finished with a
  `CLEAN` recovery journal. F5 passed without clicking Generate or using
  credits. Re-resolve the unique current approved version from live Master
  data for every new F5 run.
- D7 now has a Sheets-backed `ProductionJobStore` bridge and a Drive-backed
  Scene Job persistence boundary, covered by offline fake-transport tests. No
  live transaction rows were written. The current repository does not define
  an isolated TEST transaction sheet or approved synthetic row lifecycle, so
  D7 live validation remains blocked until that path is authorized and D2/D4/
  D5/D6 production gates are satisfied.

Offline D5/D7 code must not be reported as live PASS. Do not enter D8, modify
n8n, or start social posting.
