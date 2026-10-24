# V2 Runtime Foundation

The original foundation phase established library contracts under `src/gflow_cli/v2_runtime`; it did not start a worker, API listener, background task, provider browser, or service. The production execution adapters below are a later layer and are invoked explicitly by the production runtime.

## Configuration

The canonical variables are `RUNTIME_ENV` (`development`, `staging`, or `production`), `STORAGE_PROVIDER`, `TEMP_ROOT`, `LOG_ROOT`, `MIN_FREE_DISK_GB`, `KEEP_FAILED_JOB_HOURS`, and `SUCCESS_TEMP_RETENTION_MINUTES`; `RUNTIME_STATE_ROOT` and `LOG_LEVEL` are also supported. Production requires explicit state/temp/log roots and a storage provider. Paths are absolute POSIX paths; Windows developer paths and filesystem root are rejected. `.env.example` contains placeholders only. The initial foundation did not parse credentials; the later production adapters use externally provisioned Google Workspace credentials and inject provider keys through the runtime environment.

## Job lifecycle contract

The original generic foundation sketch was:

`QUEUED -> VALIDATING -> READY -> RUNNING -> VERIFYING -> UPLOADING -> SUCCESS`

That generic sketch predates the current transaction-backed production lifecycle. Current `PRODUCTION_JOBS`, `SCENE_JOBS`, `TTS_JOBS`, and `FINAL_OUTPUT` transitions are implemented by the production stores and orchestrator described below; their persisted vocabularies are defined by the checked-in schemas.

The earlier design notes proposed additional job metadata. The current transaction schemas supersede that sketch; do not add duplicate fields without reviewing those schemas first.

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

`AssetReference` stores a logical storage provider, stable asset ID/key, optional remote ID, checksum, MIME type, and size. The storage protocol covers resolve/download/upload/remote verify. The production `GoogleDriveStorageAdapter` uses externally provided Workspace credentials for those operations; job completion follows remote metadata and readback verification.

## Provider resolution

The read-only `MasterCatalog` protocol resolves `Generation_Profile_ID`, provider account, model, optional prompt template, mode, and active capabilities. Resolution fails closed on inactive/missing data, environment/provider mismatch, unsupported mode, a prompt template from another provider/model/mode, or aspect ratio, duration, resolution, output count, audio mode, and quality settings not explicitly supported by capability rows. Routing uses `PROVIDER_MASTER.Provider_Type` as the service type and a registered adapter. Adapter parameter keys are semantic; browser selectors remain worker code. Contract adapters cannot execute generation.

The service registry distinguishes `VIDEO_GENERATION`, `IMAGE_GENERATION`, and `TTS`. Google Flow, FAL, and future Phaya implementations would register under video generation when selected by verified Master data; ElevenLabs-like speech providers would register under TTS. No literal provider route or credential is hard-coded into a scene/job.

## Compatibility

Legacy request planning preserves the requested duration and returns `requires_scene_plan` when the model's maximum is exceeded; it never silently truncates or splits. The contract starts no HTTP server and does not alter the existing Flow CLI/API surface. See [LEGACY_API_COMPATIBILITY.md](LEGACY_API_COMPATIBILITY.md).

## Operational boundary

Production secrets belong in an external credential manager. Logs should carry job ID, timestamps, state transition, provider/profile IDs, error code, free/total bytes, threshold, and asset verification outcome, while excluding credentials, cookies, and full auth headers. Production n8n and unrelated providers are outside this runtime boundary.

## TTS and media finalization V2

The segmented TTS adapter targets the existing loopback service at
`127.0.0.1:8011`. Its health endpoint is public; the synthesis endpoint is
protected by `x-api-key` and receives `TTS_API_KEY` from the process environment.
The adapter requires the returned audio path to resolve inside the configured
absolute `TTS_OUTPUT_ROOT`, validates the complete audio stream, and never
retries a request with an uncertain outcome. It copies provider output into a
per-attempt local workspace and canonicalizes it once as mono 48 kHz PCM WAV.

TTS identity uses the existing `TTS_JOBS` key
`Production_Job_ID + Dialogue_Line_ID + Attempt_No`. Completed rows return their
persisted remote artifact without another synthesis call. Unknown or incomplete
rows block submission. Audio is uploaded through the approved Drive adapter,
re-downloaded, and SHA-256 verified before local files are deleted and the row
is marked completed.

The media merger treats validated video duration as authoritative. Shorter audio
is padded with silence; longer audio fails before FFmpeg runs. The preferred
path copies video and encodes mono 48 kHz AAC at 192 kbit/s with faststart; an
H.264 re-encode is the bounded fallback. Output validation probes and fully
decodes video and audio and records `VIDEO_COPY` or `VIDEO_REENCODE` in the
returned result, assembly manifest hash, and final-output validation message.
`-shortest` is intentionally absent to prevent speech truncation.

NO_AUDIO is a separate finalization path guarded by persisted scene metadata:
`Dialogue_Mode=NONE`, no `SCENE_DIALOGUE_LINES` rows for the scene, and no audio
stream in the validated source. It writes a `FINAL_VIDEO` row to the existing
`FINAL_OUTPUT` schema after Drive re-download/hash verification and creates no
fake TTS job. The source video remains available locally. NO_AUDIO requires no dedicated schema field or synthetic TTS row.
