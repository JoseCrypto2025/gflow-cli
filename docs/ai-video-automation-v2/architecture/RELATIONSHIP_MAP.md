# Relationship Map

Active foreign keys require application/script validation. Deferred foreign keys remain unresolved until later-phase tables exist.

## Active relationships

- UNIVERSE_MASTER: Default_Era_ID to ERA_MASTER.Era_ID
- SERIES_MASTER: Universe_ID to UNIVERSE_MASTER.Universe_ID; Era_ID to ERA_MASTER.Era_ID; Default_Visual_Style_ID to VISUAL_STYLE_MASTER.Visual_Style_ID; Default_Camera_Profile_ID to CAMERA_PROFILE_MASTER.Camera_Profile_ID
- CHARACTER_MASTER: Series_ID to SERIES_MASTER.Series_ID; Default_Wardrobe_ID to WARDROBE_MASTER.Wardrobe_ID; Default_Voice_ID to VOICE_MASTER.Voice_ID
- CHARACTER_RELATIONSHIP: Series_ID to SERIES_MASTER.Series_ID; Character_ID_A to CHARACTER_MASTER.Character_ID; Character_ID_B to CHARACTER_MASTER.Character_ID
- LOCATION_MASTER: Series_ID to SERIES_MASTER.Series_ID; Era_ID to ERA_MASTER.Era_ID; Default_Lighting_ID to LIGHTING_MASTER.Lighting_ID; Default_Camera_Profile_ID to CAMERA_PROFILE_MASTER.Camera_Profile_ID
- WARDROBE_MASTER: Character_ID to CHARACTER_MASTER.Character_ID; Era_ID to ERA_MASTER.Era_ID
- PROP_MASTER: Series_ID to SERIES_MASTER.Series_ID; Owner_Character_ID to CHARACTER_MASTER.Character_ID; Era_ID to ERA_MASTER.Era_ID
- REFERENCE_ENTITY_MAP: Reference_Asset_ID to REFERENCE_ASSET_MASTER.Reference_Asset_ID
- REFERENCE_PROVIDER_MAP: Reference_Asset_ID to REFERENCE_ASSET_MASTER.Reference_Asset_ID; Provider_ID to PROVIDER_MASTER.Provider_ID
- VISUAL_STYLE_MASTER: Default_Lighting_ID to LIGHTING_MASTER.Lighting_ID
- VOICE_MASTER: Provider_ID to PROVIDER_MASTER.Provider_ID
- PROMPT_TEMPLATE_MASTER: Provider_ID to PROVIDER_MASTER.Provider_ID; Model_ID to MODEL_MASTER.Model_ID
- MODEL_MASTER: Provider_ID to PROVIDER_MASTER.Provider_ID
- MODEL_CAPABILITY_MAP: Model_ID to MODEL_MASTER.Model_ID
- PROVIDER_ACCOUNT_MASTER: Provider_ID to PROVIDER_MASTER.Provider_ID
- MODEL_PARAMETER_MAP: Model_ID to MODEL_MASTER.Model_ID
- GENERATION_PROFILE_MASTER: Provider_Account_ID to PROVIDER_ACCOUNT_MASTER.Provider_Account_ID; Model_ID to MODEL_MASTER.Model_ID; Prompt_Template_ID to PROMPT_TEMPLATE_MASTER.Prompt_Template_ID
- PROVIDER_COST_MASTER: Provider_ID to PROVIDER_MASTER.Provider_ID; optional Provider_Account_ID to PROVIDER_ACCOUNT_MASTER.Provider_Account_ID; optional Model_ID to MODEL_MASTER.Model_ID
- SCENE_REFERENCE_MAP: Reference_Asset_ID to REFERENCE_ASSET_MASTER.Reference_Asset_ID

## Deferred relationships

- CHARACTER_RELATIONSHIP.Start_Episode_ID and End_Episode_ID to EPISODE_MASTER.Episode_ID.
- SCENE_REFERENCE_MAP.Scene_ID to SCENES.Scene_ID. No episode or scene rows are created.

## Polymorphic relationship

- REFERENCE_ENTITY_MAP.Entity_Type selects CHARACTER_MASTER, WARDROBE_MASTER, LOCATION_MASTER, PROP_MASTER, or VISUAL_STYLE_MASTER. Entity_ID must exist in that selected table. This is not a conventional single-table FK.

## Normalized references

- REFERENCE_ASSET_MASTER stores canonical Drive metadata.
- REFERENCE_ENTITY_MAP links assets to reusable entities.
- REFERENCE_PROVIDER_MAP records provider project/asset synchronization.
- Reference IDs are not duplicated on CHARACTER, LOCATION, WARDROBE, or PROP rows.


## Multi-provider resolution (V2.1)

PROVIDER_MASTER has one-to-many accounts and models. A provider account identifies an environment/project/workspace without secret material. MODEL_PARAMETER_MAP maps canonical automation parameters to semantic adapter keys. GENERATION_PROFILE_MASTER binds one account, a model from the same provider, a supported mode, and an optional prompt template. Future scenes/jobs reference the profile instead of embedding provider, model, project, or UI-specific fields.

PROVIDER_COST_MASTER records source-verified effective-dated rates at provider, account, or model level. Retain historical rows when pricing changes. Auth_Profile_Key contains only a logical secret reference.
