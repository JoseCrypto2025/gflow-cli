# TTS and Merge Pipeline V2 Implementation Plan

> **For agentic workers:** Run `/gflow:status --feature tts-merge-pipeline-v2` to
> find the next unchecked task. Run `/gflow:check` before every commit.

**Goal:** Add a secure, idempotent TTS and media-finalization path using the
existing V2 transaction schemas, then finalize the current explicitly silent
scene without fabricated speech.

**Architecture:** Implement provider, audio validation, merge, and finalization
components under `src/gflow_cli/v2_runtime`, following existing protocol and
transaction-adapter patterns. Use the authenticated segmented TTS service as the
first provider; inject credentials from the existing secure runtime environment
only. Keep Flow generation and the CLI/MCP surfaces unchanged. Reuse the existing
TTS_JOBS, SCENE_JOBS, and FINAL_OUTPUT schema fields.

**Predict verdict:** Prior review STOP; provider-auth and scene-intent findings
were resolved by new evidence. Re-run the five-person review after implementation.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Provider request may be accepted while acknowledgement is lost | Stable provider identity/readback; HOLD on unresolved ambiguity; no automatic duplicate synthesis |
| Critical | Audio may be truncated or distorted during mux | Timeline-authoritative duration policy, full decode, explicit AAC regression fixtures, no unconditional `-shortest` |
| Critical | Provider key exposure | Secret only through injected environment; redact request headers and exception data |
| High | Service not available at runtime | Preflight health/auth check and fail closed before job claim/provider call |
| High | Existing V2 schemas have no explicit NO_AUDIO flag | Do not add a duplicate field; represent the path through final-output manifest and absence of TTS rows |
| High | Legacy server paths do not match V2 workspace paths | Download/resolve into a validated per-job workspace and persist only remote IDs/URIs |

---

## File structure

### New files

```text
src/gflow_cli/v2_runtime/tts.py
  Provider-neutral request/result, validation, and idempotent TTS orchestration.
src/gflow_cli/v2_runtime/segmented_tts.py
  Authenticated adapter for the existing segmented-v2 service.
src/gflow_cli/v2_runtime/media_audio.py
  Audio probing, complete decode, canonicalization, and quality checks.
src/gflow_cli/v2_runtime/media_merge.py
  Timeline-safe deterministic MP4/AAC assembly and final validation.
src/gflow_cli/v2_runtime/finalize_scene.py
  Speech and NO_AUDIO finalization against the existing transaction contracts.
tests/validation/test_v2_tts.py
tests/validation/test_v2_media_audio.py
tests/validation/test_v2_media_merge.py
tests/validation/test_v2_finalize_scene.py
  Focused offline and mocked-service safety regressions.
```

### Modified files

```text
src/gflow_cli/v2_runtime/google_sheets_transactions.py
  Only if required to add an exact-schema, idempotent TTS transaction store.
src/gflow_cli/v2_runtime/production_jobs.py
  Wire the audio finalization service at the existing lifecycle boundary.
docs/ai-video-automation-v2/README.md
docs/ai-video-automation-v2/architecture/V2_RUNTIME_FOUNDATION.md
docs/ai-video-automation-v2/database/PRODUCTION_EXECUTION_DATA_DICTIONARY.md
  Document the implementation phase, credential boundary, and operational policies.
```

## Task 1 — Offline test scaffolds

**What:** Add tests before implementation for provider request/response handling,
audio checks, merge policy, idempotency, and NO_AUDIO.

**Files:**
- `tests/validation/test_v2_tts.py`
- `tests/validation/test_v2_media_audio.py`
- `tests/validation/test_v2_media_merge.py`
- `tests/validation/test_v2_finalize_scene.py`

**Steps:**
- [ ] Build deterministic short video/audio fixtures with installed FFmpeg tooling.
- [ ] Add required cases from `SCENARIO.md`, including copy and re-encode paths.
- [ ] Keep all provider calls mocked in this task.

**Tests created (red):**
- [ ] Missing credentials and malformed provider results fail closed.
- [ ] Duplicate TTS and merge requests reuse the existing result.
- [ ] Longer audio raises `TTS_DURATION_MISMATCH`; shorter audio preserves video duration.
- [ ] Corrupt/zero-byte audio, damaged MP4, truncated AAC, and bad media fail validation.
- [ ] NO_AUDIO produces no TTS row and no required audio stream.
- [ ] Regression verifies complete decode and no `-shortest` truncation.

## Task 2 — Provider contract and secure segmented adapter

**What:** Implement a narrow provider boundary and adapter for authenticated
`/tts/segmented-v2/create`, status, and file retrieval.

**Files:**
- `src/gflow_cli/v2_runtime/tts.py`
- `src/gflow_cli/v2_runtime/segmented_tts.py`
- `tests/validation/test_v2_tts.py`

**Steps:**
- [ ] Add immutable request/result values using repository conventions.
- [ ] Require explicit voice, locale, exact text/segments, bounded timeout, and configured base URL.
- [ ] Resolve the API key from injected environment/config only; never log or persist it.
- [ ] Validate provider execution identity, completed status, path, size, and checksum.
- [ ] Treat timeouts after submit as ambiguous; reconcile by stable provider identity and never auto-resubmit.
- [ ] Emit stable error codes with sanitized messages.

**Tests created (red):**
- [ ] Missing/invalid key rejected before provider work; 401/403 paths are covered.
- [ ] Correct key is sent but excluded from logs/errors.
- [ ] Empty text, unsafe path, malformed response, failed status, and timeout are handled.
- [ ] Provider output is not marked complete before media validation.

## Task 3 — TTS transaction lifecycle and idempotency

**What:** Persist TTS attempts against the existing TTS_JOBS contract and make
same-attempt retries safe.

**Files:**
- `src/gflow_cli/v2_runtime/tts.py`
- `src/gflow_cli/v2_runtime/google_sheets_transactions.py` (if needed)
- `tests/validation/test_v2_tts.py`

**Steps:**
- [ ] Inspect current Sheets append/readback transaction pattern and exact schema order.
- [ ] Claim by Production_Job_ID + Dialogue_Line_ID + Attempt_No; reuse or reconcile by stable TTS_Job_ID.
- [ ] Use only existing fields/statuses; no duplicate columns or secret material.
- [ ] Preserve ambiguous provider acceptance as HOLD and retain local evidence.
- [ ] Delete temporary audio only after durable remote verification.

**Tests created (red):**
- [ ] Lost acknowledgement reconciles without duplicate rows or provider calls.
- [ ] Existing COMPLETED row is returned without resynthesis.
- [ ] Concurrent claim, schema drift, and unresolved provider status hold.

## Task 4 — Audio probing and canonical validation

**What:** Add objective audio metadata, full decode, normalization, and quality
checks before merge.

**Files:**
- `src/gflow_cli/v2_runtime/media_audio.py`
- `tests/validation/test_v2_media_audio.py`

**Steps:**
- [ ] Probe codec, sample rate, channels, duration, bitrate, and stream count.
- [ ] Require nontrivial file size, positive duration, full decode, sane channels/rate, and bounded silence/clipping checks.
- [ ] Select and document one canonical intermediate format based on fixture compatibility.
- [ ] Compute SHA-256 and prevent repeated lossy conversions.
- [ ] Reject corrupted, empty, or truncated audio with stable typed errors.

## Task 5 — Deterministic merge and regressions

**What:** Assemble validated media with explicit timeline policy and a recorded
copy/re-encode path.

**Files:**
- `src/gflow_cli/v2_runtime/media_merge.py`
- `tests/validation/test_v2_media_merge.py`

**Steps:**
- [ ] Treat video duration as authoritative; pad only shorter audio deterministically.
- [ ] Reject longer speech without truncation, `-shortest`, or frozen video frames.
- [ ] Prefer video stream copy with AAC and faststart; use bounded H.264 fallback only for incompatibility.
- [ ] Probe output, fully decode video and audio, verify duration/aspect/sync, and hash output.
- [ ] Record `VIDEO_COPY` or `VIDEO_REENCODE` in returned metadata.

**Tests created (red):**
- [ ] Short/equal/long/far-too-long audio policies.
- [ ] Both mux paths produce complete decodable output and expected stream metadata.
- [ ] Historical distorted-audio and `-shortest` regressions remain fixed.

## Task 6 — Scene finalization including NO_AUDIO

**What:** Integrate validated TTS/merge or explicit NO_AUDIO finalization with
existing production job and output transaction operations.

**Files:**
- `src/gflow_cli/v2_runtime/finalize_scene.py`
- Existing production/scene job runtime modules only where the current contract requires it.
- `tests/validation/test_v2_finalize_scene.py`

**Steps:**
- [ ] Resolve audio intent from approved Scene and dialogue rows; do not infer speech from prompts.
- [ ] For NO_AUDIO, create no empty TTS job and pass through the validated video path.
- [ ] Persist FINAL_OUTPUT through the existing exact-schema/remote-verification lifecycle.
- [ ] Retain local source/output on failed or uncertain remote verification.
- [ ] Verify no Schema or Google Sheet master-data changes are required for this scene.

## Task 7 — Documentation and five-person review

**What:** Document the new production phase and rerun `/gflow:predict` after
implementation, resolving each finding as local, external, or invalidated.

**Files:**
- `docs/ai-video-automation-v2/README.md`
- `docs/ai-video-automation-v2/architecture/V2_RUNTIME_FOUNDATION.md`
- `docs/ai-video-automation-v2/database/PRODUCTION_EXECUTION_DATA_DICTIONARY.md`
- `CHANGELOG.md`

**Steps:**
- [ ] Document provider auth, secret injection, retry/ambiguity rules, workspace cleanup, and NO_AUDIO behavior.
- [ ] Record historical workflow migration evidence without claiming undocumented parity.
- [ ] Run the five-person review and address all fixable findings.

## Task 8 — Validation, commit, deployment, and readiness

**What:** Run the repository check gate and media/runtime validation, then
commit/push/deploy the exact validated source.

**Files:**
- Test/docs changes from prior tasks.

**Steps:**
- [ ] Run focused audio/TTS/media tests, affected Phase E/G/F5/transaction/schema tests, Ruff, format, Pyright, and repository `/gflow:check`.
- [ ] Run any required live TTS capability test only after fixture tests pass; do not invoke Flow Generate until every readiness gate passes.
- [ ] Run Windows and relevant VPS tests; require zero failures and no new skipped safety tests.
- [ ] Verify secrets, `.runtime/`, `.venv-d2v2/`, and media fixtures are excluded from commit.
- [ ] Commit, push, verify Windows/Origin/VPS source parity, deploy through `deploy_v2_runtime.py`, and run runtime doctor.
- [ ] Re-read Sheet, rerun snapshot/Phase E/F5, then proceed with the separately gated production Generate/approval/D2 workflow.

## Definition of done

- [ ] All tasks complete and `/gflow:check` green.
- [ ] TTS provider, job lifecycle, audio validation, merge policy, regressions, and NO_AUDIO branch pass.
- [ ] Windows/VPS checks, deployment, and runtime doctor pass.
- [ ] Phase E/F5 pass on the fresh production snapshot.
- [ ] Generation is performed only once all gates pass, and final output is verified by full decode and lineage.
- [ ] No unrelated Flow/settings/Sheet/n8n/BotTrade changes.
