# Predict: Google Flow V2 presubmit foundation

## Verdict: CAUTION
**Confidence:** 8/10

## Summary
Separate provider identity from service type, then add a Flow preparation contract with no submit capability. Keep browser work behind an injected repository-owned facade until selector and session contracts are verified; mocks do not prove live Flow compatibility.

## Persona findings

### Architect — GO (8/10)
Provider_ID is the stable adapter identity; ServiceType remains a compatibility constraint. The Flow presubmit belongs under `v2_runtime` and should depend on a narrow port rather than importing Flow transport internals into business logic.

### Security / reCAPTCHA — CAUTION (8/10)
No auth secrets or credentials should enter requests or diagnostics. Dedicated project identity, persisted approval, default-off submission guard, and one profile lease are mandatory. Never print profile/session paths or state.

### Performance / Playwright — CAUTION (7/10)
Preparation is low volume and should be serialized per account/project. Reuse existing browser ownership and profile leases; do not create a second Playwright lifecycle or attach an external CDP browser.

### CLI UX / Cross-platform — GO (8/10)
This is a V2 runtime contract with no CLI or MCP surface. Stable error codes and POSIX runtime roots are needed; browser diagnostics belong under TEMP_ROOT and must redact secrets.

### Devil's Advocate — CAUTION (9/10)
The simplest safe first slice is a typed request, checker, and non-submitting port. A live implementation should wait for dedicated project/account data and verified current UI surface; no production generation path is justified in this phase.

## High-confidence risks
1. UI and data drift can make a live prep attempt mutate the wrong project; block before mutation unless identity is proven.
2. Existing transport owns a generation-capable path; the new port must not expose or call it.
3. Offline mocks can prove our state machine only, not live Flow behavior.

## Conflicts resolved
- Reuse the existing browser ecosystem while isolating provider behavior behind a V2 port; do not instantiate the generation transport from the presubmit checker.

## Required mitigations before EXECUTE
1. Keep kill switch default false and prove submit is unreachable even for a ready state.
2. Require persisted APPROVED prompt row and exact prompt/hash agreement.
3. Treat unknown session/project/surface/settings/reference/prompt state as a deterministic HOLD.

## Recommended next step
Implement and test the offline foundation; keep live browser readiness optional and separately classified.
