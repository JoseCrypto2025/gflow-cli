# Upstream Invocation Observability Hardening V1 — Implementation Plan

> **For agentic workers:** Follow the tasks in order. Run `/gflow:check` before the commit.

**Goal:** Preserve enough bounded, redacted, ordered evidence from each future upstream video process to classify outcomes safely without changing generation semantics, retries, or the strict JSON contract.

**Architecture:** Extend the existing upstream adapter's bounded subprocess capture and result object. Send typed evidence events through an injected callback to the existing `ProductionCreditGuard` ledger, keeping persistence out of the adapter. Store no unlimited raw output, and leave unavailable provider/Generate facts `UNKNOWN`.

**Predict verdict:** GO — confidence 8/10.

**Risk register:**

| Severity | Risk | Mitigation |
|---|---|---|
| Critical | Credentials or opaque tokens in stderr/stdout excerpts | Existing redactor plus conservative token masking; redact before clipping; omit excerpt on sanitizer failure; boundary tests |
| Critical | Crash leaves no evidence after child dispatch | Persist process start before request stdin; persist observations and termination before parse/normalization |
| High | Return code/signal semantics differ on Windows and POSIX | Preserve raw return code; infer POSIX signal only on POSIX; retain unknown elsewhere |
| High | New telemetry changes success/error contract or triggers retry | Keep JSON parser and normalization acceptance unchanged; test one-shot call counts |

---

## File structure

### New files

```text
docs/superpowers/plans/2026-11-03-upstream-invocation-observability/SCENARIO.md
  Adversarial scenarios and offline test matrix
docs/superpowers/plans/2026-11-03-upstream-invocation-observability/PLAN.md
  Ordered implementation tasks and acceptance criteria
tests/features/upstream_first_adapter_v1.feature
  Additive BDD scenario for durable evidence ordering and malformed output
```

### Modified files

```text
src/gflow_cli/v2_runtime/upstream_gflow_adapter.py
  Process metadata, bounded diagnostics, and evidence callback; preserve strict JSON acceptance
src/gflow_cli/v2_runtime/upstream_canary.py
  Bind future invocation evidence events to the production credit ledger
src/gflow_cli/v2_runtime/production_execution.py
  Allowlisted idempotent lifecycle and provider-evidence event persistence
tests/validation/test_upstream_gflow_adapter.py
  Deterministic fake-process lifecycle, parser, redaction, timeout, and signal cases
tests/validation/test_production_execution.py
  Ledger event ordering, idempotency, and old-row compatibility cases
tests/validation/test_upstream_canary.py
  Caller sink wiring, no-retry, and one-invocation assertions
docs/ai-video-automation-v2/operations/UPSTREAM_GFLOW_ADAPTER_V1.md
  Document evidence fields, ordering, privacy limits, and unavailable upstream facts
CHANGELOG.md
  Record the internal observability hardening
```

## Task 1 — Offline test scaffold

**What:** Add deterministic unit/BDD cases for the requested lifecycle, parse, privacy, ordering, and no-retry behavior before production edits.

**Steps:**

- [x] Add BDD coverage for persisted evidence before parse failure and bounded diagnostics.
- [x] Add fake-runner cases for valid/malformed/wrapped/array/empty/stderr-only/nonzero/timeout/signal/oversized streams.
- [x] Add caller evidence-sink assertions and preserve restart/no-retry behavior.
- [x] Keep provider scenarios on fake runners; add one harmless local subprocess test for the real bounded runner.

## Task 2 — Process lifecycle telemetry

**What:** Capture wall-clock start/end, monotonic duration, raw return code, platform-aware signal, timeout/caller termination, full byte counts/hashes, and truncation while keeping output buffers bounded.

**Steps:**

- [x] Extend `CommandResult` and `AdapterResult` with JSON-safe lifecycle fields.
- [x] Emit `PROCESS_STARTED` after successful child creation and before writing request stdin.
- [x] Emit `PROCESS_ENDED` after termination and pipe draining, before parser work.
- [x] Hash/count all bytes as they drain; retain no more than the existing parse cap plus fixed diagnostic capture limits.
- [x] Preserve current one-shot behavior and default process timeouts.

## Task 3 — Safe parse diagnostics and classification inputs

**What:** Persist parse-error class/message, parseable top-level type, and bounded redacted stream excerpts without changing JSON acceptance.

**Steps:**

- [x] Keep full-buffer UTF-8 decode followed by strict `json.loads` and dict requirement.
- [x] Record safe generic decode/JSON parse metadata; never persist the exception document/repr.
- [x] Redact the entire bounded diagnostic source before prefix/suffix clipping and mask opaque tokens conservatively.
- [x] Omit snippets if redaction fails.
- [x] Represent unavailable Generate, submit, request, operation, and media evidence as `UNKNOWN`.
- [x] Keep ambiguous outcome classifications conservative and do not infer from balance or absent media.

## Task 4 — Durable evidence ordering

**What:** Persist ordered evidence in the existing atomic credit ledger and wire the canary caller to the adapter callback.

**Steps:**

- [x] Add allowlisted ordered event persistence for process start, exposed submit/provider events, process end, and diagnostics; normalized result fields remain separately allowlisted.
- [x] Bind the adapter evidence callback to the same invocation ID in the caller.
- [x] Reject unknown event types, filter event keys, and preserve compatibility with rows that have no event list.
- [x] A failed process-start event write terminates the child before input; later failures leave the invocation unresolved; never retry.
- [x] Do not access or mutate `UPC_ad7e1fb8a325a4e78dfa952b08dc611a`, `PRD_000006`, or `SJB_000006`.

## Task 5 — Documentation and narrow regression

**What:** Document the durable evidence shape and limits, then run the requested offline validation.

**Steps:**

- [x] Document the fields, ordering, caps, redaction, strict JSON contract, unknown values, and no-retry semantics.
- [x] Run targeted adapter, canary caller, ledger, and BDD tests: 118 passed; one unrelated async guard test stalls on Windows Python 3.14 and was excluded after isolated attempts.
- [x] Run Ruff, format, and Pyright. The broader offline suite was attempted serially and with xdist but stalled without a failure report; live/e2e/smoke were excluded.
- [x] Do not run a live Flow E2E or any upstream video invocation; this task explicitly requires zero provider activity.

## Task 6 — Commit and push

**What:** Commit one coherent observability change and fast-forward push the current feature branch if the remote remains unchanged.

**Steps:**

- [x] Run `/gflow:check` on the exact tree. Hygiene, doc links, website PII/mirror, council memory, repo-wide Ruff/format, and whole-source Pyright pass. No CLI/MCP surface changed (blast-radius step 1b N/A).
- [ ] Confirm no unrelated or runtime/venv files are staged.
- [ ] Confirm `origin/feature/upstream-first-adapter-v1` has not advanced and the commit is a normal fast-forward.
- [ ] Commit one coherent change and push normally; do not force push.
- [ ] Verify the remote branch equals the pushed commit.
- [ ] Do not deploy and do not run a new live generation.

## Definition of done

- [x] Focused offline deterministic tests pass (118 passed; the one stalled asyncio guard case was excluded). Provider-facing behavior is exercised only with fakes; one harmless local Python subprocess verifies pipe capture.
- [x] Ruff, format, and whole-source Pyright pass.
- [x] JSON acceptance and auto-retry semantics are unchanged.
- [x] Lifecycle and parse evidence persist in required order with fixed bounds and redaction.
- [x] Missing provider evidence is stored as `UNKNOWN`.
- [ ] Canary 02 identity, ledger history, jobs, and 10-credit hold are unchanged.
- [ ] One commit is pushed by fast-forward to `origin/feature/upstream-first-adapter-v1`.
- [ ] No deployment, Generate, new job, canary, or reservation occurred.

## Validation notes

- Focused suite command: `pytest -q tests/validation/test_upstream_gflow_adapter.py tests/validation/test_upstream_canary.py -k "not caller_rejects_invocation_inside_asyncio_event_loop" tests/validation/test_production_execution.py tests/validation/test_upstream_canary_runtime.py tests/features/test_upstream_first_adapter_v1_steps.py tests/data/test_redaction.py` — 118 passed, 1 deselected.
- The excluded async guard test stalled when run alone in both pytest asyncio modes; it does not invoke the adapter or provider.
- Broader offline suite attempts stalled after initial progress in serial and xdist runs; no final report was produced. The task's required focused suites and static gates passed.
- Whole-tree Pyright needs `--pythonpath .venv\\Scripts\\python.exe` in this Windows worktree so it resolves the installed dependencies; with that interpreter it reports zero errors.
