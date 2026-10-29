# VPS Browser Runtime Readiness

## Controlled application deployment

`scripts/operations/deploy_v2_runtime.py` is the application deployment
operator. It defaults to a read-only preflight and requires an exact clean
`automation-v2` commit. It packages only `src/`, `schemas/`, the production F5
operator, the legacy reconciliation, ambiguous-policy, and one-shot replacement operators,
the read-only Flow pricing inspector, the runtime doctor, `pyproject.toml`, and `uv.lock` from Git; it never
copies `.git`, tests, docs, ignored files, credentials, or a source virtualenv.
The allowlisted `validate_v2_data_architecture.py` is included because the
live Google Sheets snapshot source imports it for full schema and semantic
validation in the deployed runtime.
It copies the existing runtime virtualenv into the staged tree and synchronizes
the locked production dependencies there before promotion. Source builds for
dependencies are disabled during this root-run step; missing platform wheels
fail the staged deployment before promotion. The doctor verifies the deployed
source SHA and imports from that SHA-bound source tree. Browser state, OAuth,
runtime state, logs, and temporary files remain at their separate documented
paths.

Run on the VPS as root from the approved source checkout:

```sh
python3 /opt/gflow-v2-src/scripts/operations/deploy_v2_runtime.py \
  --commit <exact-40-character-commit>
python3 /opt/gflow-v2-src/scripts/operations/deploy_v2_runtime.py \
  --commit <exact-40-character-commit> --apply
```

The first command is a no-write preflight. Apply creates a full timestamped
rollback copy under `/opt/gflow-v2-backups/`, stages the allowlisted archive,
copies and synchronizes the existing `.venv`, promotes atomically, verifies
per-file hashes against `deployment-manifest.json`, and runs the runtime doctor
as `gflow`.
If promotion or those runtime checks fail, the previous application directory
is restored automatically and the failed candidate is retained for diagnosis.
The rollback copy also has a full-tree hash manifest. Restore only by naming
that exact verified backup:

```sh
python3 /opt/gflow-v2-src/scripts/operations/deploy_v2_runtime.py \
  --rollback /opt/gflow-v2-backups/<exact-backup-directory>
```

Deployment refuses a dirty/wrong source checkout, an unexpected runtime owner
or mode, an active process using the application/profile, unsafe archive paths,
insufficient space, or concurrent deployment. It does not restart services
and does not read or modify protected data roots. Google Sheets connectivity
is verified by the subsequent fresh canonical snapshot; F5 is a separate
explicit step.

## Target (not deployed)

- Application checkout: `/opt/gflow-v2`
- Runtime state: `/var/lib/gflow`
- Logs: `/var/log/gflow`
- Temporary job files: `/var/tmp/gflow`
- Browser state: `/var/lib/gflow/browser-state/<Provider_Account_ID>/`
- Production dependency on the development PC: zero

## Required future audit

Before deployment, measure current OS, CPU/RAM/swap/disk, Python, Node, Chrome/Chromium, Playwright browser libraries, ffmpeg, Xvfb, runtime directory ownership/permissions, `/opt` and `/var` capacity, and n8n/relay/port ownership. Record only observed facts. Do not install, configure, restart, or deploy during Phase F.

## Browser-state controls

Session/profile data is credential-equivalent. It must be owned by the dedicated Gflow service identity with restricted permissions and excluded from Git, Google Sheets, Google Drive, logs, and reports. Use the existing V2 roots; never keep browser state in temporary storage. One active Flow lane per provider account and dedicated project is the initial concurrency limit.

## Read-only audit snapshot — 2026-10-17

The current authorized SSH agent connected successfully. The following was observed on the current host; no prior report was used as current evidence:

| Check | Observed state |
|---|---|
| OS / kernel | Ubuntu 24.04.4 LTS / Linux 6.8.0-124-generic |
| CPU / memory | 2 logical CPUs; 7.8 GiB RAM, 6.1 GiB available; 4 GiB swap, 140 MiB used |
| Root filesystem | 96 GiB total, 51 GiB used, 46 GiB available (53% used); `/opt`, `/var/lib`, `/var/log`, and `/var/tmp` share this filesystem |
| Python / Node | Python 3.12.3 / Node v20.20.2 |
| Browser / media tools | Google Chrome 154.0.8037.57; FFmpeg 6.1.1; Xvfb at `/usr/bin/Xvfb` |
| Playwright | Not installed for system Python; no Playwright package path found in the bounded `/home/gflow` and `/opt/gflow-worker` search |
| V2 target roots | `/opt/gflow-v2`, `/var/lib/gflow`, `/var/log/gflow`, and `/var/tmp/gflow` are absent |
| Services / containers | `gflow-local-private-relay.service` is active. Docker reports `n8n_v1_live` running with its Postgres containers and Traefik. No active gflow adapter/display systemd unit was listed. |
| Runtime/browser processes | No Chrome, Chromium, Node, or Xvfb process was present in the process summary during the snapshot. |
| Listener ownership | n8n is on port 5678; private worker bridge on 172.16.3.1:18766; media services on 8011/8012; relay helper on loopback 8787; public listeners include SSH 22 and Traefik 80/443. |
| Existing state parent | The Gflow state parent exists with owner `gflow:gflow`; its session contents were not read. |

`VPS_READINESS=BLOCKED` for the V2 browser runtime: the V2 install/state/log/temp roots are absent and the audited paths did not contain an installed Playwright runtime. The existing n8n and relay components remain untouched. This audit did not install packages, create directories, inspect browser session contents, change services, or deploy Phase F.

## Read-only audit snapshot — 2026-10-18

The audit was repeated over SSH with strict host-key checking and BatchMode.
It made no remote changes and did not inspect browser profile contents.

| Check | Observed state |
|---|---|
| OS / kernel | Ubuntu 24.04.4 LTS / Linux 6.8.0-124-generic |
| CPU / memory | 2 logical CPUs; 7.8 GiB RAM, 6.3 GiB available; 4 GiB swap, 140 MiB used |
| Disk | Root filesystem 96 GiB total, 51 GiB used, 46 GiB available; `/opt`, `/var/lib`, `/var/log`, and `/var/tmp` share it |
| Python / Node | Python 3.12.3 / Node v20.20.2 |
| Browser / media tools | Google Chrome 154.0.8037.57; FFmpeg/ffprobe 6.1.1; Xvfb present |
| uv / Playwright | `uv` absent; system Python has no Playwright distribution |
| V2 roots | `/opt/gflow-v2`, `/var/lib/gflow`, `/var/log/gflow`, `/var/tmp/gflow`, and `/var/lib/gflow/browser-state` absent |
| Service identity | `gflow` user/group exists (UID/GID 1000) |
| Existing legacy path | `/opt/gflow-worker` exists, owned by `gflow:gflow`; it is excluded from V2 deployment |
| Protected services | Private relay active on `172.16.3.1:18766`; n8n/Postgres and Traefik containers present; BotTrade services present |
| V2 listeners/processes | No V2 checkout or V2 process found; existing listener owners were recorded without changing them |

`VPS_READINESS=BLOCKED` until the approved V2 application is installed with the
locked runtime and the V2 doctor passes. The deployment must not reuse the
legacy worker path or take ownership of the relay listener.

## V2 runtime doctor

After installing the exact approved application commit and creating service-
owned runtime roots, install the doctor from the approved operations commit at
`/opt/gflow-v2-ops/v2_runtime_doctor.py`. Keep this operator utility outside
the application checkout so the deployed source remains byte-for-byte tied to
its approved SHA. Run it as the `gflow` service user with the V2 virtual
environment and explicit production roots:

```sh
sudo -u gflow env \
  RUNTIME_ENV=production STORAGE_PROVIDER=google-drive \
  RUNTIME_STATE_ROOT=/var/lib/gflow TEMP_ROOT=/var/tmp/gflow \
  LOG_ROOT=/var/log/gflow GFLOW_APP_ROOT=/opt/gflow-v2 \
  GFLOW_EXPECTED_APP_SHA=86b172ba2987945913cbb8fcc174ac6de6609392 \
  /opt/gflow-v2/.venv/bin/python /opt/gflow-v2-ops/v2_runtime_doctor.py
```

The doctor prints bounded status details only. It checks the application SHA,
Python/uv/imports, the locked Playwright minor, Chrome/Xvfb/FFmpeg/ffprobe,
runtime ownership and permissions, browser/recovery roots, free disk, and the
planned loopback port. It never reads browser profile contents or submits work.

The expected SHA must be the exact reviewed application commit; the doctor
fails if the checkout is dirty, the installed archive manifest does not match,
or the SHA is absent. It accepts only the production Google Drive runtime and
the three documented, pairwise-disjoint roots. Root paths must be real writable
directories owned by the service account, not symlinks.

## D3/D4 checkpoint — 2026-10-18

D3 installation was completed against the current host after a fresh read-only
audit. The deployed application is the exact D2-tested commit
`86b172ba2987945913cbb8fcc174ac6de6609392` at `/opt/gflow-v2`; the runtime
doctor from operations commit `715b3f319efb0581e1da9c146f5e07a3cdb3bd64` was
installed separately under `/opt/gflow-v2-ops`. Python, the locked Playwright
1.61 line, Chrome, Xvfb, FFmpeg/ffprobe, roots, free disk, and loopback-port
checks passed. The doctor was rerun as the service user. No V2 worker or V2
service was started, and protected containers/listeners were unchanged.

D4 was reverified on 2026-10-18 using the repository CLI status path: the
`gflow-prod` profile resolved under
`/var/lib/gflow/browser-state/profile_gflow-prod` (owner `gflow:gflow`, mode
0700), and the Flow host-session verifier reported authenticated. The account
identifier was redacted from operator output. The live Master Sheet contains
active PROD account `PVA_0002` for project
`aafe9282-071f-4333-baa4-92b9f76a9c27`. TEST profile and personal browser data
were not copied or reused.

## D5 presubmit wrapper — live-source implementation and current gate

`scripts/operations/vps_flow_presubmit.py` now reads the approved Sheet through
the V2 Google Workspace credential and a read-only Sheets API transport. The
source enforces the approved spreadsheet ID, exact headers from all local V2
schemas, full schema/FK/semantic validation, secret-shaped field rejection,
and producer-owned UTC `read_at`. It writes any temporary snapshot atomically
under runtime state with mode 0600, then removes it after presubmit. The
production entry point does not accept caller-provided JSON; `load_snapshot()`
is retained only for offline tests.

The canonical V2 Google Workspace credential is installed at
`/etc/gflow-v2/google-authorized-user.json`, owned by `gflow:gflow` with mode
`0600`, and its authenticated API access is verified. An earlier 2026-10-18
F5 attempt stopped before Flow because `SPV_000006` version 6 differed from
fresh Phase E. Prompt lifecycle work produced and approved `SPV_000007` after
exact Phase E validation. On 2026-10-19, the production wrapper resolved that
unique approval and completed a live non-generating F5 presubmit.

The wrapper requires `RUNTIME_ENV=production`, explicit runtime roots,
`GFLOW_EXPECTED_GOOGLE_EMAIL`, and `GFLOW_GOOGLE_WORKSPACE_CREDENTIALS`; it
calls F5 `prepare_live_flow()` only after current live Sheet validation and
exact prompt matching. It never performs login or calls Phase G. The 2026-10-19
F5 report verified the PROD account/project, Agent-only surface, prepared model,
9:16 aspect, one output, exact provider UI prompt digest, enabled Generate
control without clicking it, and a `CLEAN` recovery journal. It recorded
`generate_clicked=false` and `credits_used=0`.

The internal `run_legacy_replacement.py` operator is also included in the
deployment allowlist. Quote mode is read-only. Execute mode requires the exact
quarantined parent and one official Standard Veo 3.1 Lite NON_ULTRA quote. A
new authorization requires a fresh formal decision; exact replay validates the
persisted decision digest and lineage, then rechecks fresh parent evidence and
the same policy/reconciliation link. It reserves the exact quote before Phase
G, permits one Generate click and at most one exact confirmation approval, then
stops after same-attempt reconciliation and local output validation. For
non-subscribers it reports Google's published
14:00–17:00 UTC availability warning as an advisory; Flow decides whether the
request can proceed. A correlated `YhhmEf` submit response with parsed RPC
reasons records sanitized refusal facts; a reason code is retained only when
exactly one safe token is present, except the exact
`PUBLIC_ERROR_UNUSUAL_ACTIVITY` reason is retained as the WAF code whenever it
appears. Response bodies are never stored. The same attempt is reconciled
without retry. Only the complete correlated WAF HTTP 403 proof contract proves
non-submission. It never enters D7.

Run it from the deployed application source path with the existing V2 roots,
Google Workspace credential, and `gflow-prod` profile. Omit `--execute` for a
quote-only read. Add `--execute` only for the exact approved one-shot lifecycle;
the operator reports the published peak window in `peak_window_advisory` and
leaves request availability to Flow.

```sh
cd /opt/gflow-v2
GFLOW_CLI_HOME=/var/lib/gflow/browser-state \
PYTHONPATH=/opt/gflow-v2/src:/opt/gflow-v2 \
RUNTIME_ENV=production STORAGE_PROVIDER=google-drive \
RUNTIME_STATE_ROOT=/var/lib/gflow TEMP_ROOT=/var/tmp/gflow LOG_ROOT=/var/log/gflow \
GFLOW_GOOGLE_WORKSPACE_CREDENTIALS=/etc/gflow-v2/google-authorized-user.json \
PYTHONUTF8=1 xvfb-run -a python scripts/operations/run_legacy_replacement.py \
  --scene-id SCN_000001 \
  --profile-id GPR_0002 \
  --provider-account-id PVA_0002 \
  --provider-project-id aafe9282-071f-4333-baa4-92b9f76a9c27 \
  --expected-email singleava508@gmail.com \
  --parent-attempt-id D2_13c62112d9881710019049d95cb6da57
```

For the authorized execution, append `--execute` after confirming the latest
quote. A peak-window refusal remains an unknown submission state unless the
correlated WAF HTTP 403 evidence meets the existing proof contract; reconcile
the same attempt and never retry it. Never invoke the operator with another
parent or account identity.

## D7 transaction coordinator — backend integration (offline-tested)

`gflow_cli.v2_runtime.production_jobs` provides the coordinator contract and
test-only in-memory implementation. Its Sheets-backed implementation and Drive
artifact boundary are described in the current checkpoint below. No live
Production Job or Scene Job rows have been written. D7 live remains dependent
on D2, D4, D5, and D6 PASS.

D6 production execution is not generalized offline. D6-shaped run IDs are now
rejected by generic Phase G ledger creation; the dedicated release-gated path
requires one exact, artifact-verified `D2_SUCCEEDED` prerequisite and stores
that run ID with the D6 record. The shared executor also refuses D6 without a
PROD execution context and shared production credit guard. This authorization
check does not provide a generalized D6 runtime or budget path. Keep D6 live on HOLD until D2 is
reconciled through a new successful D2 and a reviewed production budget/runtime
path exists. Forensic closure of an unknown D2 does not satisfy the gate.

## Current autonomous-run checkpoint — 2026-10-18

The statements above describe the earlier 2026-10-18 D7 audit and are
superseded for current source-code capability by the `automation-v2` checkpoint
validated at `94c15a5b9fe07fac64cea7bc71d246b47d1d36c0`:

- D5 OAuth is DONE. The canonical credential exists and is verified; the
  Master Sheet, all 34 schemas, and `Job_Artifacts` checks passed. A fresh
  2026-10-18 PROD snapshot found one approved prompt, but its source-context
  hash is stale against Phase E; the supported presubmit stopped before Flow.
- Phase G now has explicit environment/provider/account/project identity and a
  single shared provider/day credit guard. This is offline engineering only;
  it does not make D5 pass or resolve the historical D2 attempt.
- The D7 Sheets transaction adapter is wired to
  `ProductionJobCoordinator` through `GoogleSheetsProductionJobStore`. Claims
  use the existing request idempotency key and exact schema rows; transitions
  compare the caller's expected state and read back uncertain acknowledgements.
  Conflicts and unresolved writes fail closed, with a best-effort persisted HOLD
  when the last observed state still matches the expected state. Sheets values
  updates are not server-side compare-and-swap, so external concurrent editors
  remain a production risk.
- `persist_scene_output` connects the Scene Job persistence boundary to Drive:
  stable-key upload, remote metadata and re-download hash verification, evidence
  persistence, then local cleanup and COMPLETED. Local media stays present until
  the verified remote evidence row is written.
- D7 is still **not live-validated**. The current repo documents the transaction
  tabs as production schemas and has no isolated TEST transaction sheet or
  approved synthetic row lifecycle. Do not write test records into unrelated
  production rows. The current schemas also have no durable transition-event
  history or `Provider_Media_ID` field; the Sheet store reports only events seen
  by its current process and cannot rehydrate a provider media ID after restart.
  These schema and live-test-path limits require review before D7 production
  execution. Drive uses multipart rather than resumable upload, and its caller
  must supply a freshly verified output-folder identity.
- No live Sheets/Drive write, Flow Generate, or media generation was run. The
  non-generating D5 presubmit was run and held at `APPROVED_PROMPT_STALE` before
  opening Flow. At the time of this checkpoint, `/opt/gflow-v2` still held
  `9bb6e88b4805d63a59d0ff218e24b38f3117db0a`; the validated source update had
  been pushed but not yet deployed.

## Read-only production readiness reconciliation — 2026-10-23

The following state was freshly inspected without changing Google Sheets,
Flow, services, or the deployed runtime:

- The isolated source checkout `/opt/gflow-v2-src` was on `automation-v2` at
  `39f980fe59a22ca299f1dd1ac18a73a8c487d3f8`, with a clean tracked worktree at
  inspection start. Its Python 3.12.3 venv ran 351 relevant Phase E/G,
  production execution, snapshot, Sheets, and presubmit tests successfully
  after test fixtures were made POSIX-portable. Ruff, format, Pyright, hygiene,
  and documentation gates also passed.
- One new read-only canonical Sheet snapshot was exported at
  `2026-10-23T13:51:10Z`: 34 tables, 58 logical rows, zero phantom rows, SHA-256
  `c8b8f05239d4c1b9f686e18cd1908067f438084403b26a9a46c9c2461654df0c`. Pure
  Phase E and production request construction passed for `SCN_000001`,
  `GPR_0002`, `PVA_0002`, `MDL_0001`, `PMT_0001`, and the single approved
  `SPV_000007` version 7 prompt. The four transaction tables were empty.
- The Sheet-bound provider project ID is
  `aafe9282-071f-4333-baa4-92b9f76a9c27`. The production presubmit code still
  names its project `AI Video Automation V2 - PROD`, which differs from the
  separately supplied expected name `Google Flow V2 PROD`; the live project
  name was not verified during this read-only review.
- `/opt/gflow-v2` is a non-Git application tree with build manifest commit
  `42787383505b43394b3657b5138d137a259c3185` and Python 3.12.3. Its
  `phase_g.py` and `phase_g_run.py` checksums differ from the source checkout.
  No V2 systemd unit was present; the only matching service was the unrelated
  private relay. The existing runtime was not modified or deployed.
- The expected `gflow-prod` browser-profile directory exists with owner
  `gflow:gflow` and mode `0700`; its contents were not opened. The desktop
  Computer Use browser inventory did not expose that VPS profile or a Flow
  session. Consequently Flow login/account, live project name, composer state,
  selections, assets, and current credits remain unverified.
- The live F5 operator calls `prepare_live_flow()`, which applies and restores
  Flow settings. It was not run because this reconciliation authorized only
  read-only Flow inspection. The current provider balance and generation quote
  are therefore unknown, and pre-submit is not fully validated.

`READY_TO_RELEASE_HOLD=NO`. Reconcile the project-name binding, provide a
read-only view of the existing `gflow-prod` Flow session, verify the current
credit balance/quote and live composer/assets, and deploy the approved source
to `/opt/gflow-v2` only through a separately approved deployment workflow
before reconsidering release readiness. Generate, Approve, and production D2
remain unexecuted.

## Read-only PROD Flow pricing inspection — 2026-10-27 implementation

The 2026-10-23 readiness findings above are a historical checkpoint. The
current inspection path is `scripts/operations/inspect_flow_pricing_read_only.py`
and is deliberately limited to `PVA_0002`, `GFLOW_PROD_AUTH` / `gflow-prod`,
and project `aafe9282-071f-4333-baa4-92b9f76a9c27`. Invoke it only for that
exact target:

```bash
python scripts/operations/inspect_flow_pricing_read_only.py \
  --provider-account-id PVA_0002 \
  --project-id aafe9282-071f-4333-baa4-92b9f76a9c27
```

The operation obtains the repository `ProfileLease` non-blockingly before
session verification and retains it through browser close. Contention returns
`PROFILE_IN_USE`; it never bypasses either the repository lease or Chromium's
native profile lock. It reuses V2 Agent-only project navigation and the
read-only settings snapshot, including the duration radio's locale-invariant
number when that group is present. It does not call `prepare_live_flow()`,
`apply_settings()`, prompt-fill methods, or any Generate/approval/submission
method. The settings pane may be opened and closed to read selected values;
no save control is used. The Generate control is read-only text evidence and
is never clicked.

A cost is accepted only when one unambiguous `credits` amount appears in the
current Generate control label on an English UI. It is not inferred from
balance, account plan, historical prices, or model defaults. Other locales,
absent or ambiguous labels, hidden duration/configuration fields, and
unsupported UI shapes remain `UNKNOWN` or return a sanitized HOLD code.
`CONFIG_MATCH=PASS` requires all three canonical fields (duration, aspect
ratio, and output count) to be directly visible and equal to the intended
values. A visible quote is marked `CREDIT_COST_AUTHORITATIVE=YES` only when
that configuration, the expected model, and standard priority are verified.
Screenshots are clipped to the settings panel and, only when a quote is found,
the Generate control. Evidence files are stored under a unique
`flow-pricing-inspection/<timestamp>/` directory with owner-only permissions.
The DOM digest is computed from the allowlisted evidence envelope, not a raw
page dump. The operation writes no pricing/master-data, attempt, reservation,
prompt, or production job record.

This inspection path reports only the fields observed in its live run. It does
not validate assets or authorize Generate/Approve. It navigates the browser to
the exact project and opens/closes the settings view; `FLOW_STATE_CHANGED=NO`
means no persistent Flow project, prompt, model, or setting data was changed.
Pricing master-data updates and any production execution remain separate
tasks.

After deployment manifest parity and runtime doctor pass, run the bound live
regression once from the clean source checkout. It invokes the deployed
`/opt/gflow-v2` inspector script and requires the existing profile path; it
does not create a copy of the authenticated profile:

```sh
cd /opt/gflow-v2-src
GFLOW_CLI_HOME=/var/lib/gflow/browser-state \
GFLOW_CLI_E2E_PROFILE=gflow-prod \
RUNTIME_ENV=production \
STORAGE_PROVIDER=google-drive \
RUNTIME_STATE_ROOT=/var/lib/gflow \
TEMP_ROOT=/var/tmp/gflow \
LOG_ROOT=/var/log/gflow \
PYTHONUTF8=1 xvfb-run -a \
  uv run --locked --extra dev --python 3.11 \
  python -m pytest -q -s -m e2e_auth \
  --basetemp=/tmp/gflow-pricing-inspection-e2e \
  tests/e2e/test_flow_pricing_inspector_e2e.py
```

The `xvfb-run -a` wrapper provides a headed display in SSH sessions with no
`DISPLAY` while keeping the existing profile and browser engine.

The E2E test holds the same profile lease through the deployed inspector, prints
only its sanitized JSON report, and checks account/project identity and zero
provider mutations. A HOLD for unavailable model/config/quote evidence is a
valid inspection result; identity mismatch, profile contention, or an unsafe
surface fails the regression and must stop further pricing work.
