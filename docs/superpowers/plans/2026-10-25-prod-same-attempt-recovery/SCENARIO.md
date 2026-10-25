# Scenario: PROD Same-Attempt Recovery

## Coverage map

- **Active:** D4 ledger/recovery idempotency; D5 concurrency and transaction boundaries; D6 persistence/schema; D7 state and error handling; D8 runtime path portability; D9 captured provider transport evidence; D11 identity and evidence validation; D12 audit history; D13 surface parity decision.
- **Limited:** D1 identity is validated from the pinned execution context; no auth/session flow changes. D2 refusal evidence may be consumed, but this feature adds no CAPTCHA or WAF interaction.
- **Skipped:** D3 selector drift and D10 headed/headless behavior are outside this offline contract and implementation. No browser is opened and no Flow action is permitted.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D4 Recovery | A click was recorded without provider acknowledgement | Critical | Return `AMBIGUOUS_AFTER_RECONCILIATION`; preserve reservation and jobs on HOLD | Unit |
| 2 | D4 Recovery | One exact same-attempt provider receipt arrives late | Critical | Attach receipt to existing D2 attempt; do not create an attempt or Generate | Integration |
| 3 | D4 Recovery | Two candidates match one attempt | Critical | Reject the candidate set and keep the attempt ambiguous | Unit |
| 4 | D5 Concurrency | Two reconcilers race on the same attempt | Critical | A single idempotent receipt/state transition is recorded | Integration |
| 5 | D5 Transactions | D2 ledger write succeeds but credit guard write fails | Critical | Fail closed, preserve ambiguity, and retain enough audit data to retry reconciliation safely | Integration |
| 6 | D6 Schema | New PROD state is absent from the approved transaction schema | High | Validation rejects the write; schema and code state sets must be updated together | Unit |
| 7 | D6 Persistence | Receipt fields include unsupported or malformed identifiers | High | Reject invalid fields without corrupting the existing attempt | Unit |
| 8 | D7 State | No execution proof exists before provider call returns | Critical | Production and scene jobs remain `QUEUED` or transition to `HOLD`; never `RUNNING` | Integration |
| 9 | D7 State | Exact execution ID proves provider execution | Critical | Advance the existing scene job to `RUNNING`; keep production job consistent | Integration |
| 10 | D7 State | Exact output is proven without a locally stored execution ID | High | Reconcile only with exact project, scene, prompt, profile, and unique media identity | Unit |
| 11 | D8 Paths | Runtime state root contains Unicode or spaces on Windows | Medium | Ledger and lock paths remain under configured absolute root | Integration |
| 12 | D9 Transport | Submit response is observed but does not prove provider acceptance | Critical | Treat transport observation as diagnostic; do not classify submission as proven | Unit |
| 13 | D9 Transport | Provider-originated refusal is tied to the exact submit window | High | Classify no-submission only if the captured refusal explicitly proves no execution was accepted; otherwise HOLD | Unit |
| 14 | D11 Identity | Receipt identifies another account or project | Critical | Reject it and leave the exact attempt unresolved | Unit |
| 15 | D11 Identity | Receipt prompt hash, scene, generation profile, or attempt differs | Critical | Reject it; never match on recency or card order | Unit |
| 16 | D11 Freshness | Candidate predates the submit baseline or timestamp window | High | Reject stale evidence | Unit |
| 17 | D11 Assets | An unrelated newer asset appears in the project | Critical | Do not bind it; multiple/unrelated candidates remain ambiguous | Unit |
| 18 | D12 Audit | Late evidence is reconciled repeatedly | Critical | Append/retain a stable audit record and return the same result without duplicate reservation | Integration |
| 19 | D12 Billing | Visible credit balance is unchanged while submission is unknown | Critical | Keep billing outcome `UNKNOWN`; balance never proves submission state | Unit |
| 20 | D13 Surface parity | Internal runtime recovery entrypoint is added without a CLI/MCP command | Medium | Keep it internal; no generation MCP or worker payload changes | Review |
| 21 | D13 TEST compatibility | Existing TEST late-confirmation recovery runs against its existing fixtures | Critical | Preserve TEST contract and behavior unchanged | Regression |
| 22 | D13 Isolation | PROD recovery is invoked with TEST identity or TEST recovery with PROD identity | Critical | Fail closed at the environment boundary | Unit |
| 23 | D4 Retry | Reconciliation reports proven no-submission | Critical | Release only this attempt's reservation once; create no new attempt; caller controls any later attempt | Integration |
| 24 | D4 Unsupported proof | No authoritative post-click negative proof source exists | Critical | Keep `PROVEN_NOT_SUBMITTED` unavailable; never infer it from absence or timeout | Unit |
| 25 | D6 Identity | No `CONTENT_ID` exists in the approved model | Medium | Reconcile with existing production, scene, and D2 identities; do not invent a content key | Unit |

## Must-cover before merge (Critical + High)

1. Ambiguous outcomes remain held and keep their reservations.
2. Positive receipts match exact attempt, account, project, scene, prompt, and generation profile.
3. A recovered receipt never invokes Generate, creates an attempt, or reserves credits again.
4. No job is `RUNNING` before provider execution is proven.
5. Credit/job/ledger transitions are idempotent under duplicate and concurrent calls.
6. TEST recovery remains behavior-compatible and TEST-only.
7. `PROVEN_NOT_SUBMITTED` is enabled only with positive provider-originated proof that the exact request was not accepted; absent such proof, keep it unsupported.
8. Data dictionary, schema validation, production job transitions, and tests agree.

## Suggested BDD scenarios

These scenarios are code-only. They do not require Flow or a browser.

```gherkin
Feature: PROD same-attempt recovery
  Scenario: UI action is not proof of provider submission
    Given a PROD attempt has one recorded Generate UI action
    And no authoritative provider receipt is present
    When the attempt is reconciled
    Then the result is AMBIGUOUS_AFTER_RECONCILIATION
    And the credit reservation remains held

  Scenario: Exact late execution evidence resumes the existing attempt
    Given a PROD attempt is held with its original job and request identity
    And one provider execution receipt matches every pinned identity
    When the attempt is reconciled
    Then the receipt is attached to the existing attempt
    And the scene job may become RUNNING
    And no new attempt or credit reservation is created

  Scenario: An unrelated asset cannot be adopted
    Given a PROD attempt has a pinned project, scene, prompt hash, and baseline
    And a newer asset has a different prompt or scene identity
    When the attempt is reconciled
    Then the asset is rejected
    And the attempt remains on HOLD
```

## Known-issues cross-reference

- [`KNOWN_ISSUES.md`](../../../../KNOWN_ISSUES.md) § “interrupted post-submit tasks need manual reconciliation”: retain the conservative no-auto-retry behavior. This feature is a separate PROD runtime contract and does not claim the generic worker's authenticated SPA re-poll is verified.
- [`D2_SAME_ATTEMPT_RECOVERY_V1.md`](../../../ai-video-automation-v2/operations/D2_SAME_ATTEMPT_RECOVERY_V1.md): frozen TEST-only contract; preserve unchanged.
