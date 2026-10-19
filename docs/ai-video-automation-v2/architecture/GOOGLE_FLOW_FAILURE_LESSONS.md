# Google Flow Failure Lessons (Phase F)

This note distinguishes checked-in evidence from historical symptom reports supplied in the Phase F request. It does not claim access to old VPS logs or previous working copies.

## Findings

| Failure or observation | Classification | Evidence and limit | Preserved invariant |
|---|---|---|---|
| Existing UI automation transport contains generation-capable methods | PROVEN | `src/gflow_cli/api/transports/ui_automation_video.py` and `ui_automation.py` contain generation paths; Phase F does not invoke them | Keep the preparation port free of submission methods |
| Phase E's prompt candidate is `VALIDATED`, not approved | PROVEN | `build_scene_prompt_payload` in `src/gflow_cli/v2_runtime/phase_e.py` builds only an in-memory candidate | Persist and approve through the guarded operator lifecycle before provider preparation |
| Profile leases serialize access to browser profile state | PROVEN | `src/gflow_cli/profile_lease.py` documents and implements process and kernel locking | One account/project browser lane; no fixed-sleep lock |
| Reference identity/selection has had binding failures | PROVEN (historical snapshot) | `D:\Automation Content\VPS_AUDIT_2026-10-03.md` records six failed jobs and an existing reference-binding investigation; checked-in Flow code also warns that prompt chips and submitted reference slots can differ (#723/#723-era work) | Require exact provider asset identity and slot, never list order or visual similarity |
| `NO_APPROVAL_RENDERED`, prompt acknowledgement without confirmed generation, and stale approval controls | PROVEN (historical snapshot) | The 2026-10-03 audit says five of six failed jobs acknowledged the prompt but showed no qualifying approval/queued/generating state or new asset within 30 seconds; approval-labelled controls were present. It does not establish their cause. | Snapshot before future submit; stale controls and UI activity are not submission evidence |
| `generation_started=true` without confirmed start | PROVEN (historical snapshot) | The same audit says several failed records had this flag without meeting the requested `GENERATION_STARTED` condition | A flag alone is not submission proof; require current-attempt provider evidence |
| `PRE_SUBMISSION_VALIDATION_FAILED` and `PROMPT_CONTRACT_MISMATCH` | OBSERVED | User supplied historical labels; no old implementation or incident artifacts were copied into this repository | Check persisted approval, exact prompt text, and hashes before any UI preparation |
| `download.saveAs` ENOENT | PROVEN (historical snapshot) | The 2026-10-03 audit records one new asset followed by `download.saveAs` ENOENT. It explicitly does not determine whether the source disappeared, the destination directory was absent, or another lifecycle issue occurred. | Future output handling must use a job-scoped temp path and validate before cleanup |
| Selector drift, session failures, and stop-control confusion | OBSERVED | The user supplied these symptom labels; the audit called for investigation of stale-control filtering but did not prove a selector or session root cause | Unknown surfaces and non-ready auth states hold; never infer submission from a stop control |
| Legacy duration normalization can hide a mismatch | OBSERVED | The request identifies a prior 30→8 behavior; no Phase F code uses this normalization | Require exact duration readback; never normalize in the adapter |
| Stale controls or a `generation_started` flag might be mistaken for a new attempt | INFERRED | Safety conclusion from the supplied symptoms; no historical trace was available | Future proof must attribute new provider evidence to the current attempt |
| Pre-existing assets might be mistaken for the result of a new run | INFERRED | Safety conclusion from the supplied symptoms; no incident capture was available | Snapshot existing operation/render/asset identities before any future submission |

## Implementation guard

The historical audit is dated 2026-10-03 and is evidence only of the state then. Phase F tests exercise a fake submission-free port; they do not establish current Flow selector compatibility or live authentication. No attempt is made to restore code from old working directories or VPS deployments.
