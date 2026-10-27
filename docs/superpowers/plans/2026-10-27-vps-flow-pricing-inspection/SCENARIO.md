# VPS Flow Pricing Inspection Scenarios

## Feature: Read current PROD Flow pricing and configuration without submitting

As an operator, I want a bounded read-only inspection of the existing `gflow-prod` profile so that I can record only values Flow visibly exposes for the exact configured project and current composer state.

### Scenario: Inspect the pinned production profile and project

Given the operator requests inspection for provider account `PVA_0002`
And the only permitted browser profile is `gflow-prod`
And the only permitted project is `aafe9282-071f-4333-baa4-92b9f76a9c27`
When the inspector acquires the profile lease and opens one bounded browser session
Then it verifies the expected account and exact project before reading the composer
And it returns only allowlisted, directly observed values and evidence digests
And it does not read or return the prompt, cookies, tokens, headers, or unrelated page content
And it does not invoke Generate, approval, reservation, or any provider submission action

### Scenario: Refuse contention and identity mismatches

Given the `gflow-prod` profile is already held by another process
When the operator invokes the inspector
Then it exits immediately with `PROFILE_IN_USE`
And it does not wait, launch another browser, or bypass Chromium's profile lock

Given the live account or project differs from the expected identity
When the inspector reaches identity verification
Then it returns a sanitized HOLD reason
And it reads no pricing/configuration evidence from the unexpected identity

### Scenario: Report model and settings without changing them

Given the exact account and project are verified
When the inspector reads the currently selected model and visible configuration
Then it reports the observed model, priority, duration, aspect ratio, output count, resolution, audio mode, and quality tier when structurally available
And it reports unavailable fields as `UNKNOWN` with a reason
And it does not select a model, change a setting, edit the prompt, or switch project/account

### Scenario: Report only a directly displayed pre-submit quote

Given the exact selected model and visible configuration are unchanged
When the inspector searches only a measured, passive pre-submit quote surface
Then it reports a displayed credit amount only when the UI binds it to the current model/configuration
And it distinguishes per-generation price from current account balance
And it returns `PRE_SUBMIT_CREDIT_COST_NOT_VISIBLE` when no authoritative quote is visible
And it never clicks Generate or a submit/approval control to reveal a quote

### Scenario: Capture privacy-bounded audit evidence

Given safe, narrowly scoped evidence can be captured
When the inspector writes evidence
Then the screenshot is clipped to verified project/model/quote context and has restrictive file permissions
And the DOM digest hashes only an allowlisted sanitized evidence envelope
And neither the report nor evidence contains prompts, credentials, cookies, tokens, HAR, or unrelated account content

### Scenario: Close without persistent Flow configuration changes

Given the inspection completes or fails
When the inspector closes its browser context
Then it releases the profile lease after the browser is closed
And it has not changed prompt, model, priority, project, account, settings, attempts, reservations, or credits
And failures do not trigger HAR capture, incident-retention cleanup, raw exception output, or automatic retry

### Scenario: Verify against the real existing VPS session

Given the exact implementation commit is deployed and parity checks pass
When the operator runs one bounded read-only inspection against `gflow-prod`
Then the run either returns verified live values or a precise sanitized HOLD/UNKNOWN reason
And it does not generate, approve, create attempts/reservations, or change canonical pricing data

## Surface decision

This is a VPS-only operator operation under `scripts/operations`, not a general CLI or MCP capability. It has no safe remote MCP caller because the authenticated production profile is private to the VPS. Do not change public CLI schemas or `gflow doctor`.
