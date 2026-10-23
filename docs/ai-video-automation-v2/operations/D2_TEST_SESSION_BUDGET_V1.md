# D2 TEST Session Budget V1

**Contract ID:** `D2_TEST_SESSION_BUDGET_V1`

This contract implements the explicit CEO authorization for up to 40 **new**
Flow credits during one same-day debugging session. It is a narrow exception to
the ordinary 50-credit Bangkok-day Phase G ceiling for the exact TEST lane
below. The ordinary TEST path and all PROD paths retain the existing daily
ceiling. This contract does not alter the frozen same-attempt recovery or
forensic-closure contracts.

## Exact authorization scope

- Environment: `TEST` only.
- Browser profile: `gflow-v2-test` / `GFLOW_V2_TEST_AUTH`.
- Provider account: `PVA_0001`.
- Provider project: `34e60953-e534-46e9-ac65-993893e61065`.
- Runtime root: `D:\Automation Content\gflow-v2-d2-state`.
- New-session cap: 40 credits total, regardless of Flow credit refreshes.
- Maximum attempts: 40 distinct D2 IDs, one in flight at a time.
- Expiry: end of the current Bangkok accounting date.
- Evidence: digest of the CEO authorization source, implementation commit,
  and SHA-256 of the existing ledger before opening the session.

The open-session operator accepts only the explicit authorization evidence,
current implementation commit, exact runtime root, starting live balance, and
expected ledger SHA. It does not accept a caller-selected cap or attempt limit.
The authorization is consumed once in the lock-protected ledger. The session
digest binds its immutable authorization fields. A local JSON ledger is an
operational guardrail, not tamper-resistant accounting.

## Accounting and enforcement

The existing 50-credit daily budget remains 50. The schema migration adds a
session record without removing, resetting, re-dating, or rewriting historical
attempt rows or reservations. The exact authorized session may exceed the
ordinary same-day total only through these session-aware Phase G methods. A
request without the matching session ID uses the unchanged daily budget path.

The session reserves its full remaining authorization before a D2 row is
created and records the live balance before that attempt. Phase G rechecks the
session and exact live adapter profile immediately before Generate. Before a
one-time approval it requires one video, a known positive quote, a quote no
greater than the remaining session cap, and a captured pre-attempt balance
known to be sufficient for the quote. If Flow exposes a visible balance in the
gate, it must equal the captured pre-attempt balance; if the gate omits it, the
recorded UI value remains null and the captured live pre-attempt balance is the
budget evidence. The TEST-session lane must not be used for any other paid
action during that attempt; see the versioned pre-approval reconciliation
contract below before clearing an UNKNOWN outcome.

After the attempt, the operator reads the supported live credits command once
and reconciles the before/after values. The session records the quoted cost and
the observed balance delta separately. It only opens the next attempt when the
prior D2 is terminal, has no media/artifact requiring recovery, and the delta
is either zero or exactly the known quote. The summed positive balance delta
must remain at or below 40.

The 40-credit authorization caps new session spend. The preserved historical
50-credit unknown reservation may still represent an unknown earlier provider
charge; this session does not claim or change that historical billing outcome.
The session's cap is not a statement that total historical-plus-new possible
exposure is 40.

## Mandatory stops

- Any unknown/nonterminal provider or billing outcome stops the session and
  retains its full remaining reservation for same-attempt reconciliation.
- A changed or inconsistent provider balance, an unquoted debit, or a delta
  different from both zero and the known one-video quote stops the session.
- WAF refusal and a verified `D2_SUCCEEDED` stop the session permanently.
- Any provider media, local artifact, remote artifact, or artifact-recovery
  state blocks a new D2; recover that same D2/media.
- A missing/dirty settings recovery journal, changed identity/root, expired,
  reused, malformed, or concurrent authorization blocks before Generate.
- Each D2 keeps its one-Generate, one-approval, and `output_count=1` limits.
- No automatic retry, second click, PROD run, D6, n8n, or BotTrade action is
  authorized by this contract.

The balance delta may be confounded by another account consumer. Do not use the
TEST account for another paid action during a session attempt. Any unexplained
balance movement is a stop, not a basis for estimating spend.

## Operators

The narrow absent-visible-balance approval rule and exact evidence-backed
pre-approval reconciliation are specified in
[`D2_TEST_SESSION_PREAPPROVAL_NO_CHARGE_V1.md`](D2_TEST_SESSION_PREAPPROVAL_NO_CHARGE_V1.md).
That operator is the only supported way to transition an UNKNOWN TEST-session
attempt to `RECONCILED_NO_CHARGE`; `record_state()` and the ordinary session
reconciler remain fail-closed.

Open once with `scripts/operations/open_d2_test_session.py` after the code is
committed/pushed, the tracked worktree is clean, the current ledger SHA is
verified, and the fresh Phase E/F5 gates pass. Supply the CEO authorization
evidence file, implementation commit, and actual credits-before value. Pass the
returned session ID and that live balance to the one-shot Phase G E2E.

After every attempt, read credits again and call
`scripts/operations/reconcile_d2_test_session.py` with that D2 ID and both
observed balances. Do not start another D2 until the operator reports `ACTIVE`.
If it reports `STOPPED`, `SUCCEEDED`, or `CREDIT_CAP_EXHAUSTED`, stop paid
testing. Reconcile a stopped in-flight attempt only through the exact same-run
evidence path; never clear the session hold manually.
