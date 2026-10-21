# D2 Forensic Closure V2

This contract adds a separate, versioned closure path for an unknown D2 attempt
whose only observed post-baseline activity is project chat. The V1 contract and
its absolute-zero-chat rule remain unchanged. V2 classifies chat as
`CHAT_ONLY_UNRESOLVED_ACTIVITY`; it does not claim the chat is unrelated,
harmless, a failed generation, a successful generation, or evidence of no
charge.

## Required evidence

The sanitized canonical JSON evidence package is stored outside Git at
`<runtime-state-root>/phase-g/forensic-evidence-v2/<D2-run-id>.json`. It records
the run and exact TEST account/project identity, ledger SHA-256, observation
time, all non-chat project counts and identifiers, the persisted attempt
baseline, and a complete ordered snapshot of current chat bubble SHA-256
values. It also records whether each bubble exposes a stable ID or timestamp,
media UUID and run/execution ID findings, and exact or substring prompt-match
flags. Raw prompt and chat text are not stored. Hashes fingerprint the
observation; they do not establish attribution.

Every chat present at submission baseline must have an ordered text-hash list
captured and persisted with that baseline. V2 fails closed if a nonzero
baseline's hashes are missing. When the baseline marker is `0:`, the baseline
chat count and ordered baseline hashes are both empty.

V2 requires exact TEST lane, identity, and project verification; hydrated,
complete chat enumeration; zero current project media, execution, pending,
confirmation, and provider identifier counts; absent Phase G result, historical
temp output, provider execution and media IDs; a clean recovery journal; exactly
one Generate action; at most one approval; an unknown, nonterminal attempt;
unknown provider, credit, and billing outcomes; and `SAFE_TO_RETRY = NO`.

## Dry-run and apply

Use `scripts/operations/close_unknown_d2_forensic_v2.py`. It is dry-run by
default. The operator must supply the runtime state root, exact D2 run ID,
expected current state, expected ledger SHA-256, expected evidence SHA-256,
implementation commit, and evidence path. Applying additionally requires the
explicit `--apply` flag and performs the transition under the ledger lock after
rechecking its SHA-256.

V2 records `forensic_closure_contract_version=D2_FORENSIC_CLOSURE_V2` and
`forensic_classification=CHAT_ONLY_UNRESOLVED_ACTIVITY`, preserving the
existing accounted credit cost and setting provider, credit, and provider
billing outcomes to `UNKNOWN`. The attempt becomes
`FORENSICALLY_CLOSED_UNKNOWN`. Its run ID remains permanently non-retryable;
closure itself does not authorize another paid generation. A distinct new D2
attempt must pass the normal fresh Phase E, F5, identity, budget, and
authorization gates. A forensic closure never qualifies for D6; only a separate
D2 that reaches `D2_SUCCEEDED` with verified artifact evidence can release D6.

V2 is an operator-controlled contract. Do not run `--apply` without a separate
explicit authorization after review of the exact evidence and dry-run result.
