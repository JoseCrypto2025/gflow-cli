# Scenario: TEST D2 Session Credit Authorization

## Coverage map

Active dimensions: D1 (profile/account binding), D5 (concurrent coordinators),
D6 (ledger migration and atomic persisted state), D7 (stable fail-closed
errors), D8 (Windows path handling), D11 (credit/session boundaries), and D12
(sanitized audit events). D2 WAF behavior remains a hard stop and is not
changed. D3 selector behavior and D9 transport behavior are unchanged. D10 is
covered by the existing Windows headed-browser validation. D13 has no CLI or
MCP surface: the authorization is an internal Phase G operator contract.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D6 ledger | Upgrade an existing schema-v1 ledger containing historical rows | Critical | Preserve every historical row and reservation; add only the new versioned session container | Unit |
| 2 | D1 binding | Authorization names another account, project, profile, or runtime root | Critical | Reject before attempt creation | Unit |
| 3 | D11 budget | Historical daily reservation is 50, session authorization is 40 | Critical | Preserve historical 50 and permit only explicit authorized TEST-session reservations, capped at 40 | Unit |
| 4 | D11 budget | Quote exceeds remaining session allowance or live balance | Critical | Refuse before approval; never permit a second approval | Unit |
| 5 | D5 concurrency | Two coordinators reserve the same session concurrently | Critical | Atomic ledger lock permits at most one in-flight attempt and no cap overrun | Integration |
| 6 | D6 crash recovery | Process stops after session reserve but before Generate | High | Keep the reservation/in-flight hold; require operator reconciliation before reuse | Unit |
| 7 | D6 crash recovery | Process stops after Generate dispatch before terminal evidence | Critical | Mark session stopped/unknown and retain the full unused session cap | Unit |
| 8 | D7 outcome | Attempt ends in unknown submission or billing outcome | Critical | Block every subsequent session attempt | Unit |
| 9 | D7 outcome | Attempt is terminal and exact balance delta is reconciled | High | Account observed spend once; allow another attempt only if all normal gates pass and session cap remains | Unit |
| 10 | D7 outcome | Attempt reaches verified D2 success | Critical | Permanently close the session against another D2 | Unit |
| 11 | D2 WAF | Provider returns WAF refusal | Critical | Keep existing global WAF stop; session authorization cannot override it | Unit |
| 12 | D6 replay | Authorization digest/session ID is reused or stale | Critical | Reject under the ledger lock before any UI action | Unit |
| 13 | D8 paths | Windows runtime root contains spaces | High | Canonical resolved path is compared and stored without separator ambiguity | Unit |
| 14 | D12 redaction | Audit output is produced | High | Persist IDs, amounts, hashes, states, and timestamps only; never prompts, tokens, cookies, or response bodies | Unit |
| 15 | D6 policy isolation | Ordinary TEST or PROD request omits the exact session authorization | Critical | Existing fixed 50-credit behavior remains unchanged | Unit |

## Must-cover before merge (Critical + High)

1. Schema migration retains historical ledger rows and their values exactly.
2. Exact TEST identity, profile, project, root, expiry, authorization digest,
   and maximum of 40 new credits are checked at creation, Generate, and quote
   reservation.
3. A lock-protected session permits only one in-flight attempt; replay,
   concurrency, wrong lane, malformed authorization, and stale ledger fail
   closed.
4. Quotes and observed balance deltas are separately recorded. Unknown outcome
   stops the session and retains its remaining reservation.
5. Verified success, WAF refusal, and artifact-recovery states cannot be
   followed by another Generate.
6. Requests without the exact TEST session authorization retain default
   daily-budget enforcement, including PROD.

## Deferred (Medium + Low — log as issues, not blockers)

1. Cross-device synchronization is out of scope; the authorization is bound to
   one local runtime root and its atomic ledger.
2. Provider-side billing that is not reflected by either the quote or the
   before/after balance remains unknown and stops the session.

## Suggested BDD scenarios

```gherkin
Feature: bounded TEST D2 session credit authorization
  Scenario: preserve historical daily reservation while authorizing a bounded TEST session
    Given a ledger with a preserved historical 50-credit reservation
    And an exact TEST authorization capped at 40 new credits
    When one authorized TEST attempt is reserved
    Then the historical row is unchanged
    And the attempt is bound to the exact TEST account and project

  Scenario: stop the session when provider outcome is unknown
    Given an authorized TEST session with remaining credit allowance
    When an attempt has an unknown provider or billing outcome
    Then the session becomes stopped
    And a second attempt is rejected before Generate

  Scenario: reject concurrent session attempts
    Given one authorized TEST session
    When two coordinators try to reserve distinct attempts concurrently
    Then exactly one may become in flight
    And the cumulative authorized amount cannot exceed 40 credits
```

## Known-issues cross-reference

No `KNOWN_ISSUES.md` entry changes this decision. The existing fixed daily
budget and the V3 unknown-closure rule are intentionally preserved for ordinary
execution; this proposal adds a separately versioned, exact TEST-session
exception only.
