# Non-Ultra Peak Window Advisory

## Scope

The internal PROD replacement operator serves one already-authorized legacy
replacement attempt. Google's 14:00–17:00 UTC non-subscriber notice is an
availability warning. Flow decides whether a request is accepted. The operator
must retain the one-attempt, one-click, no-retry boundary and the unresolved
credit reservation.

This changes the existing Phase G submission path. There is no CLI or MCP
schema change. The separately authorized use of the existing attempt is its
live verification; no additional generation test is permitted.

## Acceptance scenarios

| Severity | Scenario | Expected behavior | Verification |
|---|---|---|---|
| Critical | Quote or execute begins during 14:00–17:00 UTC | Report `peak_window_advisory=true`; continue exact preconditions; no time-only hold | Operator tests |
| Critical | Existing consumed authorization is replayed | Re-read parent evidence and confirm the same ambiguous result and policy link before proceeding | Operator test and fresh operator checkpoint |
| Critical | Exact `YhhmEf` submit response contains one safe structured RPC reason | Persist only the reason token and response facts; stop; reconcile the same attempt; retain UNKNOWN exposure; never retry | Phase G and reconciliation tests |
| Critical | Exact WAF reason appears in an HTTP 200 submit response | Preserve the WAF reason, but keep the attempt UNKNOWN; the response does not satisfy the HTTP 403 non-submission proof | Live-flow and reconciliation tests |
| Critical | Submit response is WAF HTTP 403 with `PUBLIC_ERROR_UNUSUAL_ACTIVITY` | Preserve current narrowly correlated non-submission classification | Existing WAF tests |
| High | Submit error has no structured reason, an unsafe reason, or belongs to another RPC | Do not persist response body or infer capacity; preserve conservative ambiguity | Phase G evidence validation tests |
| Critical | Any post-click result is ambiguous | Keep the existing reservation, attempt identity, parent linkage, and action counts | Reconciliation tests and one authorized live run |

## Out of scope

No Generate retry, new attempt, new authorization, approval expansion, parent
reservation change, provider-cost master-data change, D7/D8 entry, or unrelated
Flow action is allowed.
