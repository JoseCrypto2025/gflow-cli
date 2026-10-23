# D2 TEST-Session Pre-Approval No-Charge Reconciliation V1

**Contract ID:** `D2_TEST_SESSION_PREAPPROVAL_NO_CHARGE_V1`

This narrow contract applies only to one unresolved attempt inside the exact
authorized `D2_TEST_SESSION_BUDGET_V1` TEST session. It does not change the
frozen same-attempt recovery or forensic-closure contracts, and it does not
authorize another Generate or approval action.

## Balance-less confirmation gates

The confirmation gate must prove exactly one video and a known positive quote.
For an attempt bound to the TEST session, Phase G also requires the exact
in-flight D2 ID, a known captured TEST-session pre-attempt balance, a quote no
greater than that balance, and a quote no greater than the remaining 40-credit
session authorization. If the gate includes a visible balance, it must exactly
match the captured pre-attempt balance. If it omits that value, the ledger keeps
`observed_visible_balance=null`; Phase G does not fabricate a UI observation.
The non-TEST and ordinary daily-credit guards are unchanged.

## Eligible reconciliation

The only eligible transition is the exact active session's held
`UNKNOWN_SUBMISSION_STATE` attempt to terminal `RECONCILED_NO_CHARGE`, after a
single Generate dispatch and before any approval. The exact attempt must have:

- `generate_action_count=1`, `approval_action_count=0`, `output_count=1`, and a
  recorded Agent turn dispatch;
- one fresh, project-attributable observed confirmation with `video_count=1`
  and a known positive quote, with no approved `confirmation_id`;
- no provider execution ID, media ID, media ID list, local artifact, or remote
  artifact;
- a fully hydrated, complete, stable project snapshot whose execution/media
  identity sets match the persisted attempt baseline, whose confirmation set
  adds only the observed gate, and whose pending count is zero;
- a complete current chat enumeration with exactly one normalized exact
  provider-directive match at a bubble index at or after the persisted baseline
  chat count; historical bubble UI/hash drift is not activity evidence;
- no new finished media identity, no new Agent-associated media identity, no
  generation progress before the confirmation, no generation progress after
  it, and no provider/WAF refusal ambiguity;
- independent provider credit reads equal to the captured TEST-session
  pre-attempt balance, with no other account activity during the interval.

An unchanged balance alone is insufficient. Missing, stale, incomplete,
unstable, conflicting, or non-attributable evidence leaves the attempt UNKNOWN
and the session reservation held. No D2 ledger value may be changed through
`record_state()` to bypass this evidence contract.

## Evidence and operator

Finished tile identity is derived from the UUID in canonical
`flow-content.google/video/<uuid>` or `/image/<uuid>` paths, or from the
measured stable `/asb/<token>` poster token. Signed query parameters and URL
refreshes are excluded. Unknown tile routes produce no identity and leave the
snapshot insufficient; path similarity is not inferred. Agent turn matching
uses normalized visible message content and the exact persisted provider
directive SHA, only at indices beyond the baseline chat count. Historical
whole-bubble DOM hashes are context only.

The sanitized JSON evidence schema is
`D2_TEST_SESSION_PREAPPROVAL_NO_CHARGE_V1`. It binds the D2 ID, canonical ledger
SHA-256, implementation commit, runtime root, exact TEST profile/account/
project/session, current and baseline activity identities, action counts,
confirmation quote, baseline/current chat counts, exact directive SHA and
post-baseline matching bubble index, snapshot completeness, progress/refusal
findings, and before/after provider balances. It must contain no prompt, token, cookie,
credential, or raw provider response.

The operator is
`scripts/operations/reconcile_d2_test_session_preapproval_no_charge.py`. It
validates the evidence and exact current ledger snapshot by default. Applying
requires the same evidence digest, expected ledger digest, implementation
commit, and explicit `--apply`. The apply path re-reads and mutates under the
ledger lock, requiring the attempt still be UNKNOWN/nonterminal and the
session still be STOPPED with that exact run in flight.

On success, the operator records the evidence digest and reconciliation
history, sets both attempt states to `RECONCILED_NO_CHARGE`, records the zero
provider credit delta, clears the in-flight ID, restores the unspent session
reservation, and reopens the session only if authorized capacity remains. It
preserves the original accounted-credit field and provider/billing outcomes;
the observed zero session delta is not rewritten as a provider billing event.
The same run remains terminal and cannot be retried.

The ordinary `reconcile_d2_test_session.py` does not close UNKNOWN outcomes.
Do not use it, `record_state()`, manual JSON edits, forensic closure, or a new
D2 to bypass this contract.
