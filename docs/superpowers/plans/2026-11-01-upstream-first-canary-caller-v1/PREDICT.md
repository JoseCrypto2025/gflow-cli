# Predict: Upstream-first Canary Caller V1

## Verdict: CAUTION

**Confidence:** 6.8/10

## Summary

Five independent reviews support a narrow, canary-only bridge around the existing Production Job,
credit guard, adapter, media validator, and Drive storage. The principal risk is that ordinary
Production/Scene Job completion currently requires provider execution evidence that the upstream
adapter does not return; the caller must persist a distinct artifact result and refuse to invent
lineage. Durable one-shot invocation and quote reservation must use the existing shared ledger,
not adapter memory or a second credit ledger.

## Persona findings

### Architect — CAUTION (8/10)

The caller fits as a separate canary bridge composing `ProductionJobCoordinator`, the shared
`ProductionCreditGuard`, canonical prompt/catalog readers, the adapter, artifact validator, and
Drive storage. It must remain outside `ProductionOrchestrator`'s `SceneGenerationPort`: upstream
JSON lacks the provider execution/account/project evidence required by the normal production result.
The adapter contract documents this limitation and says no caller currently exists.

### Security / reCAPTCHA — CAUTION (6/10)

The adapter trusts caller-supplied authorization and identity, uses an in-memory key set, and labels
project provenance `REQUEST_ARGUMENT_ONLY`. The caller must bind durable job/invocation IDs, prompt
SHA, profile/account/project, quote, reservation, and upstream pins; persist invocation-start before
launch; store only allowlisted result data/digests; and fail closed on uncertain writes. Parent
profile-lease ownership must be released before the child starts.

### Performance / Playwright — CAUTION (7/10)

`generate_once()` is synchronous and may block for up to 900 seconds. Run it outside an asyncio event
loop or shared production worker, serialize per-profile access, and create one unique staging
directory per invocation. A post-launch timeout or interruption is UNKNOWN/HOLD, never a retry.

### CLI UX / Cross-platform — CAUTION (7/10)

Keep this internal to `scripts/operations` and the V2 runtime; no public CLI or MCP twin is needed.
Use platform-neutral `Path` validation, safe normalized statuses, and never persist prompt text, raw
stdout/stderr, tokens, cookies, or raw tracebacks. Upstream success means a local MP4 was produced;
it does not establish account/project verification, media validity, Drive persistence, or production
finalization.

### Devil's Advocate — CAUTION (6/10)

Reusing the existing production operator end-to-end would invoke Phase G and require D2 lineage; the
normal orchestrator rejects PROD generation without its bound Phase G adapter. Reusing only the
existing `ProductionCreditGuard` also needs care: its ordinary `reserve_quote()` path is after a
recorded Generate click, while its pre-submit reservation path is replacement-specific. Extend the
shared guard narrowly for a distinct upstream invocation state instead of adding another credit
ledger. A successful canary artifact cannot transition an ordinary Production Job to COMPLETED
without a truthful provider execution identity. The source pin must be verified before the later live
canary; a `--version` response is insufficient.

## High-confidence risks (flagged by 2+ personas)

1. In-memory idempotency is insufficient; invocation-start must be durable and atomic before launch.
2. Exact quote reservation needs a dedicated transition in the existing shared guard; do not create a
   parallel billing ledger.
3. Upstream output cannot satisfy normal Production Job finalization without provider execution and
   account/project evidence; never fabricate these fields.
4. Parent/child profile lease contention and long synchronous process blocking must be handled.

## Conflicts resolved

- **Reuse existing production path vs remain detached:** reuse job/credit/artifact/storage components,
  but do not wire the adapter through `ProductionOrchestrator` or switch the default generation path.
- **Complete job on MP4 success vs preserve evidence boundaries:** validate and persist a canary
  artifact, while ordinary Production Job completion remains blocked until an honest lineage contract
  exists.
- **Use the existing credit guard vs reserve before submit:** extend that guard's locked ledger with
  explicit upstream states; do not misuse Phase G post-click transitions or create a second ledger.

## Required mitigations before EXECUTE

1. Atomic one-shot checkpoint and exact quote reservation in the existing shared ledger.
2. Separate canary artifact outcome from ordinary Production Job `COMPLETED`.
3. No parent-held profile lease at child launch; synchronous isolated call.
4. Replay/unknown/download failures never invoke adapter again.
5. Keep caller out of default traffic and require artifact source-pin verification before Canary 01.

## Recommended next step

Implement the offline caller under `docs/superpowers/plans/2026-11-01-upstream-first-canary-caller-v1/PLAN.md`,
starting with tests for durable quote reservation, replay, and one-shot checkpoint ordering.
