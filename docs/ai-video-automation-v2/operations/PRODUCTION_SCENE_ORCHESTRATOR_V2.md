# Production Scene Orchestrator V2

`scripts/operations/run_production_scene.py` is the one-scene production
operator for a locked, approved **NO_AUDIO** Scene. It composes the current
live Sheets snapshot, Phase E, Phase F5, the Sheets-backed Production/Scene
Job stores, the bound Phase G adapter, verified Drive finalization, and local
cleanup through `ProductionOrchestrator`.

The operator requires explicit scene, profile, account, project, and expected
Google email arguments. It reads the current Flow credit balance through the
repository's read-only `gflow credits` service using the `gflow-prod` profile;
caller-supplied balances are not accepted. It also requires the production
runtime environment and an externally provisioned Google Workspace credential.
The account, project, profile, current approved prompt, Scene audio intent, and
one-output setting are checked against fresh live Master data. Phase F5 runs
before a production job is claimed. After F5, the operator reads the full
snapshot again and holds if any canonical Sheet data changed.

Without `--execute`, the command performs the live Sheets read, Phase E checks,
and a read-only Flow balance inspection through the `gflow-prod` profile, then
prints `READY_FOR_F5_AND_EXECUTION`; it does not run F5 or write transaction
rows. The deployed script imports the SHA-verified application source tree
via `PYTHONPATH=/opt/gflow-v2/src:/opt/gflow-v2`, so the runtime does not
require an editable project installation. With `--execute`, it runs
non-submitting F5 and then the canonical one-shot lifecycle. The Phase G
request is bound to the claimed Scene Job's deterministic D2 ID. Re-entry on
an unresolved Scene Job uses the same Phase G request for recovery;
caller-supplied media is not accepted.
Existing production or Scene Jobs for the same prompt prevent a second claim.
Jobs remain `QUEUED` until the Phase G adapter returns validated provider
execution and media identities. An exception before that proof moves both jobs
to `HOLD` with `PROVIDER_RECONCILIATION_REQUIRED`; replay cannot Generate from
HOLD and may only use the exact-media Phase G recovery path. PROD evidence
reconciliation is governed by the separate
[`D2_PROD_SAME_ATTEMPT_RECOVERY_V1.md`](D2_PROD_SAME_ATTEMPT_RECOVERY_V1.md)
contract and never approves or resubmits generation.
Historical PROD attempts missing that contract's persisted job linkage use the separate,
read-only [`D2_PROD_LEGACY_ATTEMPT_RECOVERY_V1.md`](D2_PROD_LEGACY_ATTEMPT_RECOVERY_V1.md)
envelope. It joins exact canonical identities, records per-field provenance in a separate audit,
and leaves source rows untouched. Missing provider evidence remains ambiguous and reserved; a
`RUNNING` row without exact execution proof is reported as needing HOLD, not changed during
assessment. Use `scripts/operations/reconcile_legacy_prod_attempt.py --d2-run-id <exact-id>` for
the supported legacy classification path. It loads fresh canonical sources and does not release a
reservation; `--persist-audit` is an explicit separate-audit option.

Example invocation after the operator has independently verified current
identity:

```powershell
python scripts/operations/run_production_scene.py `
  --scene-id SCN_000001 `
  --profile-id GPR_0002 `
  --provider-account-id PVA_0002 `
  --provider-project-id aafe9282-071f-4333-baa4-92b9f76a9c27 `
  --expected-email singleava508@gmail.com `
  --execute
```

The balance is read before F5 and refreshed after F5, before the attempt is
claimed. A missing, unauthenticated, or mismatched profile balance holds the
operator. The Phase G quote is still subject to the shared daily credit guard.
The operator supports only
the explicit `Dialogue_Mode=NONE` branch. It creates no TTS row and does not
infer speech from visual prompt text. The speech-enabled TTS/Merge components
remain library composition until an approved voice-resolution path is
available; do not use this operator for speech-enabled scenes.

The deployed archive allowlists this operator explicitly. It does not invoke
n8n or the separate audio-enhancement endpoint; V2 merge runs locally through
the validated FFmpeg pipeline.
