# Google Flow Data Contract

## Capability source

Capabilities below reflect official Google Flow Help, checked 2026-10-15: https://support.google.com/flow/answer/16352836. The source reports both aspect ratios without naming their values; no ratio values are inferred. Model names are Flow display labels, not API identifiers. Availability can vary by region, plan, web/mobile surface, or account. Reverify at runtime.

| Model label | Mode | Duration (sec) | Aspect ratios |
|---|---|---:|---|
| Veo 3.1 Lite | TEXT_TO_VIDEO | 4, 6, 8 | Both as documented |
| Veo 3.1 Lite | FIRST_FRAME_TO_VIDEO | 4, 6, 8 | Both as documented |
| Veo 3.1 Lite | FIRST_LAST_FRAME_TO_VIDEO | 4, 6, 8 | Both as documented |
| Veo 3.1 Lite | INGREDIENTS_TO_VIDEO | 8 | Both as documented |
| Veo 3.1 Lite | EXTEND_VIDEO | 8 | Both as documented |
| Veo 3.1 Fast | TEXT_TO_VIDEO | 4, 6, 8 | Both as documented |
| Veo 3.1 Fast | FIRST_FRAME_TO_VIDEO | 4, 6, 8 | Both as documented |
| Veo 3.1 Fast | FIRST_LAST_FRAME_TO_VIDEO | 4, 6, 8 | Both as documented |
| Veo 3.1 Fast | INGREDIENTS_TO_VIDEO | 8 | Both as documented |
| Veo 3.1 Quality | TEXT_TO_VIDEO | 4, 6, 8 | Both as documented |
| Veo 3.1 Quality | FIRST_FRAME_TO_VIDEO | 4, 6, 8 | Both as documented |
| Veo 3.1 Quality | FIRST_LAST_FRAME_TO_VIDEO | 4, 6, 8 | Both as documented |
| Gemini Omni Flash 1.1 | TEXT_TO_VIDEO | 4, 6, 8, 10 | Both as documented |
| Gemini Omni Flash 1.1 | FIRST_FRAME_TO_VIDEO | 4, 6, 8, 10 | Both as documented |
| Gemini Omni Flash 1.1 | FIRST_LAST_FRAME_TO_VIDEO | 4, 6, 8, 10 | Both as documented |
| Gemini Omni Flash 1.1 | INGREDIENTS_TO_VIDEO | 4, 6, 8, 10 | Both as documented |
| Gemini Omni Flash 1.1 | VIDEO_TO_VIDEO | up to 10 | Both as documented |

## Explicit limitations

- Veo 3.1 Lite supports extending Veo 3.1 videos at 8 seconds; Fast and Quality do not support extend. All Veo 3.1 8-second videos can be extended using Lite.
- Veo 3.1 Lite, Fast, and Quality do not support video-to-video editing.
- Gemini Omni Flash 1.1 supports video-to-video editing up to 10 seconds; Extend is listed as coming soon.
- Omni standard 720p and draft 360p are documented. No universal Veo resolution list is inferred.
- No prices or credit rates were inserted. No generation was performed and no Flow credits were consumed.

## Runtime compatibility

Resolve WHO (subject/character), WHAT (action/emotion/dialogue), WHERE (location/environment), HOW IT LOOKS (style/lighting/camera), and HOW IT IS GENERATED (provider/model/mode/references/frames/ratio/duration/resolution/output configuration). Resolve supported combinations through MODEL_MASTER and MODEL_CAPABILITY_MAP before execution. A Flow help page does not prove a public API or stable automation interface exists.

## Adapter boundary for V2.1

Provider-specific behavior stays behind a provider adapter. Google Flow, direct APIs, and future providers may differ in model names, parameter keys, account/project concepts, and billing units. PROVIDER_ACCOUNT_MASTER, MODEL_PARAMETER_MAP, GENERATION_PROFILE_MASTER, and PROVIDER_COST_MASTER hold normalized configuration and verified reference data; they do not store secrets or implement runtime automation. Authentication material stays in the worker's secure secret store and is referenced only by Auth_Profile_Key.

Scenes and jobs must not encode Google Flow model names, project IDs, or UI fields. No generation was performed for this data contract; no Flow credits or paid API calls were used.

## V2 scene boundary

EPISODE_MASTER, SCRIPT_MASTER, and SCENES store creative intent and editorial approval only. A future production job resolves `Generation_Profile_ID` to provider account, model, generation mode, parameter map, and capability data. Provider-specific identifiers and controls do not belong in SCENES. Phase C adds only a no-I/O router contract; provider execution is disabled.
