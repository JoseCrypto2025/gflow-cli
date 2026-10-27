# Scenario: Flow pricing auth fallback under the inspector-owned lease

## Confirmed bug assessment

**Verdict: CONFIRMED-BUG.** The production verifier succeeds for the existing
`gflow-prod` profile and expected account when called without an outer lease. In
the inspector path, the outer `ProfileLease` is already held when cookie
decryption fails and the marker-gated Playwright fallback tries to acquire a
second lease for the same profile. The same-process lease guard rejects that
acquisition; `verify_flow_profile` turns it into `VERIFICATION_ERROR`, and
`GoogleFlowAgentOnlyPort.open` reports `FLOW_AUTH_NOT_READY`.

## Coverage map

| Dimension | Relevant | Why |
|---|---|---|
| D1 Auth & session lifecycle | Yes | Auth remains valid on Flow's migrated host; fallback must reuse the inspector's lease. |
| D10 Headed vs headed environment | Yes | The live inspector uses the production Chrome profile and headed Playwright under Xvfb. |
| D12 Observability & redaction | Yes | Reports and logs must not expose cookie values, session bodies, or unrelated page content. |
| D13 MCP parity | No | This is an internal VPS operation with no CLI option or MCP tool surface. |

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D1 Auth & session lifecycle | The inspector holds the exact profile lease; browser-cookie decryption fails; the marker-gated Playwright fallback runs with that already-held lease. | High | The fallback reads the existing profile, closes its context, and returns authenticated session evidence without attempting a nested lease. The inspector then verifies the exact account and project. | Unit regression + BDD-bound live E2E |
| 2 | D1 Auth & session lifecycle | A standalone verifier has no caller-owned lease and reaches the same cookie fallback. | High | The fallback acquires and releases its own lease, preserving existing callers. | Unit regression |
| 3 | D1 Auth & session lifecycle | A caller supplies an unacquired, released, or wrong-profile lease. | High | Reject before reading cookies or launching Playwright; never treat a foreign lease as ownership. | Unit regression |
| 4 | D12 Observability & redaction | The live inspector emits its sanitized report after successful fallback. | Critical | Exact account/project are confirmed; cookie values, tokens, and response bodies are absent; all provider mutation counters remain zero. | BDD-bound live E2E |

## BDD scenario

```gherkin
Scenario: Reuse the inspector-owned lease for authenticated cookie fallback
  Given the existing gflow-prod profile and exact production project
  When I run the read-only Flow pricing inspection
  Then the report verifies the expected Flow account and project
  And the report contains no provider mutation and no secret values
```

The existing `tests/e2e/test_flow_pricing_inspector_e2e.py` binds this scenario
to the real deployed inspector. It must be run against the existing production
profile after deployment; mocked browser tests alone do not satisfy this gate.
