# Scenario: Read Flow Credits on the Migrated App

## Coverage map

Active dimensions: D1 auth/session lifecycle (signed-out and `/about` landings), D5 browser page lifecycle, D7 typed error propagation, D8 Windows profile paths, D12 safe observability, and D13 CLI/MCP parity. D2 WAF is relevant only as a read-only navigation concern; the reader must not click or submit. D3 selectors are skipped because the balance is read from wire traffic. D4, D6, D9 generation/download paths, D10 headless-mode availability beyond existing settings, and D11 generation input validation do not apply.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D1 Auth | Flow opens signed-out | High | Raise `AuthExpiredError`; `--refresh` may use only the existing expired-session renewal path | Integration + E2E auth |
| 2 | D1 Auth | Flow opens `/about` | High | Raise the current typed landing error; do not misreport a retired labs endpoint failure | Integration |
| 3 | D5 Page pool | RPC read succeeds or fails | High | Always return the checked-out page in `finally` | Unit/integration |
| 4 | D5 Timeout | Navigation plus RPC does not produce a balance before the deadline | High | Raise a typed app/read timeout within one total bounded duration | Integration |
| 5 | D7 Wire | Unrelated RPCs arrive before `nzlxg` | High | Ignore them and return only the validated balance | Integration |
| 6 | D7 Wire | Body is unreadable or `nzlxg` is malformed | High | Fail closed with a fixed, body-free typed error | Integration |
| 7 | D7 Wire | Flow explicitly refuses `nzlxg` | High | Raise a typed wire/app error without logging the body | Integration |
| 8 | D12 Privacy | HAR capture is configured | Critical | The canonical credits service disables HAR recording before launching its browser context | Unit/integration |
| 9 | D12 Observability | Successful or failed read | High | Logs/errors contain no raw body, token, cookie, or signed URL | Integration |
| 10 | D13 MCP | Agent calls `gflow_get_credits` | High | Preserve the result keys and report tier fields as `null` when unknown | Unit + E2E auth |
| 11 | D8 Paths | Profile path contains Windows spaces/Unicode | Medium | Existing profile/client handling continues to work without path construction changes | Unit |

## Must-cover before merge (Critical + High)

1. Strictly validate `nzlxg` reply index `[0]` as a nonnegative exact integer; reject booleans, strings, missing slots, and malformed top-level shapes.
2. Observe the app's own `nzlxg` response after normal Flow navigation; ignore unrelated RPCs and never replay the private RPC.
3. Preserve signed-out and `/about` typed landing diagnoses, and use a single total observation deadline.
4. Always check the page back in, including on timeout, refusal, and malformed payload.
5. Disable raw HAR capture for the credit-read operation before browser launch.
6. Keep response text, frame payloads, and exception material out of logs and persisted artifacts.
7. Preserve CLI/MCP JSON compatibility and existing refresh semantics; do not renew on a missing RPC or Flow app error.
8. Run a live read-only TEST-profile credit check and a zero-credit intercepted Agent-only canary before any paid D2 gate.

## Deferred (Medium + Low — log as issues, not blockers)

1. Reduce startup cost for `credits list` across many profiles if live measurements show material delay; use the existing client/page-pool lifecycle for this change.
2. Add headless-specific support only if the current configured TEST profile demonstrates a real requirement; no new mode is introduced here.

## Suggested BDD scenarios

```gherkin
@e2e @e2e_auth
Feature: Read credits from the migrated Flow app
  Scenario: Read the authenticated TEST profile balance without generating
    Given the TEST profile is authenticated on flow.google.com
    When I run the credits user command
    Then the balance is a non-negative integer from the app's nzlxg response
    And no generation control is clicked
```

The browser-only scenario belongs in `tests/e2e/`; parser, routing, timeout, privacy, and CLI/MCP result-shape cases stay in offline tests.

## Known-issues cross-reference

`KNOWN_ISSUES.md` #795 documents that the old labs credit route fails on migrated accounts. This port resolves that read path by observing the migrated app's normal `nzlxg` response. It does not change generation or credit billing.
