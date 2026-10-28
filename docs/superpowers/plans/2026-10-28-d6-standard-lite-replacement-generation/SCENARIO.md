# Scenario: D6 Standard Lite Replacement Generation

## Coverage map

- **D6 Data:** model-aware quote scope, canonical 18-column cost row, source/effective-date checks,
  output multiplier, authorization and exposure digests.
- **D7 Errors:** every stale, duplicate, mismatched, or incomplete precondition fails closed.
- **D8 Cross-platform:** Windows and POSIX ledger locks, exact replay, and crash-safe reservation
  state transitions.
- **D9 Transport:** existing Phase G submit evidence and exact same-attempt reconciliation remain
  authoritative; the replacement operator adds no transport behavior.
- **D10 Runtime:** use the configured production runtime, existing `gflow-prod` profile, and
  deployment roots only.
- **D12 Audit:** versioned pricing/authorization semantics and replacement lineage bind parent,
  child jobs, quote row, source, decision, and software commit.
- **D13 MCP:** no public CLI/MCP/queue capability is added; the operator remains an internal
  operations script.

D1/D2/D3/D4/D5 and new selector behavior are out of scope; the existing Phase G browser and
`ProfileLease` path is reused without auth, reCAPTCHA, locale, or Page-pool changes.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D6 Data | Exactly one effective `Veo 3.1 - Lite` / `NON_ULTRA` / account-specific `CREDIT` row exists | Critical | Quote the official per-generation rate multiplied by output count | Unit |
| 2 | D6 Data | Output count is N | Critical | Amount is rate × N; input digest binds N | Unit |
| 3 | D6 Data | Resolution, audio, and quality are blank; duration is 8 and aspect is 9:16 | Critical | These non-pricing dimensions do not prevent the standard Lite quote | Unit |
| 4 | D6 Data | Provider, account, model, pricing class, mode, source, status, or effective date differs | Critical | `QUOTE_UNAVAILABLE`; no fallback to another row | Unit |
| 5 | D6 Data | Cost row is duplicate, monetary, missing provenance, or not the official Flow pricing page | Critical | Fail closed | Unit |
| 6 | D6 Data | Legacy parent has a historical 50-credit accounting value | Critical | It is unresolved exposure only; it never becomes the replacement quote | Unit |
| 7 | D6 Data | Quote is calculated | Critical | No Generate, authorization, attempt, or reservation is created | Unit |
| 8 | D6 Data | Parent, policy audit, reconciliation audit, or quote changes before authorization consumption | Critical | Hold without consuming a different authorization or creating another attempt | Integration |
| 9 | D6/D12 | Authorization binds parent jobs and the exact new replacement job IDs | Critical | One durable V2 authorization; `generate_authorized=false` | Integration |
| 10 | D6/D8 | Crash after child job rows, authorization, or D2 consumption | Critical | Exact replay reuses the same idempotency key and IDs; no second child, authorization, or attempt | Integration |
| 11 | D6 | Shared daily guard contains another unresolved same-day provider attempt | Critical | Hold; do not bypass or release it | Integration |
| 12 | D6 | Exact quote is pre-reserved before Generate | Critical | Reserve only after Phase G readbacks and baseline; enforce current free balance and the 50-credit daily cap | Integration |
| 13 | D6/D9 | Flow confirmation reports a count or price different from the authorization | Critical | Do not approve; preserve the same attempt and reservation for reconciliation | Integration |
| 14 | D6/D9 | One Generate click is recorded, then provider evidence is ambiguous | Critical | Reconcile only this attempt; stop without retry and retain its reservation | Integration/live |
| 15 | D6/D9 | Exact output is recovered | High | Validate provider/media identity, file, full decode, duration, aspect, corruption, Scene and Prompt links; stop before D7 upload/finalization | Integration/live |
| 16 | D13 | Internal replacement operator is added | Medium | No CLI/MCP/worker payload changes; record parity as not applicable | Static/review |

## Must-cover before merge

1. Official Standard Lite source and explicit `NON_ULTRA` scope are required; the separate
   lower-priority variant never matches.
2. Null/non-pricing dimensions do not become fabricated keys. The complete runtime config and
   matched 18-column row remain bound in the quote/decision digest.
3. Existing legacy 50-credit reservation and UNKNOWN billing remain unchanged.
4. The dedicated operator is the only path that can consume the V2 replacement authorization.
   Normal orchestration, retry, resume, and the generic production operator stay closed.
5. Multi-store partial failures are non-generating and exact-replay-only. No cleanup deletes or
   overwrites persisted child rows, authorization, attempt, or reservation.
6. The pre-Generate replacement reservation has its own state transition; existing daily budget,
   current free-balance, and unresolved same-day protections remain in force.
7. The displayed Flow confirmation may be approved only once and only if it proves one video at
   the exact authorized quote. A mismatch holds without approval.
8. Ambiguous evidence stops on the same attempt. The legacy parent is never retried or released.
9. No D7 upload, FINAL_OUTPUT write, or D8 work occurs in this task.

## Suggested BDD scenarios

```gherkin
Feature: D6 Standard Lite replacement quote
  Scenario: Quote the account-specific non-Ultra per-generation rate
    Given one effective official Standard Lite NON_ULTRA cost row
    And the request has 8-second duration, 9:16 aspect, and one output
    When the replacement quote is calculated
    Then the quote is 10 credits
    And no attempt, reservation, approval, or Generate action is created
```

```gherkin
Feature: D6 controlled legacy replacement
  Scenario: A changed confirmation amount does not receive approval
    Given one consumed replacement authorization and a pre-Generate reservation
    When Flow displays a confirmation amount different from the quote
    Then no confirmation approval or second Generate occurs
    And the same attempt and reservation remain available for reconciliation
```

These scenarios are bound to offline code and live operator tests respectively. The actual
production run is a single opt-in live verification; it must never be duplicated by a second
test invocation.

## Known-issues cross-reference

- `KNOWN_ISSUES.md` states an `UNKNOWN_SUBMISSION_STATE` attempt must not be automatically
  retried. This scenario preserves that boundary for the legacy parent and the replacement.
- The V1 replacement contract's exact-dimension quote scope is historical. The V2 contract
  supersedes it for model-priced Standard Lite rows while preserving the V1 contract unchanged.
