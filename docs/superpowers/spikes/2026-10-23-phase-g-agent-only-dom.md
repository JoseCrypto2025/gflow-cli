# Spike: Phase G Agent-only TEST DOM anchors

**Date:** 2026-10-23 · **Surface:** `flow.google.com` · **Profile:** `gflow-v2-test`
· **Cost:** $0; navigation and DOM reads only · **Script:**
[`spike_phase_g_agent_only_dom.py`](../../../scripts/dev/spike_phase_g_agent_only_dom.py)

## Question

Does the current authorized TEST project expose the measured structural editor,
submit, in-flight, permission, tile, and chat-video components needed by the
Phase G Agent-only adapter after the page hydrates?

## Pre-registered readings

- A stable, verified TEST project with `div.ProseMirror` and
  `flow-generate-icon-button button` present confirms those two structural
  anchors for this observed session.
- A stable `flow-stop-icon-button` count of zero before submission is expected;
  its post-submit transition is not measured by this read-only probe.
- A timeout, unstable snapshots, failed identity/project verification, or
  `UNKNOWN` surface is inconclusive and does not establish selector absence.
- No settings are changed, no prompt is submitted, and Generate is never clicked.

## Measurement

The verified account identity and exact project URL passed. The surface was
`AGENT_ONLY`, and the final three snapshots were identical. Counts were:

| Anchor | Count |
|---|---:|
| `div.ProseMirror` | 1 |
| `flow-generate-icon-button button` | 1 |
| `flow-stop-icon-button` | 0 |
| live permission gate | 0 |
| `flow-video-tile` | 1 |
| `flow-pending-tile` | 0 |
| `flow-a2ui-video-option` | 0 |

No settings changed, no prompt was submitted, and Generate was not clicked.
This measured the editor and submit anchors on the current TEST session. It did
not exercise the in-flight or generated-media transition; zero chat video
options in this baseline is not evidence that the component is unavailable.
