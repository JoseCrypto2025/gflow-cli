# Predict: Legacy Replacement Quote and Authorization

## Verdict: GO
**Confidence:** 8/10

## Summary
The change can stay inside the V2 runtime and reuse the existing `PROVIDER_COST_MASTER` and
`D2DailyCreditLedger`. The quote resolver must fail closed when an effective, verified credit rate
does not uniquely match the full generation configuration. The current master has no applicable
row, so the current replacement remains on HOLD after implementation.

## Persona findings

### Architect — CAUTION (8/10)
Keep the quote resolver and decision value types in an internal replacement module. Add the
legacy-specific authorization lifecycle to the D2 ledger rather than changing the generic
production credit guard. The deterministic policy lineage key is not an authorization mechanism.
The TEST retry authorization is not reusable for this unresolved PROD parent.

### Security / reCAPTCHA — CAUTION (8/10)
No Flow, auth, selector, or provider transport change is needed. Require a unique, active,
effective, source-verified `CREDIT` rate; bind its row digest and all generation parameters; recheck
it when authorization is consumed. Never use historical accounted cost or convert monetary rates.
Consume authorization and create its linked D2 row in the same lock-protected ledger mutation.

### Performance / Playwright — GO (8/10)
Quote lookup is a local operation on an already validated canonical snapshot. It adds no browser
work or hot-path polling. Do not fetch another snapshot solely for quote resolution. The current
empty cost master should return `QUOTE_UNAVAILABLE` promptly.

### CLI UX / Cross-platform — CAUTION (8/10)
This is an internal capability with no CLI, MCP, queue-codec, or Flow surface. Use `Path` and the
existing cross-platform ledger lock and atomic-write conventions. Keep credentials and prompt text
out of authorization records. A later public interface requires a separate MCP parity review.

### Devil's Advocate — CAUTION (7/10)
The standard Phase G quote comes from the confirmation UI after its Generate click and cannot meet
the pre-attempt requirement. `PROVIDER_COST_MASTER` is the only existing non-generating canonical
source, but currently has no applicable row. Its current dimensions do not include duration,
aspect ratio, audio mode, or output count, so matching must fail closed when the source cannot
establish applicability. Existing retry authorization is a useful atomicity pattern but its
`UNEXPECTED_MULTI_OUTPUT` contract must remain unchanged.

## High-confidence risks (flagged by 2+ personas)

1. No authoritative cost row exists today; code may return `QUOTE_UNAVAILABLE`, not a numeric
   quote.
2. A historical 50-credit accounted amount and monetary provider rates cannot substitute for a
   direct credit quote.
3. A separate authorization file would create a cross-file transaction window; keep authorization,
   consumption, and replacement linkage in the same D2 ledger mutation.

## Conflicts resolved

- A separate audit file was considered, but the D2 ledger can hold the durable authorization and
  replacement link and atomically consume them with the new D2 row. This avoids cross-file
  transaction recovery and leaves the policy audit schema unchanged.
- The earlier quarantine plan deferred replacement authorization. This new, explicit task expands
  that scope; it does not change the frozen TEST retry contract.

## Required mitigations before EXECUTE

1. Preserve schema-2 ledger reads and the existing TEST/modern PROD semantics during any ledger
   version change.
2. Make quote applicability exact across provider/account/model/profile/mode and all populated
   cost-relevant parameters; do not treat omitted pricing dimensions as irrelevant without source
   proof.
3. Authorization must require an APPROVE decision, current quarantine, unchanged unresolved
   reservation, valid policy and reconciliation audits, and a valid quote.
4. Creation/consumption stays an explicit controlled API; no scheduler, retry worker, or ordinary
   orchestrator call receives replacement authority.
5. Implement and test crash-safe idempotent replay under the existing D2 ledger lock.

## Recommended next step

Build the BDD and focused offline test scenarios, then implement the fail-closed quote and
legacy-specific one-time authorization in the V2 runtime; do not apply it to the live parent.
