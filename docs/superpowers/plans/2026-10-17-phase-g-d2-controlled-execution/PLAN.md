# Phase G D2 controlled execution plan

**Goal:** Safely execute at most one approved local Flow T2V attempt, attribute its exact asset, validate and persist it, then stop when the D2 contract is proven.

**Architecture:** Keep Phase F/F5 submission-free. A separate Phase G controller owns the one-action ledger and settings recovery transaction; a concrete Flow adapter composes the existing `GoogleFlowAgentOnlyPort`, migrated batchexecute parser, and exact-media recovery helper. The production Drive provider remains fail-closed; D2 persistence uses the explicitly authorized local Drive UI fallback if needed.

**Predict verdict:** CAUTION — 8/10.

## Tasks

1. [x] Establish request pinning, attempt ledger, settings recovery transaction, artifact validator, and offline tests.
2. [x] Implement concrete local Flow adapter by composing the isolated repository Flow port and one browser lifecycle.
3. [x] Add focused coverage for quote/count/cost, exact provider attribution, WAF, ambiguous timeout, settings restoration, and persistence verification.
4. [x] Add bounded local D2 orchestration from fresh Phase E through F5, one Phase G attempt, exact download, and full media validation.
5. [ ] Run repository-required hygiene, docs, Ruff, format, Pyright, schema, build, and full offline tests; repair new-code failures.
6. [ ] Inspect diff/secrets and commit/push `automation-v2`; verify local/remote SHA equality and clean tree.
7. [ ] Re-read current approved Sheet rows, TEST project state, Drive target, and recovery journal; perform at most one gated local D2 T2V attempt and stop on success, WAF, or uncertainty.

## Definition of done
- F5 has no submit/generate API and its kill switch remains false.
- Exactly one Generate action and one confirmation maximum per D2 attempt.
- Confirmation explicitly proves one video and a known cost within budget.
- Exact new provider execution/media identity belongs to the TEST project.
- Downloaded media passes full decode, duration/aspect, and SHA-256 checks.
- Drive copy is re-downloadable and hash-identical before local cleanup.
- Original settings are restored and read back; recovery state is clean.
- No production Sheet rows, n8n/VPS/Relay/BotTrade changes, personal Chrome use, or extra credits after success.
