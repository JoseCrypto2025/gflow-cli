# Predict: Ambiguous Legacy PROD Recovery Policy

## Verdict: CAUTION
**Confidence:** 6/10

## Summary
The policy addresses a real control gap: a local RUNNING status can invite automatic recovery even when provider fate remains unknown. The implementation can proceed if quarantine is distinct from HOLD, the state transition is exact and replay-safe across independent Sheet rows, and replacement eligibility never becomes authorization.

## Persona findings

### Architect — CAUTION (6/10)
Keep the policy module in the internal V2 runtime domain and put Sheets writes behind the existing production store. A focused policy audit/lineage ledger is justified because the operation spans two job rows and needs an append-only authorization boundary. Avoid changes to public CLI/MCP and generic queue abstractions.

### Security — CAUTION (5/10)
No auth, browser profile, provider transport, or Flow selectors are needed. Fail closed on exact identity/evidence mismatch, do not expose credential material in audit output, and retain the reservation and unknown billing. The evidence-exhausted result must be tied to this exact attempt and its prior reconciliation audit; do not treat missing evidence as positive refusal.

### Performance — CAUTION (7/10)
This is an operator-only path, outside generation hot paths. Bounded Sheet reads and row readbacks are acceptable. Keep the credit-ledger lock out of network calls; make each update idempotent so a lost acknowledgement can be reconciled without holding locks or repeating a provider operation.

### CLI and MCP UX — CAUTION (6/10)
No public capability or queued payload changes are needed. The operator report should distinguish local quarantine, provider fate UNKNOWN, unresolved existing exposure of 50, and unknown/unquoted future exposure. Do not imply that eligibility creates a replacement attempt.

### Devil's Advocate — CAUTION (6/10)
A new terminal quarantine state is the least ambiguous safeguard because existing HOLD intentionally resumes through recover_existing(). Keep future lineage enforcement local to the policy path and do not implement replacement creation or authorization in this task. A partial Sheet write must not be reported as complete.

## Conflict resolution and mitigations

- Multiple personas identified partial-write risk. Use exact expected-state checks, readbacks for both rows, durable idempotent policy audit, and replay-only recovery.
- Existing HOLD semantics conflict with quarantine requirements. Add QUARANTINED and preserve ordinary HOLD.
- User-reported exhaustion is not by itself provider evidence. Record its source as operator-reported, bind it to the existing audit digest, and do not rerun evidence recovery or infer refusal.
- Replacement eligibility is an assessment only. Store replacement_authorized=false and create no D2 attempt or credit reservation.
