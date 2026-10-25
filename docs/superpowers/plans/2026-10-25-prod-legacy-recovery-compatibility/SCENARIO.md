# Scenario: Legacy PROD D2 Attempt Recovery Compatibility

## Scope

This is an offline compatibility layer for persisted PROD attempts created before
`D2_PROD_SAME_ATTEMPT_RECOVERY_V1`. It joins immutable copies of Sheets, D2 ledger, provider
catalog, and shared credit records. It does not open Flow, submit generation, approve, create an
attempt, or mutate the existing rows. The frozen TEST contract remains outside this path.

## Coverage map

- **Active:** identity joins, evidence provenance, production namespace, timestamp freshness,
  audit persistence, reservation ownership, and job-state interpretation.
- **Limited:** existing provider refusal classifier can be reused only when direct action time and
  exact positive provider evidence satisfy the current modern rule.
- **Skipped:** browser, transport, auth, and generation surfaces are unchanged and prohibited here.
- **MCP:** not applicable; this is an internal runtime API with no CLI or MCP surface.

## Severity-ranked scenarios

| # | Scenario | Severity | Expected behaviour |
|---|---|---|---|
| 1 | One unique D2 → Scene Job → Production Job → prompt → scene → profile → account/project → reservation join | Critical | Produce an identity envelope; mark every field direct, derived, diagnostic, or unavailable with source provenance |
| 2 | Any missing, duplicate, or conflicting canonical join | Critical | Return `LEGACY_IDENTITY_AMBIGUOUS`; reconciliation result remains ambiguous |
| 3 | PROD environment claims disagree across runtime, jobs, profile, account, or reservation | Critical | Do not classify or mutate state |
| 4 | `generate_action_at` is absent but `updated_at` and job timestamps exist | Critical | Keep action time `UNAVAILABLE`; never copy another timestamp into it |
| 5 | No provider submit, confirmation, execution, or media identity exists | Critical | Return `AMBIGUOUS_AFTER_RECONCILIATION`; keep reservation unchanged |
| 6 | Exact attempt-bound execution identity is present and excluded from the D2 baseline | High | Classify execution found without inventing a Generate timestamp |
| 7 | Submit/confirmation evidence lacks direct action ordering | Critical | Remain ambiguous; `PROVEN_NOT_SUBMITTED` remains unavailable |
| 8 | Production and Scene Job both say `RUNNING` with no execution proof | High | Report `LEGACY_PREMATURE_RUNNING`; do not write `HOLD` during assessment |
| 9 | Same envelope audit is written twice | High | Keep one idempotent event in a separate audit file; source ledgers remain byte-for-byte untouched |
| 10 | Modern evidence satisfies the existing positive refusal rule | Critical | Preserve the existing classifier result and exact release guard; do not broaden modern proof |
| 11 | Frozen TEST recovery runs with its current fixtures | Critical | Keep TEST V1 behavior unchanged |

## BDD scenarios

The offline Gherkin binding in `tests/features/prod_legacy_reconciliation.feature` covers unique
identity reconstruction, ambiguous missing provider evidence, and duplicate-link fail-closed
behavior. The larger deterministic matrix remains in `tests/validation/test_phase_g_prod_legacy_reconciliation.py`.
