# Scenario: Phase G D2 controlled execution

## Coverage map
Active: D1 auth/profile lease; D2 WAF; D3 UI drift; D5 concurrency; D6 local ledger; D7 stable failure states; D8 Windows paths; D9 wire parsing/downloads; D10 headed profile; D11 request/count/cost validation; D12 sanitized evidence. D4 batch resume is out of scope because D2 is one request per attempt. D13 MCP parity is out of scope because Phase G is not exposed as a CLI or MCP command.

## Must-cover scenarios

| Severity | Scenario | Expected behavior | Test category |
|---|---|---|---|
| Critical | Duplicate Generate on one attempt | Reject before UI interaction; action count remains one | Unit / integration |
| Critical | Timeout after Generate | `UNKNOWN_SUBMISSION_STATE`, non-retryable; reserve quoted cost or remaining budget and reconcile only | Unit / integration |
| Critical | New confirmation does not prove count=1 or positive cost | Do not approve; account zero only when no approval occurred | Unit / integration |
| Critical | WAF refusal in batchexecute envelope | Raise non-retryable WAF result and stop this task/day | Integration |
| Critical | Settings mutation fails or restore readback differs | Keep recovery journal dirty; block all later attempts | Integration |
| Critical | Stale/old media or wrong project | Reject attribution; never select latest card | Integration |
| Critical | More than one attributable new media ID | Record all IDs and stop; no second request | Integration |
| High | Personal profile, wrong account, or wrong TEST project | Reject before mutating Flow | Integration / live zero-credit |
| High | Prompt, canonical duration mapping, or settings readback differs | Reject before Generate | Unit / live zero-credit |
| High | Daily reserved plus quoted credit exceeds 50 or visible balance | Reject confirmation | Unit |
| High | Download or full decode fails | Retain local evidence; no retry | Integration |
| High | Drive upload succeeds but re-download hash differs | Mark persistence failed and keep local file | Integration |
| High | Concurrent ledger writes | Serialize updates and preserve no-overrun budget | Unit |

## Deferred
MCP/CLI parity, production Sheets job rows, production Drive API, VPS deployment, and additional generation contracts remain deferred until their own gates.

## Live scenario
One controlled local T2V attempt only after the entire offline suite, commit/push, fresh Phase E approval check, exact live UI/cost gate, and clean recovery state pass. WAF or ambiguous post-submit state ends live testing.
