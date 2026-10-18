# AI Editor Cinematic Research Report

**Research date:** 2026-10-18
**Scope:** Research and architecture inputs only. No Edit Plan schema, Sheet tabs/columns, renderer integration, D8 runtime, or publishing automation is implemented.

## 1. Executive Summary

A credible AI editor should choose edits from story, performance, sound, and continuity evidence. It should compare **CUT** with **HOLD CURRENT SHOT**, not cut on a fixed timer. Editorial grammar is a set of expectations that helps viewers follow action, not a set of absolute constraints. Walter Murch places emotional truth and story above rhythm and spatial continuity; empirical research finds action discontinuities can impair event segmentation more strongly than spatial or temporal changes. [Murch's Rule of Six](https://blogs.ischool.berkeley.edu/i290-viznarr-s12/the-rule-of-six-walter-murch/), [Magliano et al.](https://doi.org/10.1111/j.1551-6709.2011.01202.x)

**Recommendation — INFERENCE:** keep canonical story intent and generation identity in existing V2 rows; make an immutable, versioned Edit Plan a future transaction artifact; evaluate with explicit hard gates plus transparent editorial reasoning; render through interchangeable backends. Start with read-only asset inspection and human-reviewed proposals on a small benchmark. Do not connect a plan directly to irreversible rendering or publishing.

Evidence labels: **PROFESSIONAL_PRACTICE** (editor/vendor workflow guidance), **ACADEMIC_EVIDENCE** (research result), **TOOL_CAPABILITY** (verified product behavior), **OPEN_SOURCE_IMPLEMENTATION** (inspected repository), **INFERENCE** (design conclusion), **EXPERIMENT_REQUIRED** (not settled by sources).

## 2. Professional Editor Thinking

**PROFESSIONAL_PRACTICE:** an editor chooses a cut to shape audience attention and emotional reading, then tests it in context. Murch's six considerations—emotion, story, rhythm, eye-trace, two-dimensional composition, and three-dimensional continuity—are a prioritization prompt, not a universal score; his percentages are tongue-in-cheek. [Murch](https://blogs.ischool.berkeley.edu/i290-viznarr-s12/the-rule-of-six-walter-murch/)

For each candidate, retain concise evidence: what story information changes, which performance moment is gained/lost, what the eye follows, continuity risk, and intended emotional effect. **INFERENCE:** show alternatives and rationale; avoid one opaque “best edit” score.

## 3. Continuity Editing

**PROFESSIONAL_PRACTICE:** match on action, eyeline, screen direction, shot/reverse-shot, establishing/re-establishing views, inserts, and cutaways help viewers infer a stable scene. Use them when viewers need geography, causal action, or who addresses whom. They may be broken deliberately for surprise, disorientation, emphasis, or a clear viewpoint change.

**ACADEMIC_EVIDENCE:** continuity can bridge spatial and temporal discontinuities when action stays legible; in one study, action discontinuity affected event segmentation more than spatial or temporal discontinuity. This supports action-continuity checks with geography warnings, not absolute spatial locks. [Magliano et al.](https://doi.org/10.1111/j.1551-6709.2011.01202.x)

**INFERENCE:** check action state, axis/screen direction, gaze, and object/character placement separately. An intentional rule break should state its editorial intent and receive human review early on.

## 4. Performance Editing

Select the take and range that best communicates the performance: expression, hesitation, breath, glance, posture, movement, and listener change. Evaluate a moment with its lead-in and tail; a peak expression without setup can read falsely.

Do not treat silence as dead air by default. A pause can convey thought, discomfort, comedy, grief, or anticipation. **EXPERIMENT_REQUIRED:** frame-level face/emotion models may be candidate evidence, but need calibration on reviewed scenes and must not replace dramatic context.

## 5. Dialogue Editing

Respect turns, interruptions, overlap, pauses, emphasis, and listening. Transcription gives words and rough timing; it does not decide who should occupy the frame. Adobe describes shot/reverse-shot as a common dialogue structure while leaving its duration to the scene. [Adobe shot/reverse shot](https://www.adobe.com/creativecloud/video/production/cinematography/camera-shots-and-angles/reverse-shot.html)

A future plan should represent dialogue spans independently of picture boundaries. Preserve an audible line across a picture cut when intelligibility/performance permit; check that reaction footage does not reveal visibly mismatched lip movement.

## 6. Reaction Strategy

The listener can matter more than the speaker when a line changes a relationship, reveals realization, or creates a comedic/dramatic beat. A speaker close-up may instead be necessary for delivery, physical action, or a crucial line. **PROFESSIONAL_PRACTICE:** compare speaker and listener coverage as competing candidates. **INFERENCE:** a reaction decision needs role, timing, visible-mouth state, and reason; do not cut from speaker labels alone.

## 7. Pacing / Rhythm

**PROFESSIONAL_PRACTICE:** rhythm changes with genre, information density, performance, and intent. A cut every N seconds is only a narrow style preset, never a global policy. Compare shot-length variation and hold duration within scene beats; allow acceleration, deceleration, stillness, rapid cutting, and long takes when motivated.

**ACADEMIC_EVIDENCE:** editing can affect perceived duration; one study compared continuity edits, action discontinuity edits, and no editing. It does not establish a universal ideal pace. [Movie editing and time perception](https://pmc.ncbi.nlm.nih.gov/articles/PMC9684412/)

**INFERENCE:** uniform-duration and unexplained-acceleration detectors should warn with an exception path, not automatically reject.

## 8. Emotional Editing

Track CURRENT_EMOTION, TARGET_EMOTION, EMOTIONAL_BEAT, and intensity as scene-level interpretive evidence with provenance/confidence. A cut may preserve, shift, delay, or contradict apparent emotion for irony or suspense. Murch supports emotion/story priority but not a universal measurable mapping. [Murch](https://blogs.ischool.berkeley.edu/i290-viznarr-s12/the-rule-of-six-walter-murch/)

**INFERENCE:** check contradictions between intended beat and candidate footage; rank alternatives with human-readable rationale. Emotion prediction is not sole authority.

## 9. Comedy Timing

For Sri Thanonchai-style material, preserve setup → expectation → misdirection → reveal/punchline → reaction/hold. A pause can be the joke. A cutaway may clarify or intensify embarrassment, but may also drain the punchline. Timing can be frame-sensitive; review at delivery frame rate and in context.

**EXPERIMENT_REQUIRED:** benchmark alternate reaction trims/holds on rights-cleared scenes. Do not remove silence globally or enforce one punchline duration.

## 10. Scene Geography

A runtime SCENE_GEOGRAPHY_STATE may summarize axis, character positions, movement direction, entrances/exits, gaze relations, object locations, and visible cast. Derive it from canonical scene/character facts and inspected media, with evidence and confidence. It is not a new Google Sheet source of truth.

**INFERENCE:** a geography mismatch is a warning if the cut deliberately establishes a new axis/time/space; unexplained impossible movement is a stronger gate.

## 11. Shot Selection

Wide, medium, close, extreme close, POV, OTS, insert, and reaction are choices based on information and emotional function. Wide can orient; closer coverage can prioritize detail/performance; a return to wide can re-establish changed geography. Wide→medium→close is a useful pattern, not a law. [Adobe shot/reverse shot](https://www.adobe.com/creativecloud/video/production/cinematography/camera-shots-and-angles/reverse-shot.html)

## 12. J/L Cuts

Picture and audio boundaries must be independent. A J-cut brings next audio before its image; an L-cut lets prior audio continue over the next image. Adobe documents both as split edits commonly used in dialogue/transitions. [Adobe J/L cuts](https://helpx.adobe.com/ie/premiere/desktop/edit-projects/trim-clips/perform-j-cuts-and-l-cuts.html)

**INFERENCE:** never force picture and sound cuts to coincide. Represent audio source, trims, timeline in/out, fades, and relation to picture; check dialogue continuity after render.

## 13. Sound

Treat dialogue, room tone, ambience, effects, foley, music, and intentional silence as separate layers with independent edit points. Independently generated clips need continuity checks for noise floor, reverberation, ambience, perspective, and loudness. Repairs may include crossfade, room-tone fill, ambience bed, dialogue repair, trim, or human review.

**PROFESSIONAL_PRACTICE:** sound editing/mixing is a distinct finishing pass; Resolve training presents editing, sound, and color as separate skills. [Blackmagic training](https://www.blackmagicdesign.com/au/products/davinciresolve/training)

**INFERENCE:** retain original audio and metadata; sound masking must not cover a severe story/identity defect.

## 14. Music

Music should enter, exit, duck, build, hit, or stop for an editorial reason. Protect dialogue intelligibility; do not synchronize every visual cut to a beat. A hit point is an option, not a mandate. Use envelopes and explicit transitions; evaluate music against scene tension and emotional arc.

## 15. Visual Continuity

Automatable measurements include exposure, white balance, contrast, color cast, skin-tone range, grain/noise, sharpness, frame rate, and crop. These are measurements, not final artistic judgments. Blackmagic training treats color management and shot-level correction as explicit workflows. [DaVinci Resolve training](https://www.blackmagicdesign.com/au/products/davinciresolve/training)

**INFERENCE:** normalize small technical mismatches inside measurable bounds. Wardrobe, identity, prop, lighting, or axis changes that affect story should warn, hold for review, or request regeneration rather than be hidden by grading.

## 16. AI Footage Failure Modes

| Failure | Candidate response |
|---|---|
| Minor duration excess/awkward head or tail | TRIM |
| Isolated bad boundary/frame | TRIM / REEDIT / HUMAN_REVIEW |
| Correct beat with unstable span | CUTAWAY / REACTION_SHOT / INSERT if supported by footage |
| Small measurable exposure/white balance mismatch | COLOR_NORMALIZE |
| Ambience gap/mild audio discontinuity | SOUND_MASK / sound edit / HUMAN_REVIEW |
| Character/wardrobe/prop identity drift affecting story | REGENERATE or HUMAN_REVIEW |
| Contradictory action, impossible geography, severe lip-sync | REGENERATE or HUMAN_REVIEW |

**INFERENCE:** classify severity and story impact before repair. Do not use transitions/sound to conceal a severe defect.

## 17. Professional Workflow

Use gated steps: ingest/checksum → organize/sync → selects → assembly → rough cut → fine cut → picture lock → sound/music → color → QC → master/archive. Color/sound may overlap after lock; revisions can reopen work with explicit versions. The South African Guild of Editors describes assembly/rough-cut refinement followed by fine tuning with editorial stakeholders. [Guild guide](https://www.editorsguildsa.org/system/asset/file/374/SAGE_what_to_expect_from_an_editor_v2.pdf)

## 18. Multi-Pass Proposal

**INFERENCE — future workflow only:** 0) Asset QC/identity manifest; 1) story assembly; 2) continuity/geography; 3) performance candidates; 4) pacing/emotional beats; 5) dialogue/ambience/effects/music; 6) visual continuity; 7) render validation/editorial QC. Each pass returns versioned evidence and suggested changes; failures stop affected output. Nothing here implements those passes.

## 19. Decision Model

Hard gates: wrong/unauthorized assets, missing required dialogue, invalid trims, unapproved inputs, unsupported media, impossible timeline bounds. For candidates that pass, rank transparently: story clarity/performance intent first; emotion, dialogue, continuity/geography, gaze, rhythm, sound next; technical polish last. Expose dimensions and trade-offs.

**INFERENCE:** flat weighted sums can hide severe story/continuity failure behind unrelated high values. Hard gates plus transparent comparisons are more auditable.

## 20. Editor State

Candidate temporary EDITOR_STATE values:

- Previous shot, shot size, camera side/axis, direction, gaze, character/object positions, visible cast — DERIVED RUNTIME from clips/scene evidence.
- Speaker/listener, dialogue span, emotion, reaction pending — DERIVED RUNTIME / EDIT EVIDENCE.
- Music/ambience state — DERIVED RUNTIME from source audio/timeline.
- Visual reference state — REFERENCE ONLY to stable asset IDs and approved style references.
- Prior decisions and exception rationale — versioned EDIT PLAN EVIDENCE.

Do not duplicate SCENES continuity/audio direction, SCENE_CHARACTER_MAP positions/actions/emotion/continuity, SCENE_DIALOGUE_LINES, or SCENE_REFERENCE_MAP as canonical facts.

## 21. EditPlan Design Inputs

| Concept | Classification | Existing V2 ownership / note |
|---|---|---|
| Production_Job_ID | MUST_REPRESENT | PRODUCTION_JOBS |
| Scene_ID | MUST_REPRESENT | SCENES; resolve via prompt |
| Scene_Job_ID | MUST_REPRESENT | SCENE_JOBS |
| TTS_Job_ID | SHOULD_REPRESENT | TTS_JOBS; optional if no TTS |
| asset ID/URI | MUST_REPRESENT | Reference/job/output IDs; stable identity |
| source trim | MUST_REPRESENT | Missing from production tables |
| timeline placement | MUST_REPRESENT | Missing from production tables |
| separate picture/audio cuts | MUST_REPRESENT | Missing; needed for J/L cuts |
| cut reason | SHOULD_REPRESENT | Missing; rationale should be versioned |
| transition | SHOULD_REPRESENT | Renderer instruction; explicit intent useful |
| dialogue placement/reaction role | SHOULD_REPRESENT | Text/voice exist; timeline placement/listener role absent |
| ambience/music/SFX/ducking | SHOULD_REPRESENT | SCENES has Audio_Direction; TTS_JOBS stores speech, not timeline mix |
| crop/reframe/color normalization | SHOULD_REPRESENT | Scene/style hold intent; per-clip decisions absent |
| output target | MUST_REPRESENT | FINAL_OUTPUT has output metadata; target/codec profile belongs to render config |
| QC requirements | SHOULD_REPRESENT | Existing validation statuses; editorial dimensions absent |
| plan version/hash | MUST_REPRESENT | Needed for replay/audit; do not store in Scene rows |

These are assessments of checked-in schemas/dictionaries, not instructions to change them.

## 22. Timeline Engine Options

**A — AI→custom FFmpeg:** deterministic/scriptable; larger engineering surface for timeline semantics, effects, sound, preview.
**B — AI→FableCut:** quick visual review/agent control; project JSON is editor-specific and requires capability/version management.
**C — canonical Edit Plan→FableCut adapter:** preserves canonical plan while using FableCut for review/export; requires tested adapter and loss reporting.
**D — renderer-neutral plan→interchangeable backends:** strongest portability/auditability if precise; higher initial validation cost.

**Recommendation — INFERENCE:** evolve toward D, starting with one local adapter prototype. C can be a review prototype after license/security review. Do not install FableCut in production or alter the FFmpeg service now.

**Adjacent tool capabilities — TOOL_CAPABILITY:** [Auto-Editor](https://github.com/WyattBlue/auto-editor) analyzes media (including loudness/silence) and exports editor timelines; use only for ingest suggestions, because automatic silence removal conflicts with intentional pauses. [HyperFrames](https://github.com/heygen-com/hyperframes) and [Remotion](https://www.remotion.dev/docs/render) provide code-driven HTML/React rendering, which suits graphics and templated sequences more than performance-led dialogue cutting. [OpenTimelineIO](https://github.com/AcademySoftwareFoundation/OpenTimelineIO) is an editorial timeline interchange API/format, not a renderer. These may be backend/interchange candidates after licensing, capability, and deterministic replay tests; none is installed or integrated.

## 23. Open Source Findings

Snapshot: **2026-10-18**. SHAs are exact default-branch heads returned by GitHub commit search during research; activity is the latest inspected commit, not a maintenance guarantee.

| Project | Branch / HEAD / activity | License evidence | Architecture and tests inspected |
|---|---|---|---|
| [ronak-create/FableCut](https://github.com/ronak-create/FableCut) | main, a963fa58bc3c40d9972cc6b5dba300f9b2700fa5, 2026-10-17 | MIT in root LICENSE and package metadata | Browser NLE, project.json timeline, REST/MCP/SSE, local media, optional ffmpeg export; package has node --test and test directory. Prototype candidate; review bind/import/export security before deployment. |
| [inematds/skill-video-plan-editor](https://github.com/inematds/skill-video-plan-editor) | main, e2aa67537342cdb83477a3948a305fc2b9576d04, 2026-10-05 | README says “A definir” (to be determined): treat as no license grant | Pydantic EditPlan, CLI scaffold/validate/summary, presets/skill knowledge, optional HyperFrames render. Tests cover model round-trip, bounds, enum checks. Fixed average-cut presets need adaptation to drama/editorial evidence. |
| [inematds/mdd](https://github.com/inematds/mdd) | master, 26a6c25acb88d6b59c227f88fb48a5e1fe1db64f, 2026-10-05 | README says MIT; GitHub lookup found no root LICENSE; verify before reuse | Skill creates project card, scene package, style/continuity locks, storyboard, director track, final/negative prompts. No code/test suite surfaced in inspected tree; primarily prompt/workflow content. |
| [vatsanatech/bmad-for-filmmaking](https://github.com/vatsanatech/bmad-for-filmmaking) | main, e0c73ccb1817f99792ab789fbc3d5f5f176cde01, 2026-02-26 | No root LICENSE found by GitHub contents lookup; treat as unlicensed until clarified | BMAD agents/workflows for synopsis, character bible, screenplay, shot breakdown, with example project. Preproduction assistance, not a timeline/render engine. No tests surfaced in repo listing. |

**Evidence limits:** inspected repository pages, READMEs, manifests, tests, and license paths; no clone was made. SHAs anchor the snapshot. “No license found” means do not reuse code/content absent a verified grant; it is not a legal conclusion.

## 24. QC Framework

Assess narrative clarity, emotional progression, performance, reaction choice, continuity, pacing, dialogue naturalness, audio continuity, visual continuity, and technical correctness separately. Report PASS, REEDIT, REGENERATE_SCENE, or HUMAN_REVIEW with evidence and time spans.

A future AI_EDIT_CONFIDENCE may summarize uncertainty only while retaining each dimension; it cannot override a hard gate. **INFERENCE:** calibrate thresholds on benchmark scenes.

## 25. Human vs AI Gap

Compare 5–10 short rights-cleared drama/dialogue scenes. Use identical source assets; compare AI proposal/render against human reference or human-reviewed edit. Blind reviewers rate story clarity, emotion, reaction, pacing, geography, dialogue flow, audio naturalness, over-editing, unmotivated cuts, and “stitched AI” feel. Track disagreement and confidence calibration, not only mean scores. Do not reproduce copyrighted sequences.

## 26. Recommended Gflow V2 Architecture

Keep V2 Sheets canonical. Resolve approved Scene Prompt → Generation Profile → account/model and exact output assets. Create a versioned renderer-neutral Edit Plan referencing immutable IDs/hashes. Validate provenance, trims/timeline bounds, media identity, and explicit exception reasons. Render through an adapter; independently inspect QC; record verified persistent output in FINAL_OUTPUT. Preserve source clips and render manifest for replay.

Research recommendation only; production runtime and FFmpeg service stay unchanged.

## 27. What to Build First

1. Read-only source-media manifest: identity, duration, frame rate, dimensions, audio streams, checksum.
2. Human-reviewed proposal format in a temporary offline workspace, not a Sheet schema.
3. Fixed 5–10-scene benchmark and rubric; record editor disagreement.
4. Hard validation for job/asset identity, trims, timeline bounds, and picture/audio separation.
5. One renderer adapter in an isolated local prototype; compare with manual edits.
6. Only after benchmark success, design Edit Plan versioning and persistence ownership.

## 28. What NOT to Automate

Do not auto-approve story interpretation, hide identity drift with effects, remove silence globally, cut at fixed intervals, force cuts to music beats, infer listener shots from speaker labels alone, treat confidence as truth, overwrite human-locked work, or auto-publish. Keep severe continuity/story repair and ambiguous emotional beats in human review.

---

## Source register

- Murch, Rule of Six excerpt: https://blogs.ischool.berkeley.edu/i290-viznarr-s12/the-rule-of-six-walter-murch/
- Magliano et al., continuity editing/event segmentation: https://doi.org/10.1111/j.1551-6709.2011.01202.x
- Zacks et al., segmentation in film comprehension: https://pmc.ncbi.nlm.nih.gov/articles/PMC8710938/
- Movie editing and time perception: https://pmc.ncbi.nlm.nih.gov/articles/PMC9684412/
- Adobe, shot/reverse-shot and J/L cuts: https://www.adobe.com/creativecloud/video/production/cinematography/camera-shots-and-angles/reverse-shot.html ; https://helpx.adobe.com/ie/premiere/desktop/edit-projects/trim-clips/perform-j-cuts-and-l-cuts.html
- Blackmagic Resolve training: https://www.blackmagicdesign.com/au/products/davinciresolve/training
- South African Guild of Editors guide: https://www.editorsguildsa.org/system/asset/file/374/SAGE_what_to_expect_from_an_editor_v2.pdf
- Auto-Editor, HyperFrames, Remotion, and OpenTimelineIO: https://github.com/WyattBlue/auto-editor ; https://github.com/heygen-com/hyperframes ; https://www.remotion.dev/docs/render ; https://github.com/AcademySoftwareFoundation/OpenTimelineIO
- FableCut source HEAD: https://github.com/ronak-create/FableCut/tree/a963fa58bc3c40d9972cc6b5dba300f9b2700fa5
- Video Plan Editor source HEAD: https://github.com/inematds/skill-video-plan-editor/tree/e2aa67537342cdb83477a3948a305fc2b9576d04
- MDD source HEAD: https://github.com/inematds/mdd/tree/26a6c25acb88d6b59c227f88fb48a5e1fe1db64f
- BMAD Film source HEAD: https://github.com/vatsanatech/bmad-for-filmmaking/tree/e0c73ccb1817f99792ab789fbc3d5f5f176cde01

**EDIT_PLAN_V1_READY_FOR_DESIGN = YES** — ownership and design inputs are researched; implementation remains a separate reviewed phase.
**IMPLEMENTATION_STARTED = NO**
