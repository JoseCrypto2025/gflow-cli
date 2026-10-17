# Plan: Google Flow V2 presubmit foundation

1. [x] Audit baseline, Phase E handoff and existing Flow browser ownership.
2. [x] Record prediction and failure-scenario gates.
3. [x] Correct ProviderRouter to select by Provider_ID and check service type.
4. [x] Add typed approved-prompt handoff and default-off Flow presubmit state machine behind a submission-free facade.
5. [x] Add offline tests for provider coexistence, approval, kill switch, safety holds, exact readbacks, references, prompts, state paths and concurrency.
6. [x] Document adapter contract, failure forensics, runtime layout and future Phase G attribution boundary.
7. [x] Run Phase F, Phase E, V2, transport, schema, repo quality, and build checks.
8. [x] Perform read-only VPS audit; no VPS changes.
9. [ ] Commit/push only if code foundation and offline verification pass; live status is independent.
