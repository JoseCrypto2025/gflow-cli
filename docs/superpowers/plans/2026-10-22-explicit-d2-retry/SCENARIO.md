# Scenario: Explicit one-time D2 retry authorization

## Coverage map

Active dimensions: D2 WAF stop, D5 ledger-lock concurrency/replay, D6 ledger
accounting, D7 fail-closed errors, and D8 portable ledger persistence. Browser
selectors, provider transport, CLI/MCP parity, and Flow behavior are unchanged.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D6 / D7 | Paid terminal predecessor, no authorization | Critical | Default creation remains denied | Unit |
| 2 | D7 | Exact terminal multi-output predecessor and exact authorization | Critical | One new zero-action row; authorization marker persisted atomically | Unit |
| 3 | D5 / D7 | Reuse authorization or duplicate new run ID | Critical | Second row denied under ledger lock | Unit |
| 4 | D2 | Predecessor is WAF rejected | Critical | Retry remains blocked | Unit |
| 5 | D7 | Predecessor is successful, unresolved, missing, or mismatched | Critical | Fail closed with stable policy error | Unit |
| 6 | D6 | Daily credits already reach 50 | Critical | Retry row is not created; accounting is unchanged | Unit |
| 7 | D6 | Prior 10-credit charge remains present | High | Old row remains unchanged; approval gate still enforces the shared ceiling | Unit |

## Must-cover before merge

All seven scenarios are covered by `tests/validation/test_phase_g.py`. The live
Phase G E2E remains paid and explicitly opt-in; this policy change does not run
it.

## Deferred

No additional retry states, arbitrary retry counts, same-run retries, or new
CLI/MCP surfaces are in scope.

## Known-issues cross-reference

None. The retry authorization does not address auth or Flow transport issues.
