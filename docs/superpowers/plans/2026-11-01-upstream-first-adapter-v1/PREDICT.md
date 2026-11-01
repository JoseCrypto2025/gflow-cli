# Predict: Upstream-first video adapter V1

## Verdict: CAUTION

**Confidence:** 8/10

## Summary

The adapter is suitable as an offline, disconnected experiment, using the existing V2 job orchestration policy as its future caller. The pinned upstream CLI performs generation and downloads media, but its JSON result lacks exact account, project, execution, and D2 identities required by `GeneratedSceneAsset`; this adapter must not implement `SceneGenerationPort` or be presented as production-ready. A timeout, killed child, malformed result, or unknown post-spawn error must become a non-retryable unknown outcome.

## Persona findings

### Architect — CAUTION (8/10)

Reuse the V2 package boundary, but keep a distinct upstream result type. `ProductionOrchestrator` and `SceneGenerationPort` require lineage evidence that upstream `video_result()` does not emit. Do not synthesize execution/account/project/D2 IDs or relax the production Phase G requirement merely to inject the new adapter.

### Security / reCAPTCHA — CAUTION (6/10)

Pin the absolute interpreter and upstream package version, use an argv array with `shell=False`, pass the prompt over stdin to a fixed launcher rather than expose it in process argv, and allowlist normalized errors. Require exact profile/project/account expectation and the caller's authorization before dispatch. Unknown child termination is not a safe retry signal.

### Performance / process lifecycle — CAUTION (8/10)

A CLI process adds cold-start overhead and can own the upstream profile lease. The parent must not hold that same lease while the child runs. Bound the overall timeout above upstream's internal polling window, drain output with capped buffers, terminate and reap the process tree on timeout, and treat the attempt as unknown afterward.

### CLI UX / Cross-platform — CAUTION (8/10)

No CLI or MCP capability is being added, so surface symmetry is not applicable. Keep a stable local JSON contract, no raw stderr/traceback/prompt, strict Windows/POSIX path handling, and explicitly report that upstream does not return project/account/execution/D2 proof. Do not invent an engine-switch environment setting.

### Devil's Advocate — CAUTION (9/10)

The existing Phase G production adapter already owns the exact authorization, reservation, submit evidence, and recovery boundary. The upstream-first wrapper is a strategic migration path, but not a drop-in production replacement. The offline adapter should remain disconnected until a separately authorized canary proves the missing identity and submit-evidence contract.

## High-confidence risks (flagged by 2+ personas)

1. **Missing production lineage evidence:** upstream JSON omits provider account, project, execution and D2 identities; Architect and Devil's Advocate flagged that this prevents direct production-port compatibility.
2. **Ambiguous process outcome:** a child timeout/termination can occur after submit; Security, Performance, Architect, and Devil's Advocate require no retry and same-attempt recovery.
3. **Profile ownership:** the child must be the sole holder of its upstream profile while the command runs; parent/child lease overlap can block or corrupt use.
4. **Schema/security boundary:** prompt and raw error streams must not leak; pass prompt through stdin and return digests plus allowlisted fields only.

## Conflicts resolved

- **Reuse `SceneGenerationPort` vs separate adapter result:** the existing port is the correct future orchestration seam, but its success type requires exact identities missing from upstream JSON. Resolution: implement a separate adapter/result and do not inject it into production yet.
- **Prompt as argv vs process privacy:** upstream CLI's public positional prompt interface exposes the value in the child command line. Resolution: invoke the pinned CLI entry point through a fixed Python bootstrap and pass its argument vector as stdin JSON, keeping the prompt out of OS process argv.
- **Use a feature switch vs avoid config framework:** no established generation-engine switch exists. Resolution: add none; production remains on local Phase G.

## Required mitigations before EXECUTE

1. Hard-pin interpreter path, upstream version `0.92.1`, tag commit `63dd0081e117dc081ed7fc774d3dd19ac91ec9aa`, command flags, and JSON fields.
2. Keep adapter detached from `ProductionOrchestrator`, Phase G, n8n, D2 and D6 state transitions.
3. No automatic retry for any post-spawn ambiguity; duplicate caller key must not invoke twice in one adapter instance.
4. Bound and validate staging output and return only local MP4 completion, not production finalization.
5. Keep upstream live Flow verification as a separate canary gate; no Flow/provider action is allowed in this task.

## Recommended next step

Implement a standalone offline adapter and fake-runner tests on the production-head feature branch; keep current Phase G as the only production engine until a separate canary proves identity, submit and recovery parity.
