# Scenario: V3 Project Listing Enumeration

## Coverage map

- D1 Auth/session: relevant only as a live collection gate; use the pinned TEST account and fail closed if identity verification fails.
- D2 WAF/reCAPTCHA: no submit or auth change; ensure the observer performs no request mutation and stop on provider refusal.
- D5 Concurrency/Page lifecycle: attach before reload, await observer work, remove listener in `finally`, and keep the profile lease lifecycle intact.
- D7 Error handling: incomplete or malformed enumeration must be unavailable evidence, never an empty project history.
- D8 Paths: preserve existing `Path` and UTF-8 evidence serialization.
- D9 Transport: decode only proven video record shapes and exact `Zzl0ze` listing frames; preserve `generation_record()` behavior.
- D12 Observability/privacy: never persist or print response bodies, signed URLs, prompts, tokens, or cookies.
- D13 MCP parity: no user-facing CLI/MCP capability is added; the standalone operations collector has no queued worker twin.

## Scenario table

| # | Dimension | Scenario | Severity | Expected behaviour | Test category |
|---|---|---|---|---|---|
| 1 | D9 Transport | One payload contains several nested video records and repeated copies | High | Return all unique records in deterministic order using the existing record invariant | Unit |
| 2 | D9 Transport | Project listing mixes image records, arbitrary UUID arrays, and video records | Critical | Decode only measured video record shapes; ignore unrelated shapes and reject ambiguous candidates | Unit |
| 3 | D9 Transport | Listing has a known historical record and a new execution or media identity | Critical | Fail closed; create no forensic evidence | Integration |
| 4 | D5 Page lifecycle | Reload produces no `Zzl0ze` response, a body read error, or a malformed record payload | Critical | Mark enumeration unavailable and create no evidence | Integration |
| 5 | D5 Page lifecycle | Three snapshots reuse one listing response instead of causing three reloads | High | Reject the observation; require three independently observed project listings | Integration |
| 6 | D9 Transport | Listing and live status observer return the same historical record | High | Union and deduplicate by workflow/project/media identity | Integration |
| 7 | D9 Transport | Listing record status or identity set changes between reload snapshots | Critical | Stop before evidence creation | Integration |
| 8 | D12 Privacy | Listing includes signed media/poster URLs and prompt metadata | Critical | Keep raw body transient; evidence contains only sanitized identity/status fields | Integration |
| 9 | D1 Auth/session | Current account or TEST project does not match pinned values | Critical | Stop before collection and close the session | Integration |

## Must-cover before merge (Critical + High)

1. Decoder tests cover nested/multiple records, deduplication, exact statuses and media fields, and unrelated UUID-containing arrays.
2. Collector tests cover missing listing, malformed listing, new identities, record union/deduplication, unstable snapshots, and sanitized evidence.
3. Existing singular decoder and normal Phase G attribution tests remain unchanged and green.
4. Live collection is read-only, TEST-account/project-pinned, and requires three independent listing observations over the contract's bounded interval.

## Deferred (Medium + Low — log as issues, not blockers)

- The observation horizon cannot prove permanent provider absence; evidence remains scoped to its timestamp and 60–120 second observation window.
- No new user-facing CLI or MCP surface is introduced.

## Suggested BDD scenarios

These scenarios exercise parser and collector code directly, so they remain offline integration tests rather than browser-only BDD bindings.

```gherkin
Feature: V3 enumerates provider records from the project listing
  Scenario: A listing with multiple historical video records is completely enumerated
    Given the TEST project reload returns a Zzl0ze listing with nested video records
    When the V3 collector observes three stable project reloads
    Then it records the sanitized union of historical provider identities
    And it persists no response body, signed URL, or prompt metadata

  Scenario: A listing is missing or malformed
    Given the TEST project reload does not yield a decodable Zzl0ze listing
    When the V3 collector evaluates the observation
    Then it creates no forensic evidence
```

## Known-issues cross-reference

- [`docs/superpowers/spikes/2026-09-08-migrated-queue-is-readable-on-zzl0ze.md`](../../spikes/2026-09-08-migrated-queue-is-readable-on-zzl0ze.md) measured project-load record enumeration on `Zzl0ze` and distinguished length-8 video records from length-7 image records. It supports using the listing as an enumeration source, not as attempt attribution.
- No direct `KNOWN_ISSUES.md` entry blocks this collector-only observer. Unknown records remain ambiguous and block closure.
