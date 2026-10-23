# D2 Forensic Misattribution Closure V2

V2 supersedes the frozen V1 evidence contract. It closes a `GENERATION_SUCCEEDED`
retry only when persisted provider identities are proven to belong to an earlier
same-project D2 attempt and complete structured read-only enumeration found no
new retry execution or media. It invalidates the retry row's attribution; it
does not prove the retry generated nothing.

## Evidence contract

The canonical compact JSON evidence package contains the exact schema version,
run ID, pre-closure ledger SHA-256, implementation commit, TEST account and
project IDs, current submission and terminal state, action counts, historical
retry authorization and predecessor, recorded provider execution/media IDs,
historical identity matches, complete structured enumeration, zero new
candidate identities, no post-baseline media association, no remote upload or
verification, unknown provider/credit/billing outcomes, recovery journal state,
completion timestamp, and a sanitized reason. Raw prompts, cookies, tokens,
signed URLs, and response bodies are prohibited.

`CURRENT_TERMINAL_STATE` is the literal persisted value represented safely:
`NONE` for a null ledger value, or `GENERATION_SUCCEEDED` for the legacy marker.
The operator accepts exactly those two values and binds the evidence to the
current ledger row. `_TERMINAL_STATES` is unchanged. Every actual terminal state,
including `D2_SUCCEEDED`, remains ineligible for this closure.

## Preconditions and use

Use `scripts/operations/close_misattributed_d2_forensic.py`. It is a dry run by
default and takes the same versioned operator arguments as the other Phase G
forensic operators. Applying requires `--apply` after review. The operator
requires the exact canonical ledger SHA and evidence digest, exactly one run
row, `GENERATION_SUCCEEDED` with a null or exact `GENERATION_SUCCEEDED`
terminal marker, one Generate action, at most one approval, the exact TEST
account/project, the consumed one-time authorization naming the exact
predecessor, historical same-project matches for every recorded provider ID,
zero new candidate IDs, no verified remote artifact, unknown provider/credit
outcomes (including the defined absence-as-unknown behavior), `UNKNOWN`
billing outcome, and a CLEAN recovery journal. Each condition is rechecked
under the ledger lock before mutation.

The transition sets both states to `FORENSICALLY_CLOSED_UNKNOWN`, keeps
provider, credit, and billing outcomes `UNKNOWN`, and records
`attribution_invalidated = true`, `artifact_attribution_valid = false`, the
evidence digest, implementation commit, and sanitized evidence. It preserves
the original Generate/approval counts, accounting date, accounted credit
reservation, provider IDs, and any local artifact path/hash/size. It never
clears credits, deletes evidence, claims no charge, or makes the attempt
eligible for D6. `record_state()` cannot create this state.

## Future paid-attempt policy

After this exact one-time retry chain has been closed for historical
misattribution, only its exact original `UNEXPECTED_MULTI_OUTPUT` predecessor
is considered consumed when a later distinct D2 row is created. The closed
retry must retain its valid authorization, matching predecessor, V2 closure
contract, classification, invalidation flags, and digest-valid evidence.
Unrelated paid terminal attempts, WAF refusal, D2 success, unresolved work,
`GENERATION_SUCCEEDED`, and `PERSISTENCE_FAILED` remain blocking conditions.
This exception does not authorize another retry; after a new D2 spends its
Generate action, that attempt becomes the next controlling paid row.

Before a Generate click can be recorded, the locked ledger also rejects a
Bangkok-day accounted total at or above the fixed 50-credit budget. Earlier
accounting dates remain historical and are evaluated separately on the new
Bangkok day.
