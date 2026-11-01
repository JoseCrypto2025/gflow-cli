# Scenario: Upstream-first video adapter V1

## Coverage map

- **D1 Auth/session:** profile and expected account are required inputs; adapter does not perform or claim identity verification.
- **D4 Idempotency/retry:** reject unauthorized and duplicate invocation keys; no retry after process dispatch or ambiguous outcomes.
- **D5 Concurrency/process lifecycle:** concurrent same-key calls and timeout/termination must not dispatch a second command.
- **D7 Error propagation:** map the pinned CLI JSON envelope and conservative fallbacks to a stable local result.
- **D8 Cross-platform paths:** validate absolute executable and staging/output containment on Windows and POSIX.
- **D9 Transport:** N/A for adapter-owned code; Flow transport stays inside pinned upstream CLI.
- **D10 Headed/headless:** no browser is started by tests; upstream invocation is replaced by a fake command runner.
- **D11 Input validation:** validate profile, project, model, aspect, duration, output count, staging path, and job identities before launch.
- **D12 Observability/redaction:** emit digests and allowlisted metadata only; never include prompt or raw streams in results/errors.
- **D13 MCP parity:** N/A; no CLI command or MCP tool is added. The caller contract remains private and is documented locally.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D4/D11 | Generation is not authorized | Critical | Reject before version check or generation runner invocation | Unit |
| 2 | D4/D5 | Same caller idempotency key is submitted twice to one adapter | Critical | Second call is rejected; no second generation command | Unit |
| 3 | D7/D4 | Child times out or is killed after process launch | Critical | Return `POST_SUBMIT_UNKNOWN`; `AUTO_RETRY=false` | Integration |
| 4 | D7/D4 | Non-zero exit without a parseable error envelope | Critical | Conservatively return `POST_SUBMIT_UNKNOWN`; never retry | Integration |
| 5 | D7/D4 | Upstream reports a post-submit non-retryable timeout | Critical | Map to `POST_SUBMIT_UNKNOWN`; preserve upstream class and safe remediation | Integration |
| 6 | D7 | Upstream reports download failure after a media ID exists | High | Map to `DOWNLOAD_FAILURE_AFTER_GENERATION`; do not Generate again | Integration |
| 7 | D7 | Auth error is returned in upstream JSON | High | Map to `AUTH_FAILURE`; operator correction required before reevaluation | Integration |
| 8 | D7 | Project navigation error is returned | High | Map to `PROJECT_NAVIGATION_FAILURE`; no retry of the unchanged job | Integration |
| 9 | D7/D11 | Unsupported host/UI state or insufficient credits | High | Map to typed stable status; no automatic retry | Integration |
| 10 | D8/D11 | Missing project, non-absolute executable, path escape, symlink, or pre-existing output | Critical | Reject before generation runner invocation | Unit |
| 11 | D11 | Prompt contains quotes, newlines, Unicode, and shell metacharacters | High | Preserve exact prompt as one argv element; `shell=False` | Integration |
| 12 | D7/D11 | Upstream JSON is malformed, duplicated, wrong version, or does not match requested settings | High | Return safe normalized failure; no retry when submit state is uncertain | Integration |
| 13 | D12 | Prompt/token sentinel appears in stderr/stdout/error detail | Critical | Only hashes and allowlisted error fields are returned | Unit |
| 14 | D10 | Offline fake CLI runs success, missing output, process error, and stderr-only modes | High | Deterministic result; no real Flow browser/provider access | Integration |

## Must-cover before merge (Critical + High)

1. Unauthorized requests never start any subprocess.
2. A duplicate key never dispatches twice within an adapter instance; durable cross-process idempotency remains the caller's Production Job responsibility.
3. Timeout, termination, malformed output, and unknown post-spawn exits are non-retryable unknown outcomes.
4. Exact project/profile/model/aspect/duration/count/output arguments are present and prompt is one unmodified argument.
5. Paths cannot escape the staging root or overwrite an existing asset.
6. No prompt, raw stdout/stderr, account secret, or provider body is returned or logged.
7. Success requires pinned version, one strict success JSON object, expected request readback, a media ID, and an MP4 path that exists within staging.
8. The normalized result must explicitly say that project/account/execution/D2 IDs are not upstream readbacks and cannot finalize a production job.

## Deferred (Medium + Low — log as issues, not blockers)

1. Live verification against a real Flow profile and production project is prohibited in this offline task; it is a separate canary gate.
2. POSIX/Windows process-tree termination and real upstream timeout behavior need a dedicated subprocess smoke test before a production canary.
3. Upstream currently accepts the prompt as a positional argument, so it is visible to local process-inspection tools while the child runs. Do not pass credentials or secret material in prompts.

## Suggested BDD scenarios

```gherkin
Feature: Offline upstream video adapter
  Scenario: Unauthorized request does not invoke the upstream CLI
    Given a request without generation authorization
    When the adapter is asked to generate once
    Then it returns PRE_SUBMIT_FAILURE
    And the fake upstream runner has no calls

  Scenario: Child timeout after dispatch is ambiguous and cannot be retried
    Given an authorized request with a stable idempotency key
    And the fake upstream runner times out after process launch
    When the adapter is asked to generate once
    Then it returns POST_SUBMIT_UNKNOWN
    And AUTO_RETRY is false

  Scenario: Successful output is normalized without claiming production finalization
    Given a successful pinned CLI JSON result and an MP4 in staging
    When the adapter is asked to generate once
    Then it returns UPSTREAM_GENERATION_COMPLETED
    And the result does not claim Drive persistence or D2 finalization
```

These scenarios are adapter-code behavior and are bound to offline tests (no e2e tag). The live generation path remains explicitly unverified because this task prohibits Flow/provider actions.

## Known-issues cross-reference

- The upstream lost-submit behavior and recovery are documented in its migrated composer implementation and the `2026-10-27-lost-video-submit-recovery` spike. This adapter preserves the conservative no-resubmit rule but does not replace same-attempt reconciliation.
- No current local `KNOWN_ISSUES.md` item is resolved by this offline adapter; production integration remains gated on a separate canary.
