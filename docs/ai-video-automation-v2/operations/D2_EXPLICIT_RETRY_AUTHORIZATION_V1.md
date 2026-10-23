# D2 Explicit Retry Authorization V1

This contract permits at most one distinct D2 retry row after a verified,
terminal `UNEXPECTED_MULTI_OUTPUT` attempt. It does not authorize a second
Generate action for an existing run, change historical evidence, release a
credit reservation, or establish a provider billing outcome.

## Authorization binding

The operator supplies a typed `D2RetryAuthorization` to the existing Phase G
execution path. It must name the exact predecessor `D2_*` run ID and the
expected terminal state `UNEXPECTED_MULTI_OUTPUT`. The ledger is authoritative:
the predecessor must exist and its submission and terminal states must both
match the authorization, with exactly one recorded Generate action.

The retry is rejected when the predecessor is missing, mismatched, unresolved,
successful, WAF-rejected, or nonterminal. The existing settings recovery
journal must pass `require_clean_or_missing()` before execution proceeds. The
current Bangkok-day accounted amount must be below 50 credits at row creation;
the existing approval gate still checks the new quote against the same 50-credit
ceiling, including all retained historical accounting.

The durable Generate-action recorder checks the locked current-day accounted
total once more immediately before the UI click. At or above 50 credits, the
action count remains zero and the UI click is not reached. A later Bangkok
accounting date evaluates independently; historical reservations retain their
original dates.

Normal `create_attempt()` remains deny-by-default after a paid terminal
attempt, except where a separate forensic-closure contract already permits a
distinct run. The narrow misattribution-closure exception can consume only the
exact predecessor named by its closed, digest-verified retry descendant; it
does not authorize a second retry. See
[`D2_FORENSIC_MISATTRIBUTION_CLOSURE_V2.md`](D2_FORENSIC_MISATTRIBUTION_CLOSURE_V2.md).
The retry authorization is consumed atomically with creation of
the new `PREPARED` row under the ledger lock. The new row stores the contract
version, predecessor ID, expected state, and consumption timestamp. The prior
row is not edited. A consumed marker prevents reuse of the authorization or a
second authorized retry in the same ledger, including concurrent attempts.

The one-shot Phase G E2E accepts the optional pair of operator variables:

- `GFLOW_CLI_E2E_PHASE_G_RETRY_PREDECESSOR`
- `GFLOW_CLI_E2E_PHASE_G_RETRY_EXPECTED_STATE`

Set both or neither. For the supported retry, the expected state is exactly
`UNEXPECTED_MULTI_OUTPUT`. A partial pair fails before the execution call; a
stale or mismatched pair fails against the locked ledger. The existing paid
E2E opt-in remains independently required. This contract does not itself
authorize a live or paid run.

## Preserved stops

- `D2_SUCCEEDED`, `WAF_REJECTED`, unresolved submission states, and artifact
  recovery states remain hard stops.
- Every D2 row retains the existing one-Generate, one-approval, and
  `output_count=1` controls.
- Prior accounted credits are never subtracted or reset. A prior 10-credit
  attempt remains counted; the new quote must fit within the remaining daily
  budget.
- The consumed retry authorization cannot be used to make another row. Any
  future exception requires a separately reviewed policy change.
- This policy changes no Flow, ledger history, n8n, D6, or BotTrade state by
  itself.

## Validation boundary

The implementation is tested offline against temporary ledgers. A live D2
requires the separate paid E2E opt-in and fresh pre-D2 gates; this contract
does not authorize Generate or approval.
