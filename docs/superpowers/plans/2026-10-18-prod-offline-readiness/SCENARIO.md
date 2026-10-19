# Scenario: OAuth promotion, controlled PROD execution, and durable D7 readiness

## Coverage map

Active dimensions: D1 auth/session lifecycle (OAuth candidate promotion and refresh failures); D2 WAF/reCAPTCHA (never bypass and stop); D5 concurrency (single writer and one in-flight attempt); D6 data layer (bounded optimistic Sheets persistence); D7 error propagation (safe fail-closed outcomes); D8 cross-platform paths (Windows operator to Linux runtime); D9 transport (timeouts and uncertain acknowledgements); D11 input validation (identity, quotes, digests, ranges); D12 observability (safe status only). D3 selectors and D10 browser launch are unchanged offline. D4 batch resume is covered by idempotent job replay. D13 MCP does not apply because this is internal V2 runtime/operations code with no CLI or MCP leaf.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D1/D9 | Remote credential rename succeeds but SSH acknowledgement is lost | Critical | Digest-guarded status reconciliation, then canonical verification; otherwise rollback/hold without deleting an unrelated credential | Unit |
| 2 | D1 | Staged Google API verification fails | Critical | Canonical remains unchanged/absent and stage is removed | Unit |
| 3 | D7/D12 | SSH or Google error includes credential material | Critical | Operator error is stable and redacted | Unit |
| 4 | D5 | TEST execution context is paired with PROD account/project/profile | Critical | Reject before adapter binding, ledger mutation, or browser use | Unit |
| 5 | D5/D9 | A prior attempt is still unresolved or its quote is unknown | Critical | Shared provider/day budget refuses a second attempt; unknown quote fails closed | Unit |
| 6 | D2 | Provider returns WAF/unusual activity | Critical | Record WAF terminal state and prohibit retry/bypass | Unit |
| 7 | D11 | Quoted credit is money, exceeds remaining free budget, or implies paid fallback | Critical | Refuse before confirmation approval; no spend | Unit |
| 8 | D6/D5 | Two writers claim the same idempotency key or update a changed row | Critical | Serialize the local writer; compare expected row digest before bounded batch; read back; report conflict, never overwrite silently | Integration |
| 9 | D6/D9 | Sheets write times out after request may have applied | Critical | Read back by stable key and exact digest; reconcile or hold, never append a duplicate blindly | Integration |
| 10 | D4/D6 | Same production request is replayed after completion | Critical | Reuse the verified job/result and create no attempt or upload | Unit |
| 11 | D6 | Retry requires another Scene Job attempt | High | Append a new attempt; preserve previous attempt rows | Integration |
| 12 | D7/D11 | Drive upload returns but remote metadata or checksum differs | Critical | Keep local media; do not mark verified or complete | Integration |
| 13 | D7 | Drive upload acknowledgement is lost | High | Search by stable app properties/idempotency metadata and verify exact candidate before reusing; otherwise hold, no duplicate upload | Integration |
| 14 | D8 | Credential/runtime path is symlinked, escapes approved root, or has broad permissions | High | Fail closed without reading/writing outside approved path | Unit |
| 15 | D12 | Status report includes prompt, token, cookies, or account secrets | Critical | Report only safe codes and non-secret digests/IDs explicitly allowed by the contract | Unit |

## Must-cover before merge

1. Candidate OAuth promotion and rollback remain fail-closed under lost acknowledgements.
2. Explicit environment/provider/account/project/auth-profile identity is validated as one coherent tuple.
3. One shared daily free-credit ceiling applies across TEST and PROD; unknown reservations cannot be spent twice.
4. WAF and unknown post-submit states stop execution and preserve reservations.
5. Durable Sheets operations use bounded ranges, expected-value checks, readback, and explicit optimistic-concurrency limitations.
6. Drive verification proves metadata, size, and exact re-download SHA-256 before local cleanup.
7. Same-request replay produces no new generation, job attempt, or upload.

## Deferred

- True distributed compare-and-swap is unavailable through ordinary Sheets values/batchUpdate calls; use one VPS writer lock and document that external writers can still race.
- The D5 OAuth grant and canonical credential are verified. The earlier D5 F5 attempt held because `SPV_000006` failed fresh Phase E; after `SPV_000007` was approved, the 2026-10-19 non-generating F5 presubmit passed. D6 generation and Drive persistence remain gated on D2 reconciliation.
- Browser-only UI behavior is unchanged; no e2e generation test is run or needed for this offline state/store work.

## Suggested BDD scenarios

```gherkin
Feature: Fail-closed production automation readiness
  Scenario: Reject mixed TEST and PROD execution identity
    Given a PROD generation profile and a TEST provider account
    When the execution context is validated
    Then no browser or provider operation begins

  Scenario: Reconcile a Sheets write after an uncertain acknowledgement
    Given a bounded row update whose acknowledgement is lost
    When the store reads the stable row key back
    Then it reuses the exact intended value or reports a conflict without overwriting

  Scenario: Preserve local media until Drive checksum verification
    Given a validated local video and a mismatched remote download
    When Drive persistence is verified
    Then the local video remains and the Scene Job cannot complete
```

## Known-issues cross-reference

- [`KNOWN_ISSUES.md`](../../../KNOWN_ISSUES.md) documents WAF/reCAPTCHA and migrated Flow limitations. This work adds no stealth, selector bypass, or retry around those states.
- [`docs/ai-video-automation-v2/operations/VPS_BROWSER_RUNTIME.md`](../../ai-video-automation-v2/operations/VPS_BROWSER_RUNTIME.md) records the absent Google Workspace credential and D5 hold; this remains the live gate.
