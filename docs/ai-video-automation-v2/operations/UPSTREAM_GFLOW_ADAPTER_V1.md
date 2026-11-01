# Upstream gflow-cli adapter V1

## Status and scope

This is an **offline adapter experiment**, not a production generation engine. It is not wired to
`ProductionOrchestrator`, `run_production_scene.py`, n8n, Google Sheets, D2, or D6. The existing local
Phase G production path remains the only production engine. No generation-engine switch exists in the
current V2 runtime settings, so V1 adds no switch and does not change the default.

The adapter lives at `src/gflow_cli/v2_runtime/upstream_gflow_adapter.py`. It is deliberately not a
`SceneGenerationPort`: `GeneratedSceneAsset` requires exact provider account, execution, media, and D2
identities, while upstream's current JSON result does not provide those fields. The adapter returns
`UPSTREAM_GENERATION_COMPLETED` only to mean that the child CLI exited successfully and produced the
requested local MP4. It does not claim project readback, account verification, D2 success, media
validation, Drive persistence, or production finalization.

## Pinned upstream contract

V1 is pinned to release `gflow-cli==0.92.1`, tag source commit
`63dd0081e117dc081ed7fc774d3dd19ac91ec9aa` (annotated tag `v0.92.1`). The adapter requires an
absolute Python interpreter and checks `python -m gflow_cli --version` returns exactly `0.92.1` before
building the generation request. The release tag/commit is a source pin; the version probe alone does
not prove the installed wheel's source hash. A future canary must install a separately verified
artifact built from this pin.

The pinned `gflow video t2v` surface includes:

| Input | Upstream contract |
|---|---|
| Prompt | Required positional argument |
| Profile | `--profile <name>` |
| Existing project | `--project <UUID>`; project creation occurs only when no ID is supplied |
| Model | `--model omni-flash\|veo-lite\|veo-fast\|veo-quality\|veo-lite-lp` |
| Aspect | `--aspect 9:16\|16:9` |
| Duration | `--duration 4\|6\|8\|10`; 10 seconds is Omni Flash only; cohort UI controls can refuse pre-submit |
| Output count | `--count 1..4`; V1 forces `1` |
| Output file | `--output <file>` (alias `-o`); V1 always supplies an explicit staging MP4 path |
| Machine-readable result | `--json` |
| Timeout | No CLI timeout flag. The transport has an internal 600-second poll timeout; V1 applies a 900-second outer process deadline by default |
| Transport selection | No stable explicit transport selector for migrated Flow. Routing follows the host/composer observed by upstream; `--ui-mode` is not used by V1 |

The CLI version command is checked first. Generation then runs the upstream Click entry point through
the pinned interpreter with the same Click argument vector as `gflow video t2v`, supplied to a fixed
bootstrap over stdin. The prompt is therefore not part of the OS process command line. The child
receives only platform essentials and the explicit `GFLOW_CLI_HOME`; there is no shell string or
interpolated command.

### Upstream success JSON

V1 validates the exact shape and values that matter from upstream `video_result()`:

```json
{
  "status": "ok",
  "command": "video t2v",
  "media_id": "<Flow media UUID>",
  "generation_status": "MEDIA_GENERATION_STATUS_SUCCESSFUL",
  "succeeded": true,
  "local_path": "<staging MP4 path>",
  "request": {
    "model": "veo_3_1_lite",
    "mode": "t2v",
    "aspect": "portrait",
    "duration": 8,
    "count": 1,
    "seed": null
  }
}
```

The real result also contains `failure_reasons` and `error_message`; V1 does not return raw detail.
Upstream error JSON has `status: fail` and an `error` object containing RFC 9457 fields plus `class`,
`exit_code`, and `retryable`. V1 uses the allowlisted class/exit fields, not arbitrary detail or
stderr. Common upstream codes include auth expired `3`, timeout `9`, WAF `10`, selector drift `23`,
agent UI `25`, unsupported migrated feature `36`, and insufficient credits `37`.

Not present in the upstream success JSON: provider account, project readback, Flow operation/workflow
ID, D2 ID, authorization/reservation evidence, and submit-click stage. The adapter leaves
`upstream_operation_id`, `d2_run_id`, and `drive_file_id` empty and labels project binding as
`REQUEST_ARGUMENT_ONLY`. It must never fabricate these values.

## Normalized result: `UPSTREAM_GFLOW_ADAPTER_RESULT_V1`

`AdapterResult.to_dict()` is the stable internal boundary. It contains status and exit code; optional
upstream operation/media IDs; requested project plus binding provenance; profile, Production Job,
Scene Job, and caller idempotency IDs; local output path; stdout/stderr SHA-256 digests; elapsed time;
normalized error code/type; whether submission may have occurred; `auto_retry`; safe message and
remediation; and empty Drive/D2 fields. It excludes the approved prompt, account email, raw stdout,
raw stderr, cookies, provider bodies, and tokens. n8n must consume a later orchestration contract,
not upstream JSON or this adapter directly.

### Status mapping

| Result status | Mapping | Safe caller behavior |
|---|---|---|
| `UPSTREAM_GENERATION_COMPLETED` | Exact success JSON, matching config, media UUID, and existing MP4 inside staging | Continue existing validator/storage steps; no finalization implied |
| `PRE_SUBMIT_FAILURE` | Authorization/input/version check or process launch proven before child dispatch | Correct cause and reevaluate the same durable job; adapter does not retry |
| `AUTH_FAILURE` | Known upstream auth/account errors | Operator verifies exact account/profile before reevaluation |
| `PROJECT_NAVIGATION_FAILURE` | Upstream UI selector/project error; submission stage is not proven | Inspect/reconcile exact job and project; no retry |
| `UNSUPPORTED_FLOW_STATE` | Known unsupported host/account state | Stop for operator review |
| `INSUFFICIENT_CREDITS` | Known credit refusal | Recheck canonical quote/guard; no retry |
| `POST_SUBMIT_UNKNOWN` | Timeout, killed child, truncated/malformed output, unknown error, config mismatch, or uncertain stage | Reconcile the exact existing attempt; never invoke Generate again |
| `DOWNLOAD_FAILURE_AFTER_GENERATION` | Upstream media download error after media generation | Recover/download the same existing asset; never Generate again |

`auto_retry` is always `false`. The adapter's in-memory key check is only defense in depth. Durable
one-invocation-per-canary enforcement belongs to the caller's shared credit ledger, bound to its
Production Job, Scene Job, request key, and invocation ID.
Because upstream CLI does not expose a durable stage marker, V1 treats generic post-launch failures as
unknown even if upstream labels an error retryable.

## Responsibilities retained locally

The external production caller must still perform, before any future child process launch:

- canonical Master Data/prompt approval and prompt SHA binding;
- exact profile ID/name and account identity verification;
- exact existing Flow project authorization and project readback policy;
- CEO authorization, current canonical quote, ceiling, daily limit, and reservation;
- durable Production Job/Scene Job/idempotency claim and one-shot checkpoint;
- no-blind-retry and same-attempt reconciliation on unknown outcome.

After a local MP4 is returned, downstream code retains:

```text
adapter result
→ phase_g_artifacts.validate_video / canonical artifact validator
→ full media decode + duration/aspect check + SHA-256
→ Google Drive storage adapter
→ remote re-download and SHA-256 equality
→ production output finalization
```

The adapter does not call Flow media validation, upload to Drive, write Sheets, or change D2/D6.

## Future integration point (not activated)

`ProductionOrchestrator.run_scene_once()` currently uses the Phase G request factory and
`PhaseGSceneGenerationAdapter` for PROD. Upstream JSON cannot safely populate `GeneratedSceneAsset`;
future integration needs a separately reviewed provider identity/submit evidence contract before
this topology can change:

```text
Production Job claimed
→ approved prompt + Phase E validation
→ exact profile/account/project + canonical credit authorization
→ durable one-shot/unknown-submit checkpoint
→ UpstreamGflowVideoAdapter.generate_once()
→ normalized local result (not D2 success)
→ existing artifact validation
→ Drive persistence + remote SHA verification
→ finalize only after required lineage/evidence is supplied
```

No runtime feature flag was added. The default remains local Phase G. Ordinary episode generation may
later avoid requiring full D2 certification, but must retain job identity, credit authorization,
one-shot invocation, unknown-submit protection, and a same-attempt reconciliation hook. D6 remains
outside ordinary generation.

## Future canary and rollback

The canary is a separate, explicitly authorized task. Before it runs, pin and verify the installed
artifact/source hash; verify that no parent process holds the same `ProfileLease`; verify expected
profile/account/project and the quote/credit ceiling; use one fresh job with one output; and ensure the
caller persists its durable attempt/checkpoint before child dispatch. If the child times out, is
killed, or returns unreadable output, keep the job ambiguous and reconcile the same project/attempt.
Do not make a second invocation.

After one run, verify exact project context, observed submit/provider identity, one attributable media
ID, media validity, local/remote artifact integrity, and Drive finalization. Only a separately reviewed
parity result can authorize routing any production job through the adapter. Rollback means switching
the **future caller** back to the already-supported local Phase G implementation; it cannot undo a
Flow generation or credit charge already accepted by the provider.

## Durable canary caller V1 (implemented, offline only)

`v2_runtime.upstream_canary.UpstreamCanaryProductionCaller` is an injectable bridge for a later,
separately authorized canary. It is not connected to a CLI/MCP entrypoint, `ProductionOrchestrator`,
n8n, or the default PROD route. The ordinary Phase G orchestrator explicitly rejects its
`upstream-canary-v1:` idempotency-key namespace, so a canary job cannot be resumed by the normal
Generate path.

Before preparing the durable checkpoint, the caller requires a fresh reader result with an approved
prompt, exact prompt hash, locked Scene, `GPR_0002`, the verified `singleava508@gmail.com` account,
the exact PROD project UUID, a released parent profile lease, and a quote read within five minutes.
The reader is the integration port to canonical Master Data/account/quote sources; the caller
rechecks these values and will stop if any binding or authorization differs. The quote must be
positive, equal to the validated per-run cap, within the CEO ceiling, current free provider quota,
and the shared 50-credit daily ceiling. The ledger reserves exactly the quote.

The shared locked credit ledger stores the invocation ID, Production/Scene Job IDs, request key,
prompt/profile/account/project/config bindings, source pins, output path, quote, reservation and
`READY_FOR_UPSTREAM_INVOCATION` checkpoint before child launch. It atomically changes to
`UPSTREAM_INVOCATION_STARTED` before calling the synchronous adapter. A process restart with that
checkpoint or any later state never calls the adapter again. A proven pre-submit refusal releases
the quote and becomes terminal. An uncertain result retains the reservation in HOLD. A generated
but undownloaded result can use the dedicated `download_existing(media_id, destination)` port; it
has no generation method.

On adapter success, the caller runs the existing full MP4 validator and `persist_and_verify` path,
including a remote re-download and SHA-256 comparison. It records a canary artifact result while
leaving the ordinary Production Job and Scene Job incomplete. The upstream result still lacks
provider execution/account/project readback and submit-stage evidence, so no `GeneratedSceneAsset`,
provider execution ID, or normal job completion is synthesized.

The caller implementation and its tests were exercised offline with fakes only. Before any future
live canary, verify that the installed adapter and upstream artifact match their pinned source
commits, use the approved one-shot authorization, and follow the separate live-verification gate.
