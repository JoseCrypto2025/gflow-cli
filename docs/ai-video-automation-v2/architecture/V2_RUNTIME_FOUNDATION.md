# V2 Runtime Foundation

This phase establishes library contracts under `src/gflow_cli/v2_runtime`; it does not start a worker, API listener, background task, provider browser, or service.

## Configuration

The canonical variables are `RUNTIME_ENV` (`development`, `staging`, or `production`), `STORAGE_PROVIDER`, `TEMP_ROOT`, `LOG_ROOT`, `MIN_FREE_DISK_GB`, `KEEP_FAILED_JOB_HOURS`, and `SUCCESS_TEMP_RETENTION_MINUTES`; `RUNTIME_STATE_ROOT` and `LOG_LEVEL` are also supported. Production requires explicit state/temp/log roots and a storage provider. Paths are absolute POSIX paths; Windows developer paths and filesystem root are rejected. `.env.example` contains placeholders only. Auth is resolved by a future secret-store integration and is not parsed or stored here.

## Job lifecycle contract

The future orchestration lifecycle is:

`QUEUED -> VALIDATING -> READY -> RUNNING -> VERIFYING -> UPLOADING -> SUCCESS`

Terminal alternatives are `FAILED`, `CANCELLED`, and `HELD`. Failures carry stable `error_code` and message values. Transitions and durable job rows remain the responsibility of the future worker/queue integration; this package does not claim job state persistence.

Future job records may need `input_asset_ids`, `temporary_workspace`, `output_asset_id`, `remote_output_uri`, `remote_verified`, `output_sha256`, `local_cleanup_status`, `storage_provider`, `storage_error`, `download_started_at`, `upload_started_at`, `remote_verified_at`, and `local_deleted_at`. These are design notes only; no PRODUCTION_JOBS or SCENE_JOBS canonical schema is created here.

Each workspace is contained at `<TEMP_ROOT>/<validated-job-id>/` with `input/`, `generated/`, `audio/`, `ffmpeg/`, `validated/`, and `logs/`. Cleanup refuses path traversal, symlink targets, and root targets. RUNNING, unknown, or unverified successful jobs are retained. Expired FAILED/CANCELLED jobs use a longer retention window than verified SUCCESS jobs. Cleanup never scans outside the configured root.

Before admitting work, the disk guard compares available bytes with the configured floor plus an optional job estimate. It returns a structured result and can hold new admission; it never stops or kills active work.

## Data and storage classes

- Development data: source-controlled code, schemas, tests, and docs in Git.
- Runtime temporary data: per-job media under `/var/tmp/gflow/<job_id>/`; removed only after retention and remote verification rules pass.
- Persistent runtime state: small queue/checkpoint metadata under `/var/lib/gflow`; no durable media.
- Structured production data: Google Sheets.
- Remote reference and production assets: Google Drive.
- Final output: verified Drive object, referenced by logical ID/URI and checksum.

## VPS deployment design (not deployed)

- `/opt/gflow-v2`: application runtime.
- `/var/lib/gflow`: lightweight persistent runtime state only.
- `/var/log/gflow`: rotating logs.
- `/var/tmp/gflow`: temporary media workspaces.
- Google Drive remains persistent media storage; Google Sheets remains structured state.

Deployment is intended to flow from local development to Git commit, GitHub, then an explicitly approved VPS pull/deploy. **Production must have zero dependency on the local Windows PC.** This mission did not deploy or change the VPS.

## Storage and asset references

`AssetReference` stores a logical storage provider, stable asset ID/key, optional remote ID, checksum, MIME type, and size. The storage protocol covers resolve/download/upload/remote verify. The current Drive class is a no-I/O placeholder and raises `PROVIDER_ADAPTER_UNAVAILABLE`; no OAuth or API call occurs. Completion must require a verified remote object before a future job is marked successful.

## Provider resolution

The read-only `MasterCatalog` protocol resolves `Generation_Profile_ID`, provider account, model, optional prompt template, mode, and active capabilities. Resolution fails closed on inactive/missing data, environment/provider mismatch, unsupported mode, a prompt template from another provider/model/mode, or aspect ratio, duration, resolution, output count, audio mode, and quality settings not explicitly supported by capability rows. Routing uses `PROVIDER_MASTER.Provider_Type` as the service type and a registered adapter. Adapter parameter keys are semantic; browser selectors remain worker code. Contract adapters cannot execute generation.

The service registry distinguishes `VIDEO_GENERATION`, `IMAGE_GENERATION`, and `TTS`. Google Flow, FAL, and future Phaya implementations would register under video generation when selected by verified Master data; ElevenLabs-like speech providers would register under TTS. No literal provider route or credential is hard-coded into a scene/job.

## Compatibility

Legacy request planning preserves the requested duration and returns `requires_scene_plan` when the model's maximum is exceeded; it never silently truncates or splits. The contract starts no HTTP server and does not alter the existing Flow CLI/API surface. See [LEGACY_API_COMPATIBILITY.md](LEGACY_API_COMPATIBILITY.md).

## Operational boundary

Production secrets belong in an external credential manager. Logs should carry job ID, timestamps, state transition, provider/profile IDs, error code, free/total bytes, threshold, and asset verification outcome, while excluding credentials, cookies, and full auth headers. Production n8n, VPS files/services, and paid providers are not touched in this phase.
