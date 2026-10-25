# Ambiguous Legacy Recovery Policy V1

This policy applies only to a legacy PROD D2 attempt whose reconciliation is
`AMBIGUOUS_AFTER_RECONCILIATION` and whose evidence recovery has already ended
with `AMBIGUOUS_EVIDENCE_EXHAUSTED`. It does not repeat evidence recovery and
does not change the modern PROD or frozen TEST contracts.

## Safe disposition

The linked legacy Production Job and Scene Job move from `RUNNING` to terminal
`QUARANTINED`. The transition preserves every existing row field except
`Job_Status` and `Updated_At`. A quarantined job cannot resume, recover, append
an attempt, or reach the provider adapter. Ordinary `HOLD` behavior is
unchanged.

The exact existing 50-credit reservation remains unchanged in
`UNKNOWN_SUBMISSION_STATE`. Provider submission and billing remain `UNKNOWN`.
The policy reports 50 credits of unresolved held reservation exposure; provider
cost and any replacement reserve remain unknown, so combined exposure is
unquoted. It never marks submission failed or releases the reservation.

## Replacement safeguards

Quarantine may make a replacement eligible for a later, separately approved
decision. Eligibility is not authorization. This policy cannot create a
replacement attempt, approve it, or authorize Generate. Its deterministic
lineage key binds any later replacement decision to this predecessor. A future
replacement must have an explicit authorization event, and only one such
replacement may bind to a predecessor.

## Operator and durable audit

The internal `scripts/operations/apply_ambiguous_legacy_recovery_policy.py`
operator is scoped to the specifically authorized D2 run ID. It reads the
current exact D2 reservation and linked job rows, checks that the reservation
is still 50 / `UNKNOWN_SUBMISSION_STATE`, then appends a durable `POLICY_INTENT`
event before changing job rows. It quarantines Scene Job then Production Job,
reads both back, and appends `POLICY_APPLIED` only after both statuses are
verified. The audit is separate from reconciliation and credit ledgers,
append-only, hash-chained, and idempotent for exact event replay. Partial writes
remain replayable from fresh reads; mismatched state fails closed.

The evidence-recovery result in the audit is an operator-confirmed handoff
value. The operator does not search logs, provider systems, browser state, or
other evidence sources. Any new provider evidence or changed reservation
requires a separate assessment before policy application.

## Deployment / application sequence

Deploy and verify the exact reviewed source commit first. Apply the policy only
after source/deployed head parity and runtime doctor pass. Application changes
only the two linked job statuses and the separate policy audit; it does not
touch BotTrade, unrelated n8n, the D2 ledger, credit reservation, prompt, or
provider.
