# Gflow V2 Architecture Gap Report

Audit scope: local repository source/config/docs and read-only VPS service, ports, disk, and path checks. No repository runtime was changed during the audit; Phase C remains isolated under `src/gflow_cli/v2_runtime`.

## Existing repository

- The checkout is the canonical fork at `D:\Gflow-CLI`, branch `automation-v2`, based on the verified upstream `develop` baseline documented in [GIT_BASELINE.md](GIT_BASELINE.md).
- The product is a mature Google Flow CLI/MCP implementation. Existing `FlowApiClient`, CLI, queue/daemon, local media output, and optional GCS/S3 storage are Flow-specific and should remain outside the new provider-neutral worker boundary.
- Existing storage abstractions already cover filesystem/cloud paths, but not logical Google Drive asset references, remote-upload verification, per-job temp retention, or Master-data-backed provider routing.
- An optional development helper uses `C:/development/github/claude-video`; it is not required by the product runtime. Windows-specific Chrome/ACL paths are conditional on Windows and do not become VPS runtime defaults.
- No required production runtime dependency on a developer Windows path was found in the audited execution path.

## VPS read-only snapshot

- Ubuntu Linux, root filesystem 96 GB total, 51 GB used, 46 GB available (53% used) at audit time.
- `gflow-local-private-relay.service` was active. Its listener was `172.16.3.1:18766`; no claim is made that a `127.0.0.1:18766` tunnel exists.
- n8n health endpoint returned HTTP 200. The production n8n container and BotTrade/relay services were observed only; none were modified.
- `/opt/gflow-v2`, `/var/lib/gflow`, `/var/log/gflow`, and `/var/tmp/gflow` did not exist at audit time. No directories or services were created.
- ffmpeg, Node.js, and Python were present. No packages or services were installed.

## V2 gaps addressed in this foundation

1. Six creative tables now have schemas and blank Sheet tabs with active relationship keys.
2. A schema/CSV validator checks schema counts, keys, controlled fields, timestamps, conditional dialogue references, FK integrity, and approved-script uniqueness.
3. The isolated V2 runtime defines settings, temp isolation and cleanup guardrails, logical assets, verification and disk checks, catalog resolution, provider routing, and a planning-only legacy contract.

## Remaining before a live production rollout

Connect a real Master-data reader, secure credential resolution, storage adapter with Drive verification, worker lifecycle/queue integration, observability, provider adapters, deployment paths/service ownership, backup/restore, and end-to-end tests. These are intentionally not implemented by this foundation and require a separately reviewed deployment phase.

## Path scan classification

- `src/gflow_cli/tools/runtime.py` (`C:/development/github/claude-video`): `VALID_DEV_ONLY`; optional helper with a text-only fallback.
- `src/gflow_cli/winsec.py` (`C:\Windows`): `VALID_DEV_ONLY`; used only by Windows ACL handling.
- `scripts/dev/measure_html_lang_settle.py` and `scripts/probe/spike_auth_matrix.py`: `VALID_DEV_ONLY`; developer experiments that add an explicit local checkout to `sys.path`.
- `tests/auth/**`, `tests/cli/test_cli_docs.py`, `tests/test_marker_registry.py`, and the Phase C config tests: `TEST_FIXTURE`; Windows paths are mocked inputs.
- Existing `docs/**`, `skills/**`, and generated website copies that show Windows paths: `DOC_ONLY` historical setup examples; not runtime defaults.
- `src/gflow_cli/mcp/setup.py` and `src/gflow_cli/cli.py` use the suffix `.gflow-backup` for a one-time backup of a user config file. This is unrelated to the protected `D:\gflow-backup` directory; no files are read from or restored from that directory.
- `/opt/gflow-v2`, `/var/lib/gflow`, `/var/log/gflow`, and `/var/tmp/gflow` in V2 docs are `DOC_ONLY` until a separately approved deployment. No `G:\` or Google Drive Desktop production dependency was found. The search found no MUST_FIX path in the isolated V2 runtime.
