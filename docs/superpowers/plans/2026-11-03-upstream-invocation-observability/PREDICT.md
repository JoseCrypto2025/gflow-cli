# Predict: Upstream Invocation Observability Hardening V1

## Verdict: GO
**Confidence:** 8/10

## Summary

Five independent reviews support a narrow adapter-to-ledger observability change. Keep the pinned upstream invocation, strict JSON result contract, and one-shot behavior unchanged. The main risks are secret leakage in output excerpts and the current caller persisting results only after the adapter returns; both are addressed by redacted bounded diagnostics and a durable evidence callback.

## Persona findings

### Architect — GO (8/10)

Keep process execution in the upstream adapter and persistence in the canary caller's existing `ProductionCreditGuard`. An injected callback preserves the boundary and lets the caller durably record process-start and termination events before JSON normalization. Preserve older ledger rows and the Canary 02 history.

### Security / reCAPTCHA — CAUTION (8/10)

The existing redactor handles common credentials and signed URLs but does not guarantee that arbitrary standalone token-like strings are safe. Redact before clipping, add conservative long-token masking, and omit snippets if redaction fails. Do not store raw exception documents or raw output. Represent absent submit evidence as `UNKNOWN`.

### Performance / Playwright — GO (8/10)

Byte counts and hashes can be collected in the existing pipe-drain threads with constant work and no extra browser or provider call. Preserve the current output cap. Persist process start before writing the request to child stdin; persist process termination before parsing.

### CLI / MCP UX / Cross-platform — GO (8/10)

This internal adapter/caller change needs no CLI flags, exit codes, MCP tool, or queue changes. Store UTC ISO timestamps and JSON-safe values. Keep signal interpretation platform-aware and missing action/request evidence as `UNKNOWN`.

### Devil's Advocate — GO (8/10)

The task directly targets a proven observability gap. The smallest viable change is bounded pipe telemetry plus the existing durable canary ledger; a new service, browser observer, CLI surface, or JSON contract change would add unnecessary risk. No known issue or active-plan decision blocks this work.

## High-confidence risks (flagged by 2+ personas)

1. Persisted stdout/stderr excerpts may contain credentials, signed URLs, or private data. Redact the bounded captured stream before clipping and add adversarial tests.
2. A crash can occur between process start and normalized result persistence. Persist lifecycle evidence through the caller callback before later parse/normalization stages.
3. The pinned upstream CLI does not expose all Generate/submit/provider facts. Keep unavailable values `UNKNOWN`; do not infer false or add Flow UI instrumentation.

## Conflicts resolved

- Security favored caution on excerpts while the other personas supported the approach. The plan resolves this with conservative masking, strict caps, omit-on-redaction-error, and no parser relaxation.
- The adapter owns subprocess telemetry while the caller owns the durable ledger. An injected event callback keeps those responsibilities separate.

## Recommended next step

Create the scenario and implementation plan, then implement offline with no provider invocation and no Canary 02 mutation.
