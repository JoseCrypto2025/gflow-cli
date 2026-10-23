# Flow Credits Migrated Reader Implementation Plan

> **For agentic workers:** Run `/gflow:status --feature flow-credits-migrated-reader` to find the next unchecked task. Run `/gflow:check` before every commit.

**Goal:** Read the authenticated Flow balance from the migrated app's own `nzlxg` response while preserving the existing CLI and MCP result contracts.

**Architecture:** Keep the existing `CreditsInfo`, CLI, and MCP boundaries. `FlowApiClient.get_credits()` observes the app's own batchexecute reply through the existing migrated transport, with page check-in in `finally`; the shared service no longer calls the retired labs HTTP route. The credits service disables raw HAR capture before browser launch and bounds navigation plus response observation to one deadline.

**Predict verdict:** CAUTION — confidence 8/10. Security and performance concerns are mitigated by refusing configured raw HAR capture, keeping typed body-free errors, and bounding the full read duration.

**Risk register:**
| Severity | Risk | Mitigation |
|---|---|---|
| High | Flow changes or omits the undocumented `nzlxg` reply | Strict decoder; typed app/wire failure; live read-only verification |
| Critical | Authenticated response data is written to an opt-in HAR | Opt the credits client out of HAR recording before browser launch; test its context kwargs |
| High | Unbounded navigation plus reply wait delays users | Enforce a total deadline and preserve listener/page cleanup |
| High | CLI/MCP result fields drift | Preserve `CreditsInfo` fields and test CLI/MCP schema parity |

---

## File structure

### New files

```text
docs/superpowers/plans/2026-10-23-flow-credits-migrated-reader/SCENARIO.md
  Edge-case and BDD coverage map
docs/superpowers/plans/2026-10-23-flow-credits-migrated-reader/PLAN.md
  Implementation checklist
```

### Modified files

```text
src/gflow_cli/api/dto.py
  Add strict nzlxg balance decoding while preserving the DTO's public fields
src/gflow_cli/api/transports/migrated_composer.py
  Add passive, bounded, body-safe Flow balance observation
src/gflow_cli/api/client.py
  Route get_credits through the browser reader and block raw HAR capture
src/gflow_cli/services/credits.py
  Remove the retired labs HTTP fast path and preserve refresh behaviour
tests/api, tests/services, tests/features, tests/e2e
  Decoder, lifecycle, privacy, compatibility, and read-only BDD coverage
docs/USAGE.md, docs/MCP.md, docs/AUTHENTICATION.md, KNOWN_ISSUES.md and generated website mirrors
  Document browser-based balance reads and resolve the stale #795 status
```

## Task 1 — Offline test scaffold

**What:** Add failing tests for the decoder, passive RPC observation, errors, page lifecycle, HAR guard, and service behavior.

**Steps:**
- [x] Cover valid/zero balances and malformed, negative, boolean, string, and missing values.
- [x] Cover unrelated RPCs, unreadable bodies, explicit refusal, timeout, and body-free errors.
- [x] Cover page check-in and raw HAR refusal before launch.
- [x] Cover service no longer using `fetch_credits_http` and existing refresh semantics.
- [x] Preserve CLI/MCP JSON fields, including null tier fields.

## Task 2 — BDD and live binding

**What:** Bind the zero-credit read-only Flow scenario to the E2E suite.

**Steps:**
- [x] Add the Gherkin feature and exactly one `tests/e2e/` binding.
- [ ] Run against the authenticated TEST profile and confirm integer balance with no generation action.

## Task 3 — DTO and transport implementation

**What:** Parse only `[0]` from the observed `nzlxg` frame and map missing/refused/malformed responses to typed failures.

**Steps:**
- [x] Add strict decoder with no assumptions about undocumented fields.
- [x] Observe normal app traffic without replaying the RPC.
- [x] Apply a single total deadline across navigation and response wait.
- [x] Remove listeners in `finally`; never log or persist the body.

## Task 4 — Client and service wiring

**What:** Replace the old credits route throughout the canonical path.

**Steps:**
- [x] Use the current page pool and check pages in on every path.
- [x] Disable raw HAR capture before launch, even when the global HAR setting is configured.
- [x] Remove the retired HTTP fast path and its dead imports/route if no other caller remains.
- [x] Keep `--refresh` limited to existing expired-session renewal.

## Task 5 — Docs and API surface review

**What:** Keep CLI/MCP instructions accurate and update generated mirrors.

**Steps:**
- [x] Document that credit reads launch the selected profile browser.
- [x] Update CLI/MCP behavior, `KNOWN_ISSUES.md`, and generated website docs.
- [x] Verify existing CLI and MCP result schemas remain unchanged.

## Task 6 — Validation and live read

**What:** Verify offline behavior, then run the authorized zero-credit live checks.

**Steps:**
- [x] Run focused credit/API/service, Phase G, Agent-only, forensic V1/V2/V3, and retry-policy suites.
- [x] Run Ruff, format, Pyright, documentation hygiene, generated-doc check, and secret review.
- [ ] Run the live TEST balance read and zero-credit intercepted Agent-only canary.
- [ ] Stop before paid D2 if any live read/canary invariant fails.

## Task 7 — Commit and push

**What:** Commit one focused change and secure it on `automation-v2`.

**Steps:**
- [x] Run the full canonical `/gflow:check` gate.
- [ ] Commit with `fix(v2): read Flow credits from migrated app`.
- [ ] Push and verify local HEAD equals `origin/automation-v2`.

## Definition of done

- [ ] Decoder, transport, service, CLI, and MCP tests pass.
- [ ] Live TEST balance read and intercepted zero-credit canary pass.
- [ ] Canonical check is green and generated docs are current.
- [ ] Focused commit is pushed and remote HEAD matches.
- [ ] No D2 is attempted until the explicit fresh snapshot, Phase E, F5, project, journal, and budget gates pass.
