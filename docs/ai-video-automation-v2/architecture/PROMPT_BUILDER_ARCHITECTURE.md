# Phase E Prompt Builder Architecture

Phase E compiles one locked SCENES row using an explicit Generation_Profile_ID.
It is a deterministic, provider-neutral library boundary under
src/gflow_cli/v2_runtime/phase_e.py. It reads normalized rows through the
Runtime Foundation MasterCatalog contract extended by SceneCatalog.rows.
Tests use InMemorySceneCatalog. No compiler code calls Sheets, Drive, a
browser, a provider, n8n, or a local workstation path.

## Canonical flow

    Scene_ID + Generation_Profile_ID + runtime environment
      -> Scene / Script / Episode / Series context
      -> explicit profile -> account -> provider + model -> prompt template
      -> references and inherited visual settings
      -> capability and parameter validation
      -> semantic prompt blocks -> template rendering
      -> deterministic hashes + PREFLIGHT_PASS / PREFLIGHT_HOLD
      -> optional in-memory SCENE_PROMPT_VERSION candidate (VALIDATED)
      -> stop before approval or provider handoff

The compiler does not choose a profile, provider, model, account, or template
implicitly. Default-profile lookup, if needed later, is a separate operation
that must require environment and purpose and return only one unambiguous
ACTIVE default.

## Scene context and inheritance

The Scene must resolve to an APPROVED Script, Episode, and Series. It must be
GENERATED_VIDEO and LOCKED. Scene creative intent is read from existing Scene
fields, not copied into another table. A profile must be ACTIVE and match the
requested environment. Its account and model must be ACTIVE and belong to the
same Provider. The selected Prompt Template must be ACTIVE and compatible with
provider, model, and generation mode.

Approved inheritance is applied only where documented:

- Visual style: Scene override, then Series default.
- Camera: Scene override, then Location default, then Series default.
- Lighting: Scene override, then Location default, then Visual Style default.
- Aspect ratio: Scene override, then Series default. No profile/model fallback
  is introduced.
- Wardrobe: Scene-character mapping override, then Character default.
- Language: Script dialogue language, then Episode primary language, then
  Series primary language.

Characters are ordered by Sort_Order, then stable Scene Character and Character
IDs. The subject block uses canonical Prompt_Identity_Text, curated wardrobe
prompt text, and existing Scene Character instructions. Dialogue lines are
resolved in Line_No, then Dialogue_Line_ID order and included in source
context identity. The locked template vocabulary has no dialogue placeholder,
so dialogue is not injected into prose by guesswork. No TTS voice or credential
is resolved.

## References

ACTIVE explicit SCENE_REFERENCE_MAP rows are sorted by Sort_Order, then stable
IDs. For eligible entities without an explicit Scene reference,
REFERENCE_ENTITY_MAP candidates are ranked by primary flag, priority, and
stable IDs. Equal-ranked candidates for different assets hold with
REFERENCE_SELECTION_AMBIGUOUS; spreadsheet order is never priority.

Every selected asset must be ACTIVE, have Reference_Quality_Status=PASS, and
have a persistent Drive_File_ID. Descriptors preserve asset ID, role, version,
checksum, usage instruction, mention name, primary flag, and optional
provider-sync status. They adapt to the shared AssetReference type; no local
path or Drive Desktop dependency is introduced. Provider sync is reported but
not required unless a later adapter contract says so. Required first/last-frame
roles are checked structurally. TEXT_TO_VIDEO does not require image inputs.
EXTEND_VIDEO and VIDEO_TO_VIDEO hold with
SOURCE_VIDEO_INPUT_CONTRACT_MISSING: the locked relation role vocabulary has
no source-video role, and Phase E does not repurpose frame inputs.

## Semantic blocks and rendering

The only recognized placeholders are SUBJECT, REFERENCE_USAGE, ACTION,
ENVIRONMENT, EMOTION, CAMERA, LIGHTING, VISUAL_STYLE, CONTINUITY_RULES, and
NEGATIVE_CONSTRAINTS. Unknown tokens hold. A referenced empty positive block
holds; an empty negative-constraint block may render empty. Technical settings
remain structured and never enter prompt prose implicitly.

Continuity combines Scene start/end state and notes, Scene-character
overrides, Character/Wardrobe/Location rules, and at most one referenced
Scene's end state. Self-reference and a detected two-Scene cycle hold.

Negative constraints retain first occurrence in this stable source order:
Era; each Scene Character's Character then effective Wardrobe in Scene order;
Location; Visual Style; Scene override. Exact duplicates are removed. The
approved Negative_Template is rendered separately and then combined with
these constraints. System_Prompt is compiler metadata, not a persisted
SCENE_PROMPT_VERSION field; because it is part of the selected template row it
contributes to source-context identity.

## Technical settings and parameter keys

Resolved configuration remains structured: mode, requested/profile duration,
Scene/Series aspect ratio, resolution, output count, audio mode, quality tier,
canonical parameter values, and semantic adapter parameter keys. Model
capabilities are read from ACTIVE MODEL_CAPABILITY_MAP records. Enumerated
JSON values are checked as exact values; source prose such as “Both (official
source wording; values not enumerated)” is not converted into invented values.
When a required configured value cannot be safely checked, preflight holds and
emits CAPABILITY_NOT_MACHINE_ENUMERABLE.

ACTIVE MODEL_PARAMETER_MAP rows are selected by model and generation mode.
Their JSON defaults and allowed values must parse; values must match the
declared JSON type and configured allowed list. Missing required values,
duplicate active mappings, or malformed data hold. Adapter keys are semantic
names; UI selectors and coordinates remain future adapter code.

## Hashes and versioning

Canonical serialization is UTF-8 JSON with recursively sorted keys, compact
separators, stable list ordering, and non-finite numbers rejected. The source
hash includes only effective semantic context: Scene/Script/Episode/Series,
resolved Characters/Wardrobes, dialogue, references and asset identity,
Location/Style/Camera/Lighting/Era, profile/account/provider/model, capability
and parameter configuration, template/System Prompt, semantic blocks, and
structured technical configuration. Audit-only timestamps such as
Created_At, Updated_At, and Verified_At are excluded. Logical auth profile
references are excluded from results and hashes.

Prompt_SHA256 is SHA-256 over canonical JSON containing only the final
positive and negative prompt texts. Timestamps and IDs do not affect it.

Compilation and persistence remain separate. The in-memory payload builder
requires an explicit Scene_Prompt_ID and positive Prompt_Version; it does not
scan Sheets or implement unsafe MAX(version)+1 allocation. It yields
Prompt_Status=VALIDATED, never APPROVED, and does not write the live sheet.

## Boundary

The future Provider Adapter receives an approved prompt identity, compiled
prompt, structured settings, and structured reference descriptors. It does not
resolve Story/Master relationships or compile prompt text. Phase E stops
before explicit prompt approval, Scene Job creation, routing to an executable
adapter, and all provider calls.
