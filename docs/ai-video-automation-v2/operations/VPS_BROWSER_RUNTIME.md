# VPS Browser Runtime Readiness

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

## D5 presubmit wrapper — live-source implementation, awaiting credential

`scripts/operations/vps_flow_presubmit.py` now reads the approved Sheet through
the V2 Google Workspace credential and a read-only Sheets API transport. The
source enforces the approved spreadsheet ID, exact headers from all local V2
schemas, full schema/FK/semantic validation, secret-shaped field rejection,
and producer-owned UTC `read_at`. It writes any temporary snapshot atomically
under runtime state with mode 0600, then removes it after presubmit. The
production entry point does not accept caller-provided JSON; `load_snapshot()`
is retained only for offline tests.

No V2 Google Workspace credential is currently installed. The source is
implemented and tested with fake transports, but live reads remain
`WAITING_FOR_HUMAN_AUTH_SETUP`; D5 must hold before opening Flow until the
dedicated OAuth credential is installed as documented in
`GOOGLE_WORKSPACE_AUTH_BOOTSTRAP.md`.

The wrapper requires `RUNTIME_ENV=production`, explicit runtime roots,
`GFLOW_EXPECTED_GOOGLE_EMAIL`, and `GFLOW_GOOGLE_WORKSPACE_CREDENTIALS`; it
calls F5 `prepare_live_flow()` only after current live Sheet validation. It
never performs login or calls Phase G. The report always records
`generate_clicked=false` and `credits_used=0`. D5 is held only on the missing
V2 Google Workspace API credential; D4 and PROD project/account identity are
verified.

## D7 transaction coordinator — offline implementation

`gflow_cli.v2_runtime.production_jobs` provides canonical idempotency digests,
schema-shaped row serializers, append-only Scene Job attempts, compare-and-
transition guards, and remote-verification-before-cleanup checks. Its
in-memory store is test-only. No live Google Sheets transaction writer or
atomic compare-and-set implementation exists, and no Production Job or Scene
Job rows were written. D7 live remains dependent on D2, D4, D5, and D6 PASS.

D6 production execution is not generalized offline. The current Phase G executor
and its credit ledger are explicitly D2-scoped, while D2's post-submit credit
state remains unresolved. Reusing that TEST-only runner or opening a second
credit ledger could violate cumulative accounting; keep D6 live on HOLD until
D2 is reconciled and a reviewed production budget path exists.
