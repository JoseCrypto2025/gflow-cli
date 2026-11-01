# Scenario: Upstream-first Canary Caller V1

## Predict summary

**Verdict: CAUTION — 6.8/10.** A narrow, canary-only bridge can reuse production jobs,
the shared credit guard, canonical Master Data readers, the artifact validator, and Drive
storage. It must remain disconnected from routine `ProductionOrchestrator` traffic because
the pinned upstream result does not provide provider execution or account/project readback
identities. Persist the one-shot decision and quote reservation before child launch; never
let adapter memory or a caller boolean serve as the durable gate.

## Coverage map

- **Active:** D1 (account/profile binding), D4 (replay), D5 (concurrent claims), D6 (durable
  ledger and uncertain writes), D7 (normalized failures), D8 (staging paths), D9 (upstream
  response drift), D10 (blocking subprocess and profile lease lifecycle), D11 (strict config
  and quote inputs), D12 (safe persisted evidence).
- **D2 WAF/reCAPTCHA:** no caller-side token minting; child CLI owns its browser path. A child
  WAF/authorization failure must be represented without retry.
- **D3 selectors/locales:** caller adds no selectors; selector behavior remains inside the
  pinned upstream CLI.
- **D13 MCP parity:** no public gflow CLI or MCP surface is added. This is an internal
  operations caller; if a public command is later added, its MCP twin is a separate required
  scope.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D1/D11 | Approved prompt ID resolves to a different scene, profile, or SHA | Critical | Reject before claim/reservation/invocation | Unit |
| 2 | D1 | Live identity evidence is stale, mismatched, or project is not exact | Critical | Reject before invocation; never use latest-project fallback | Unit |
| 3 | D4/D6 | Same request key is submitted twice before launch | Critical | Reuse exact Production/Scene Job and invocation IDs | Integration |
| 4 | D4/D6 | Replay after durable invocation-start, success, or unknown outcome | Critical | Return persisted state; never call adapter again | Integration |
| 5 | D5/D6 | Two caller processes race on one request key | Critical | One durable invocation-start wins; at most one adapter call | Integration |
| 6 | D6 | Reservation write or checkpoint write fails/acknowledgement is uncertain | Critical | Do not launch child; preserve durable state and fail closed | Integration |
| 7 | D6/D11 | Quote absent, invalid, over the 30-credit authorization, or over remaining shared daily budget | Critical | Reject before reservation or adapter invocation | Unit |
| 8 | D6 | Reservation is written | Critical | Reserved amount equals the fresh quote exactly; daily ceiling never enlarges it | Unit |
| 9 | D10 | Caller holds the profile lease when starting the upstream child | High | Do not invoke; parent must release/close its profile lease first | Integration |
| 10 | D10 | Child times out, is interrupted, or returns malformed output after launch | Critical | Persist UNKNOWN/HOLD, preserve reservation, forbid replay | Integration |
| 11 | D7/D9 | Adapter returns each normalized status, including auth/project/credit/download errors | High | Persist safe outcome; no automatic invocation retry | Integration |
| 12 | D9/D12 | Adapter success has no provider execution ID or account/project readback | Critical | Validate and store the artifact, but do not fabricate lineage or mark the normal Production Job `COMPLETED` | Integration |
| 13 | D8/D11 | Staging path is relative, outside the invocation workspace, occupied, or shared | High | Reject; use one unused per-invocation output path | Unit |
| 14 | D9 | MP4 validation or Drive verification fails after generation | Critical | Preserve same invocation and reservation; recover existing asset only | Integration |
| 15 | D12 | Persisting the adapter result would include prompt, raw stdout/stderr, cookie, token, or traceback | High | Persist only allowlisted fields/digests | Unit |

## Must-cover before merge (Critical + High)

1. Durable production/request/invocation identity is reused on replay; concurrent callers cannot
   both cross the invocation-start gate.
2. Exact quote, authorized per-run ceiling, and shared daily credit limit are enforced, with
   reservation equal to quote before adapter invocation.
3. Durable invocation-start is written before child launch. Any replay after that point returns
   HOLD and never calls the adapter.
4. PRE_SUBMIT_FAILURE, AUTH_FAILURE, PROJECT_NAVIGATION_FAILURE, INSUFFICIENT_CREDITS,
   POST_SUBMIT_UNKNOWN, DOWNLOAD_FAILURE_AFTER_GENERATION, and malformed results never trigger
   another invocation.
5. No parent profile lease is held while the upstream CLI starts. The caller itself adds no
   browser or selector logic.
6. Successful output uses the existing validator and Drive upload/re-download SHA check. Because
   upstream lacks execution identity and project/account readback, the caller must not mark a
   normal Production Job complete or construct `GeneratedSceneAsset` from synthetic values.
7. The caller remains outside `ProductionOrchestrator` default routing; Phase G, D2 and D6
   behavior are unchanged.

## Deferred

- A real Flow e2e/canary is prohibited in this implementation task and remains the separate
  authorized Canary 01 task.
- A production status/schema that finalizes an upstream result as a normal completed Production
  Job is deferred until the provider supplies adequate execution/account/project lineage or a
  separately reviewed schema contract defines honest upstream provenance.
- Public CLI/MCP access is out of scope.

## Suggested BDD scenarios

```gherkin
Feature: Durable upstream-first canary invocation
  Scenario: A replay after invocation-start does not call the adapter again
    Given a canary invocation with a durable READY checkpoint
    When the caller persists invocation-start and calls the adapter once
    And the caller is restarted and receives the same request idempotency key
    Then the same invocation identity is returned in HOLD
    And the adapter call count remains one

  Scenario: An unknown post-launch result preserves the quote and blocks replay
    Given a canary invocation with an exact quote reservation
    When the adapter returns POST_SUBMIT_UNKNOWN
    Then the reservation remains equal to the validated quote
    And the invocation is held for same-invocation reconciliation
    And a replay does not call the adapter

  Scenario: A successful upstream asset is validated and stored without fabricated lineage
    Given a canary invocation with a matching successful adapter result and staged MP4
    When media validation and Drive remote re-download pass
    Then the canary artifact is durably recorded
    And no provider execution identity is fabricated
    And the normal Production Job is not marked COMPLETED
```

## Known-issues cross-reference

No existing `KNOWN_ISSUES.md` entry directly covers the caller. The documented upstream contract
gap—missing provider execution/account/project readback—is the governing limitation and is
preserved rather than hidden.
