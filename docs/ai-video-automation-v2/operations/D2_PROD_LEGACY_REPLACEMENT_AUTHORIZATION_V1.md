# D2 PROD Legacy Replacement Quote and Authorization V1

**Status:** frozen historical contract. Its exact-dimension quote matcher and
post-Generate reservation describe V1 only. New model-aware quotes and
pre-Generate replacement reservations follow
[`D2_PROD_LEGACY_REPLACEMENT_AUTHORIZATION_V2.md`](D2_PROD_LEGACY_REPLACEMENT_AUTHORIZATION_V2.md).

This contract is separate from legacy reconciliation, the ambiguous recovery
policy, modern PROD recovery, and frozen TEST retry behavior. It is an internal
runtime capability; it adds no CLI, MCP, queue, scheduler, or Flow surface.

## Pre-attempt quote

`quote_generation_reservation()` accepts only the exact approved live Google
Sheets snapshot produced by the schema-validated snapshot source. The quote
timestamp must equal that snapshot's read timestamp. It then resolves the
`PROVIDER_COST_MASTER` row and exact active PROD `GENERATION_PROFILE_MASTER`
scope. Its input digest binds provider, provider account, model, generation
profile, mode, duration, aspect ratio, resolution, audio mode, output count,
and quality tier. A valid result requires exactly one active, effective,
source-verified direct `CREDIT` rate with all those exact dimensions. Monetary
rates, incomplete dimensions, ambiguous rows, future or expired rates, and
profile mismatches return `QUOTE_UNAVAILABLE`. No history-based or default
amount is used.

The current Phase G runtime does not use `PROVIDER_COST_MASTER` to price a
generation. Its `quoted_credit_cost` and `accounted_credit_cost` come from the
observed Flow confirmation after the attempt has been created and Generate has
already occurred. That path cannot quote a future replacement before attempt
creation. The canonical cost table has no applicable source-backed rate for
this exact profile, so both conditions are material: a direct row is missing,
and a distinct preflight quote must not depend on the post-Generate
confirmation path.

The appended cost-scope fields in `PROVIDER_COST_MASTER` are optional for
existing records and must be populated to quote this exact configuration. The
current repository has no source-backed applicable cost row. Therefore the
current production quote remains `QUOTE_UNAVAILABLE`; a replacement decision
must remain `HOLD`. Do not add a rate without an authoritative provider source.
The historical 50-credit reservation describes only the unresolved parent and
is never used as a quote for a future generation.

Quote resolution creates no D2 attempt, credit reservation, Generate action,
approval, or provider call.

## Decision and exposure

The pure decision gate returns `APPROVE` only if both existing evidence gates
pass, the quote is valid, existing unresolved exposure is known, combined
exposure is calculated, authorization and parent linkage are ready, the exact
parent remains quarantined, no replacement or conflicting authorization
exists, and standard evidence capture is ready. The maximum combined exposure
is unresolved parent exposure plus the validated replacement quote. Unknown
quote or exposure yields no numeric combined value and holds the decision.

## One-time authorization and lineage

The D2 ledger schema 3 stores a single legacy replacement authorization for an
exact parent. Schema 1 and 2 readers migrate in memory to schema 3 without
rewriting historical attempts or TEST sessions. The authorization binds the
parent D2 ID, Production Job, Scene Job, exact ambiguous recovery policy,
reconciliation audit digest, decision digest, exposure, quote/source/version,
combined exposure, implementation commit, creator, and timestamp. Its
`generate_authorized` value is always `false`.

Only an explicitly invoked controlled task may consume an approved
authorization. Consumption revalidates the exact quote and parent state under
the D2 ledger lock, then atomically appends one new `PREPARED` attempt with
explicit authorization and parent lineage and marks the authorization
`CONSUMED`. An exact replay returns that same linked attempt. A different
attempt ID, duplicate authorization, changed quote, changed parent, or second
replacement fails closed. Consumption itself records zero Generate and
approval actions; the authorization never permits either action.

Ordinary attempt creation, retry, recovery, scheduler, and orchestrator paths
cannot consume or infer this authorization. `QUARANTINED` is terminal in the
Production Job and Scene Job state machines. The explicit future replacement
task remains separate from implementation and deployment.

## Live-state boundary

Implementation and validation do not create a live authorization or attempt,
reserve or release credits, update the legacy parent, call Flow, Generate, or
Approve. The legacy parent remains quarantined with its 50-credit
`UNKNOWN_SUBMISSION_STATE` reservation and `UNKNOWN` billing until a distinct
authorized task changes it.
