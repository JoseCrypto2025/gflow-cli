# Scenario: TTS and Merge Pipeline V2

## Coverage map

Active dimensions: D1 (provider credential resolution), D6 (transaction
idempotency), D7 (stable error handling), D8 (Windows/Linux workspace paths),
D9 (provider and media protocol errors), D11 (empty/oversize text and duration
limits), and D12 (redacted structured events). D13 is explicitly not applicable
to this internal runtime-only feature: no CLI or MCP command/option is added.
The remaining dimensions concern Flow browser generation or selector behavior;
this pipeline runs after Flow and does not alter that surface.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D1 Auth | Missing or invalid TTS API key | Critical | Reject before provider work; never log key/header | Integration |
| 2 | D1 Auth | Active key is absent from runtime environment | High | Hold before claiming a billable TTS attempt | Unit |
| 3 | D6 Idempotency | Same production job, dialogue line, attempt submitted twice | Critical | Return the existing job/result; do not call provider twice | Unit |
| 4 | D6 Idempotency | Provider accepted a request but acknowledgement was lost | Critical | Reconcile by stable provider job ID/readback; unresolved state is HOLD | Integration |
| 5 | D7 Errors | Provider returns non-JSON, malformed, failed, or incomplete result | High | Stable typed error; no COMPLETED state | Unit |
| 6 | D8 Paths | Workspace contains spaces/Unicode or is outside configured root | High | Normalize valid paths; reject traversal and out-of-root artifacts | Unit |
| 7 | D9 Media | Zero-byte, truncated, corrupt, or undecodable audio | Critical | Reject before merge; preserve diagnostics and attempt evidence | Unit |
| 8 | D9 Media | AAC mux cannot stream-copy source video | High | Use bounded H.264 fallback and record strategy | Integration |
| 9 | D11 Duration | Audio is shorter than video | High | Preserve the video timeline and pad audio deterministically | Integration |
| 10 | D11 Duration | Audio exceeds video by any amount or far exceeds it | Critical | Raise `TTS_DURATION_MISMATCH`; never truncate speech or freeze video | Unit |
| 11 | D11 Duration | NO_AUDIO scene has no dialogue rows | Critical | Finalize validated video without an empty TTS job or fake audio stream | Unit |
| 12 | D12 Observability | Provider/FFmpeg error contains secrets or source text | High | Emit stable codes and redacted metadata only | Unit |
| 13 | Media regression | Historical `-shortest` behavior truncates audio/video | Critical | Regression fixture proves complete timeline and decode | Integration |
| 14 | Media regression | Historical distorted audio | Critical | Input and final AAC independently decode; measured duration/sample properties match policy | Integration |
| 15 | Deployment | Provider service is unavailable or unauthenticated | Critical | Hold before TTS work; no fallback to a second provider automatically | Integration |

## Must-cover before merge (Critical + High)

1. Missing/invalid credential rejection before provider activity.
2. Idempotent TTS claim and uncertain-ack reconciliation.
3. Full audio probe/decode checks for zero, corrupt, and truncated media.
4. Timeline-authoritative duration rules, including rejection of longer audio.
5. NO_AUDIO branch creates no fake TTS job and preserves the complete video.
6. Merge copy and H.264 fallback paths both produce fully decodable MP4/AAC.
7. Regression checks for `-shortest` truncation and distorted audio.
8. Temporary-file cleanup only after verified durable output; failures retain evidence.

## Deferred (Medium + Low — log as issues, not blockers)

1. Music, ambience, SFX, ducking, J-cuts, and L-cuts; no current approved scene requires them.
2. Multiple TTS providers or automatic provider failover.
3. Multi-speaker dialogue scheduling beyond ordered independent line attempts.
4. Quality scoring beyond objective codec, decode, duration, clipping, and silence checks.

## Suggested BDD scenarios

```gherkin
Feature: TTS and merge production finalization
  Scenario: An existing successful TTS attempt is reused
    Given a completed TTS job for the same production job, line, and attempt
    When the coordinator receives the same request again
    Then it returns the persisted result without calling the provider

  Scenario: Audio longer than the authoritative video timeline is rejected
    Given a fully decodable video and audio whose duration exceeds the video
    When the merge coordinator validates the inputs
    Then it raises TTS_DURATION_MISMATCH without truncating audio or video

  Scenario: An explicitly silent scene takes the NO_AUDIO branch
    Given an approved scene with Dialogue_Mode NONE and no dialogue rows
    When the finalization coordinator receives a validated video
    Then it creates no TTS job and records a validated video final output
```
