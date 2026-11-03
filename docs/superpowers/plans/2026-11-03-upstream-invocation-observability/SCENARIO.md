# Scenario: Upstream Invocation Observability Hardening V1

## Coverage map

- **D7 — Error propagation:** process launch, timeout, signal, and evidence-persistence failures must remain distinguishable and never trigger an automatic retry.
- **D8 — Cross-platform paths/processes:** UTC timestamps, POSIX signal interpretation, Windows return-code behavior, and JSON-safe ledger fields.
- **D12 — Observability:** bounded byte counts/hashes, parse diagnostics, redaction, durable event ordering, and correlation with the one canary invocation.
- **D6 — Data layer:** relevant only to the JSON credit ledger's atomic update and compatibility with rows written before this change; no SQLite migration.
- **D13 — MCP:** N/A. No CLI command, MCP tool, worker payload, or user-facing schema changes.

Other dimensions are out of scope: this does not change auth, reCAPTCHA, DOM selectors, browser automation, page pooling, batch resume, request inputs, Flow transport, or production routing. A live generation E2E is explicitly prohibited by the task's zero-provider requirement; the implementation is internal subprocess/ledger behavior and will be validated offline.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D7 / D12 | Valid JSON object, exit 0 | High | Preserve result success semantics and add process metrics without changing acceptance | Unit |
| 2 | D7 / D12 | Malformed JSON, valid JSON surrounded by text, array, or empty stdout | Critical | Keep strict `json.loads` object contract; retain bounded redacted parse diagnostics and return unknown post-submit | Unit |
| 3 | D7 / D12 | Stderr-only failure or nonzero return code | High | Record both streams and exit status; normalize conservatively, no retry | Unit |
| 4 | D7 / D12 | Timeout before any submit evidence vs after submit evidence | Critical | Preserve timeout and known evidence separately; classify pre-submit only when proven, otherwise hold | Unit |
| 5 | D8 / D12 | Process returns a POSIX signal code; Windows return code resembles a signal | High | Record raw return code everywhere; infer a signal only on POSIX; otherwise `UNKNOWN` | Unit |
| 6 | D12 | Output exceeds capture and diagnostic limits | High | Hash/count full streams while retaining only capped parse input and capped diagnostics; set truncation accurately | Unit |
| 7 | D12 | Bearer, cookie, signed URL, email, and long opaque token appear at excerpt boundaries | Critical | Redact before prefix/suffix clipping; omit a diagnostic if safe redaction cannot be assured | Unit |
| 8 | D12 | Submit/provider evidence arrives before malformed final output | Critical | Durably persist the allowlisted evidence event before parser diagnostics and normalized result | Integration (fake event sink) |
| 9 | D7 / D12 | Evidence persistence fails after process start | Critical | Terminate before request stdin is delivered when possible, leave durable invocation unresolved, never retry | Integration |
| 10 | D7 / D12 | Caller restarts after process-start or terminal-evidence checkpoint | Critical | Existing one-shot claim prevents a second upstream invocation; existing evidence remains readable | Integration |
| 11 | D12 | No Generate-click, submit, request, operation, or media evidence is exposed | High | Store `UNKNOWN`, never fabricate `false` or provider identifiers | Unit |
| 12 | D6 | Existing ledger row predates new evidence fields | High | Read and update compatibly without requiring a migration or changing old row semantics | Unit |
| 13 | D7 | Tests exercise all runner outcomes | Critical | Fakes only; no real upstream subprocess, Google Flow call, or Generate action in tests | Unit |

## Must-cover before merge (Critical + High)

1. Strict JSON result acceptance stays byte-for-byte equivalent in behavior.
2. Full-stream byte counts and SHA-256 are computed while capturing remains bounded.
3. Start, provider observation (when available), process termination, diagnostics, and normalized result persist in order.
4. Diagnostics are bounded and redacted before clipping; tests include secrets crossing prefix/suffix boundaries.
5. Timeout, caller termination, signal, and normal nonzero exit remain distinct.
6. Missing Generate/submit/provider evidence remains `UNKNOWN`.
7. A persistence or parser failure never retries or invokes a second provider subprocess.
8. Canary 02 files/state are never read for mutation and its reservation is not released.

## Deferred

1. Live Flow E2E of the upstream CLI result format is deferred because this task expressly forbids provider invocations and Generate actions.
2. Real Generate-click/request/operation instrumentation is deferred unless the pinned upstream CLI exposes those facts in a documented output/event contract.

## Suggested BDD scenarios

```gherkin
Feature: Upstream invocation observability
  Scenario: Persist bounded evidence before reporting malformed JSON
    Given a one-shot upstream adapter with a durable evidence sink
    And the process reports a submit observation before returning malformed output
    When the adapter normalizes the process result
    Then the submit observation is persisted before the parse diagnostic
    And the result remains post-submit unknown
    And the adapter is not invoked again

  Scenario: Redact diagnostic excerpts before storing them
    Given process output containing credential-like values at the excerpt boundaries
    When strict JSON parsing fails
    Then bounded redacted prefix and suffix diagnostics are persisted
    And the credential-like values are absent
```

## Known-issues cross-reference

No directly applicable `KNOWN_ISSUES.md` entry or active-plan decision was found. This change does not address Flow host routing, auth, WAF, or selector behavior.
