# Legacy API Compatibility Contract

This document defines a future compatibility boundary. It does not add an HTTP listener, route, webhook, or change the existing `gflow` CLI / Flow API client.

## Request translation

Legacy callers may provide creative prompt intent and a duration. The compatibility layer must resolve a `Generation_Profile_ID` (explicitly supplied or chosen by a future catalog-backed policy) before dispatch. It must not accept provider-specific model names, account/project IDs, UI selectors, or credentials as an alternate source of truth.

Known legacy request keys are `image_url`, `prompt`, `duration`, `aspect_ratio`, and `zoom`. The pure request model can carry those fields plus an optional internal `profile_id`; it does not fetch the image URL.

Duration is checked by resolving `GENERATION_PROFILE_MASTER` to its `Model_ID` and `Generation_Mode`, then reading `MODEL_CAPABILITY_MAP`. For example, legacy `duration=30` is never forwarded blindly; if the selected model/mode cannot accept 30 seconds, the result requires explicit atomic scene planning or a verified provider-native operation. A supported duration may proceed to profile validation. An unsupported or overlong duration returns `requires_scene_plan` and the verified per-scene maximum. The layer does not invent scene boundaries, truncate duration, or execute generation.

## Response shape

Known legacy response keys are `job_id`, `status`, `video_url`, and `error`. The future adapter should preserve those keys, leave `video_url` empty until an output has been verified, and map failures to stable machine-readable error codes without leaking secrets. The exact envelope must be checked against consumers before any listener or production route is introduced. Intended routes are `POST /api/gflow/jobs` and `GET /api/gflow/jobs/{job_id}`; neither is registered in this phase.

The existing relay remains configured outside this package; its inspected listener is `172.16.3.1:18766`. No port mapping or relay behavior was changed.

## Compatibility guarantees for the foundation

- No endpoint or listener is created.
- Existing Flow CLI and `FlowApiClient` remain unchanged.
- No generation request is submitted.
- Calls that require provider execution fail closed with `PROVIDER_ADAPTER_UNAVAILABLE` until an approved adapter exists.
- Before exposing an API, verify consumer expectations, authentication, idempotency, timeout/retry semantics, and error mapping in a separate deployment review.
