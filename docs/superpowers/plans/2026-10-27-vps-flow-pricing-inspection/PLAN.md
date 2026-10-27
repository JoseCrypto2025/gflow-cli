# VPS Flow Pricing Inspection Plan

## Predict gate

Five independent personas returned CAUTION, with no STOP. Proceed only with these conditions: operator-only entry point; exact profile/project/account pinning; fail-fast `ProfileLease`; no `FlowApiClient` lifecycle (incident-retention side effect); no `prepare_live_flow()` or mutation-capable settings/prompt helpers; no retries; passive structural DOM reads only; and explicit UNKNOWN/HOLD for unmeasured fields or absent quote. The existing account and project are already verified in the saved VPS runtime evidence, but this inspector must verify them again for its own run.

## Implementation tasks

- [x] Complete the source audit and document the existing profile loader, Flow navigation, read-only probes, locking, and screenshot capability.
- [x] Add a narrowly scoped read-only inspection service/adapter that reuses repository profile mapping, browser startup, exact project routing, and `ProfileLease`, holding one fail-fast lease for the full browser lifetime. It must not expose submission, prompt-edit, settings-write, or approval methods.
- [x] Implement allowlisted, locale-safe passive readers for the verified current account/project, current model/priority and visible composer fields. Keep unmeasured fields `UNKNOWN`; do not use menus or overlay dismissal unless the exact trigger is proven non-mutating and locale-safe.
- [x] Implement direct quote extraction only for a proven pre-submit UI element bound to current settings. Otherwise return `PRE_SUBMIT_CREDIT_COST_NOT_VISIBLE`; never infer from balances, account plan, history, model defaults, or settings.
- [x] Emit a compact sanitized JSON report and privacy-bounded evidence digests. Store a clipped screenshot only if it excludes prompt and unrelated account data; disable HAR, traces, incident recording/cleanup, and raw exception/page output.
- [x] Add deterministic tests for profile mapping and lock contention; account/project mismatch; model/config mismatch without correction; passive quote found/missing; sanitized output; screenshot/HAR/incident safeguards; and absence of submission/settings/prompt mutation capability.
- [ ] Add a real, read-only VPS E2E run for the exact `gflow-prod` identity after deployment. Run it once, bounded, with no WAF/reCAPTCHA retry. If UI cannot safely attest to identity/project or the quote, stop and report the precise HOLD/UNKNOWN reason.
- [ ] Update the applicable operations docs and run the repository's canonical `/gflow:check` gates, including the six mirror axes and relevant tests.
- [ ] Before commit, re-check `origin/automation-v2`, verify clean source and exact ancestry, and confirm no unrelated files or runtime data. Commit one coherent change and push only as a normal fast-forward to `automation-v2`.
- [ ] Deploy the exact pushed commit with the canonical V2 workflow: create rollback backup, confirm clean checkout and exact source commit, preserve unrelated runtime state, run runtime doctor and manifest checks, then verify local/origin/VPS/deployed head parity.
- [ ] Only after deployment parity, run the one-shot read-only inspector against `PVA_0002`, `gflow-prod`, and project `aafe9282-071f-4333-baa4-92b9f76a9c27`. Do not write pricing master data.

## Stop conditions

- Active profile lease, native Chromium lock, wrong account/project, stale or unexpected UI, WAF/reCAPTCHA refusal, ambiguous selectors, or any need to change settings or select a model.
- No verified passive quote surface. In that case return `PRE_SUBMIT_CREDIT_COST_NOT_VISIBLE` and keep pricing data unchanged.
- Any requirement to reuse a lifecycle that performs incident cleanup, expose page/session contents, or create/approve/submit provider work.

## Verification and docs

Behavior touches the Flow UI, so the Iron Law requires a real E2E run. The run is read-only and does not consume credits. Offline tests cannot establish the live UI shape. Documentation is updated in `docs/ai-video-automation-v2/operations/VPS_BROWSER_RUNTIME.md` after implementation. No canonical pricing master-data changes are in scope.
