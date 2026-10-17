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
