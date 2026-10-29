# D2 PROD Legacy Replacement Authorization V2

**Status:** active for newly approved legacy replacements. V1 remains frozen for reading old
records and does not acquire V2 semantics.

This contract permits one separately priced replacement of a quarantined legacy attempt whose
provider submission remains ambiguous. It does not resolve, rewrite, or release the legacy attempt
or its reservation. The old 50-credit exposure stays `UNKNOWN_SUBMISSION_STATE` until authoritative
provider evidence changes it.

## Pricing identity

A direct credit quote is usable only when one current live `PROVIDER_COST_MASTER` row matches all
of the following:

- provider, provider account, model, generation mode, and account pricing class;
- active account, model, and PROD generation profile identities;
- `Cost_Unit=CREDIT`, active status, effective timestamps, and official Google Flow source;
- the configured supported pricing variant.

For standard `Veo 3.1 - Lite`, an account verified as `NON_ULTRA` uses the standard-rate row. The
`Lower Priority` variant is separate and is not a substitute. A per-generation rate is multiplied
by output count. Resolution, audio mode, quality tier, duration, and aspect ratio are not quote keys
unless the current official source explicitly distinguishes them. These dimensions remain part of
the execution request and must still pass Phase G readback.

No missing, ambiguous, stale, future, inactive, wrong-identity, or non-official row may be replaced
with a historical charge, a default price, or an inferred value. The canonical live table retains
its existing 18 columns. Non-applicable `Quality_Tier` and `Resolution` cells remain blank.

## Authorization and child jobs

The operator computes one stable request idempotency key from the exact authorization identity and
uses it for one new Production Job and one new Scene Job. Replays with that key must resolve to the
same request digest and same job IDs. A conflicting request, duplicate row, unrelated active job,
or ambiguous write is a hold.

One V2 authorization binds:

- the exact parent D2 attempt, parent Production Job, and parent Scene Job;
- the replacement Production Job and replacement Scene Job;
- the applied recovery policy and legacy reconciliation audit;
- the decision digest, exact quote amount/unit/source version/input digest, and combined exposure;
- the replacement D2 attempt ID after consumption.

The authorization alone never enables Generate. Consumption atomically creates the one zero-action
replacement D2 row and permanently links it to the authorization, parent attempt, quote, and child
jobs. Exact replay may return only that same row after digest and lineage verification. It may not
create a new authorization, job pair, or attempt.
The supported operator also requires fresh parent reconciliation to remain ambiguous and the
policy/reconciliation link to match the consumed authorization, both on replay and again against
the refreshed pre-consumption snapshot. A changed parent result or link blocks Phase G.

## Reservation and Phase G

Before any provider action, Phase G must revalidate the consumed authorization and exact child
lineage, then create a replacement-only shared-credit reservation for the exact quote. The
reservation is `PRE_GENERATE_RESERVED`, has zero Generate clicks, and is bounded by the 50-credit
daily ceiling and the fresh provider free balance. Existing unresolved same-day provider attempts
block reservation. Normal production attempts cannot use this transition.

Only the matching authorization may advance that reservation through one Generate click. Flow's
confirmation must identify one video and the exact pre-reserved amount before Phase G records one
approval and clicks confirmation. Any amount, count, identity, or balance mismatch stops before
approval.

The Flow settings snapshot may include display-only model metadata. The recovery journal persists
and restores only the semantic settings that Phase G changes, and restoration readback compares
those same fields.

Before a click, a failure may release only this new replacement quote after the shared ledger proves
zero clicks. After a click, all failures remain on the same attempt and preserve the quote reservation
for reconciliation. Ambiguity never proves non-submission, never authorizes a retry, and never
releases either reservation.

### Pre-click diagnosis

`scripts/operations/run_legacy_replacement.py --diagnose-preclick` is a read-only diagnostic lane
for one already-consumed authorization and its existing `PREPARED` replacement attempt. It requires
the authorization, attempt, Production Job, and Scene Job IDs explicitly, then rechecks the fresh
quote, parent quarantine and reservation, child-job linkage, zero-action attempt, and consumed
authorization before opening Flow.

The lane verifies the signed-in profile, project, and current settings snapshot. It stops before
`journal.begin`, settings application, prompt filling, submission baseline capture, attempt creation,
shared-credit reservation, Generate checkpoint, or provider submission. Flow navigation and opening
the settings view are read-only and the settings view is closed after snapshotting. The lane cannot
create or consume authorization, attempt, or job records. It writes append-only, redacted stage and
failure events to `production/phase-g/execution-diagnostics/<attempt-id>.jsonl`; it never stores
cookies, tokens, OAuth credentials, browser storage, authentication headers, or prompt text. A
failure retains the public reason code `PHASE_G_EXECUTION_FAILED_BEFORE_CLICK` and includes the
failing stage, last completed stage, exception class and sanitized message, source location, and a
digest of the raw traceback.

The operator stops after exact local media validation. It does not upload, persist `FINAL_OUTPUT`,
or enter D7/D8.

The operator reports Google's published 14:00–17:00 UTC non-subscriber availability warning as
an advisory; Flow decides whether a request can proceed. A correlated `YhhmEf` submit response
with parsed reasons records sanitized refusal facts. A reason code is retained when exactly one
safe token exists, with `PUBLIC_ERROR_UNUSUAL_ACTIVITY` retained as the WAF code whenever that
exact reason appears. Response bodies and arbitrary reason text are never stored. This does not
prove non-submission or authorize a retry: the exact same attempt is reconciled, and unresolved
exposure remains reserved. Only the complete correlated WAF HTTP 403 proof contract establishes
non-submission.
