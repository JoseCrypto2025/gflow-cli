# AI Project Secretary Integration

This fork is used as the Google Flow / Veo generation dependency for the **AI Project Secretary** project.

## Integration scope

The current scope is intentionally narrow:

```text
n8n Production workflow
        ↓
Prepare Prompt / Scene
        ↓
gflow-cli
        ↓
Google Flow / Veo
        ↓
Generated scene clips
        ↓
Existing Production post-processing / merge pipeline
```

The immediate goal is to replace the existing **Phaya** video-generation step inside the existing n8n `Production` workflow with `gflow-cli`.

## Current rules

- Keep `develop` aligned with upstream `ffroliva/gflow-cli`.
- Project-specific work must be made on the `ai-project-secretary-integration` branch.
- Do not copy or rewrite upstream source unless an integration requirement proves it is necessary.
- Prefer using the published `gflow-cli` package and its supported command surface.
- Preserve the existing n8n Production workflow outside the video-generation boundary until the current Phaya-to-merge interface has been inspected.

## Out of scope for this phase

Do not expand this integration into unrelated redesign work yet:

- SFX automation
- BGM selection
- database/schema redesign
- new retry framework
- MCP-first redesign
- replacement of the existing merge pipeline
- subtitle redesign

These may be evaluated later only if the existing Production workflow proves they are required.

## Next implementation step

Inspect the existing n8n `Production` workflow and document the exact boundary:

```text
Phaya input
→ Phaya output
→ generated clip files / metadata
→ first merge/post-processing node
```

Once that interface is known, replace only the Phaya generation step with the smallest compatible `gflow-cli` integration.

## Upstream

Upstream project:

- `ffroliva/gflow-cli`
- Default upstream development branch: `develop`

This fork should remain easy to synchronize with upstream.
