# D2 Forensic Provider-Silent Closure V3

**Contract ID:** `D2_FORENSIC_PROVIDER_SILENT_CLOSURE_V3`

V3 is a separate, TEST-project-only evidence contract for the exact provider-silent
unknown attempt `D2_8d892c38cc8f4c99`. It does not change the frozen V1 absolute-zero-chat
rule or V2 baseline-relative chat-hash rule. Chat count, text, ordering, and hashes are
secondary context only; they cannot prove provider success or failure.

## Evidence boundary

The canonical sanitized evidence JSON is supplied to
`scripts/operations/close_provider_silent_d2_forensic_v3.py`. It binds the exact run,
implementation commit, canonical ledger SHA, TEST account/project, request identity,
accounting date/cost, durable baseline, clean recovery journal, local/remote artifact
absence, and a fresh live project observation. It stores no raw chat text, cookies, tokens,
signed URLs, or raw batchexecute response bodies.

Live evidence requires at least three fully hydrated structured-provider snapshots spanning
60–120 seconds. Every snapshot must show zero pending work and zero confirmations. Structured
execution and media identities must be enumerated and match the durable historical/baseline
sets; the observed project and finished-media DOM IDs must also match the historical media
set. The structured record identity set must remain stable across snapshots; any new or
changing candidate blocks closure. Chat identifiers without a matching structured provider
record are ambiguous and block closure. Chat hashes need not preserve a baseline prefix and
never establish attribution.

The attempt must still have one Generate action, zero approval actions, no provider IDs,
unknown provider/credit/billing outcomes, no local attempt artifact, no remote upload or
verified artifact, and exactly the preserved `2026-10-23` / `50` credit accounting. A dirty
or unreadable journal, changed ledger, malformed evidence, mismatch, or any incomplete live
snapshot fails closed.

## Preview and apply

The operator is dry-run by default. It requires the exact current state, ledger SHA, canonical
evidence SHA, implementation commit, run ID, and evidence file. `--apply` is an explicit
terminal ledger mutation. The apply method rechecks the ledger SHA and attempt predicates
under the ledger lock before writing; it preserves the reservation, baseline, request
identity, and all unknown outcomes. It records
`forensic_closure_contract_version=D2_FORENSIC_PROVIDER_SILENT_CLOSURE_V3` and
`forensic_classification=NO_PROVIDER_EVIDENCE`.

Closure permanently disables retry of this run ID and does not itself authorize a distinct
paid D2. The existing daily accounting remains unchanged: the 50-credit reservation remains
counted for its Bangkok accounting date, so a same-day paid Generate remains blocked. V3
does not establish that the provider refused, did not charge, or generated successfully, and
it does not qualify the attempt for D6.
