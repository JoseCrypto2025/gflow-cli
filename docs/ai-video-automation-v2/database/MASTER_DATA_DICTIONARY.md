# Master Data Dictionary

Schema baseline: AI Video Automation Master Foundation V2 plus Phase B. The spreadsheet has 23 Master, 3 transaction, and 3 relation tabs. Field definitions are JSON under schemas/master, schemas/transaction, and schemas/relation. No fake production rows or unverified cost rows are included. Spreadsheet timezone is Asia/Bangkok; canonical timestamps remain ISO 8601 UTC.

## Table index

| Table | Type | Primary Key |
|---|---|---|
| UNIVERSE_MASTER | master | Universe_ID |
| ERA_MASTER | master | Era_ID |
| SERIES_MASTER | master | Series_ID |
| CHARACTER_MASTER | master | Character_ID |
| CHARACTER_RELATIONSHIP | master | Relationship_ID |
| LOCATION_MASTER | master | Location_ID |
| WARDROBE_MASTER | master | Wardrobe_ID |
| PROP_MASTER | master | Prop_ID |
| REFERENCE_ASSET_MASTER | master | Reference_Asset_ID |
| REFERENCE_ENTITY_MAP | master | Reference_Entity_Map_ID |
| REFERENCE_PROVIDER_MAP | master | Reference_Provider_Map_ID |
| VISUAL_STYLE_MASTER | master | Visual_Style_ID |
| CAMERA_PROFILE_MASTER | master | Camera_Profile_ID |
| LIGHTING_MASTER | master | Lighting_ID |
| VOICE_MASTER | master | Voice_ID |
| PROMPT_TEMPLATE_MASTER | master | Prompt_Template_ID |
| PROVIDER_MASTER | master | Provider_ID |
| MODEL_MASTER | master | Model_ID |
| MODEL_CAPABILITY_MAP | master | Model_Capability_ID |
| PROVIDER_ACCOUNT_MASTER | master | Provider_Account_ID |
| MODEL_PARAMETER_MAP | master | Model_Parameter_ID |
| GENERATION_PROFILE_MASTER | master | Generation_Profile_ID |
| PROVIDER_COST_MASTER | master | Provider_Cost_ID |
| EPISODE_MASTER | transaction | Episode_ID |
| SCRIPT_MASTER | transaction | Script_ID |
| SCENES | transaction | Scene_ID |
| SCENE_CHARACTER_MAP | relation | Scene_Character_ID |
| SCENE_DIALOGUE_LINES | relation | Dialogue_Line_ID |
| SCENE_REFERENCE_MAP | relation | Scene_Reference_ID |

## Table definitions

### UNIVERSE_MASTER

Reusable story world / universe.

Primary key: Universe_ID. Fields: Universe_ID (STRING, required), Universe_Name (STRING, required), Universe_Description (STRING), World_Rules (STRING), Default_Era_ID (STRING, ACTIVE FK to ERA_MASTER.Era_ID), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### ERA_MASTER

Historical and visual context shared by reusable entities.

Primary key: Era_ID. Fields: Era_ID (STRING, required), Era_Name (STRING, required), Era_Description (STRING), Country_Context (STRING), Start_Period (STRING), End_Period (STRING), Visual_Context (STRING), Wardrobe_Context (STRING), Architecture_Context (STRING), Negative_Constraints (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### SERIES_MASTER

Reusable series-level configuration.

Primary key: Series_ID. Fields: Series_ID (STRING, required), Series_Name (STRING, required), Universe_ID (STRING, ACTIVE FK to UNIVERSE_MASTER.Universe_ID), Genre (STRING), Story_Tone (STRING), Era_ID (STRING, ACTIVE FK to ERA_MASTER.Era_ID), Default_Visual_Style_ID (STRING, ACTIVE FK to VISUAL_STYLE_MASTER.Visual_Style_ID), Default_Camera_Profile_ID (STRING, ACTIVE FK to CAMERA_PROFILE_MASTER.Camera_Profile_ID), Default_Aspect_Ratio (STRING), Default_Scene_Duration_Sec (DECIMAL), Target_Duration_Min (DECIMAL), Primary_Language (STRING), Series_Status (STRING), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### CHARACTER_MASTER

Reusable character identity, personality, speech, and continuity rules.

Primary key: Character_ID. Fields: Character_ID (STRING, required), Series_ID (STRING, ACTIVE FK to SERIES_MASTER.Series_ID), Character_Name (STRING, required), Character_Alias (STRING), Character_Role (STRING), Gender_Profile (STRING), Age_Range (STRING), Nationality_Style (STRING), Face_Description (STRING), Hair_Description (STRING), Body_Description (STRING), Skin_Tone (STRING), Distinctive_Features (STRING), Personality (STRING), Behavior_Profile (STRING), Speaking_Style (STRING), Default_Wardrobe_ID (STRING, ACTIVE FK to WARDROBE_MASTER.Wardrobe_ID), Default_Voice_ID (STRING, ACTIVE FK to VOICE_MASTER.Voice_ID), Prompt_Identity_Text (STRING), Continuity_Rules (STRING), Negative_Constraints (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### CHARACTER_RELATIONSHIP

Normalized relationship between two characters; episode references are deferred.

Primary key: Relationship_ID. Fields: Relationship_ID (STRING, required), Series_ID (STRING, ACTIVE FK to SERIES_MASTER.Series_ID), Character_ID_A (STRING, ACTIVE FK to CHARACTER_MASTER.Character_ID), Character_ID_B (STRING, ACTIVE FK to CHARACTER_MASTER.Character_ID), Relationship_Type (STRING, required), Relationship_Description (STRING), Current_State (STRING), Start_Episode_ID (STRING, DEFERRED FK to EPISODE_MASTER.Episode_ID), End_Episode_ID (STRING, DEFERRED FK to EPISODE_MASTER.Episode_ID), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### LOCATION_MASTER

Reusable locations and visual continuity.

Primary key: Location_ID. Fields: Location_ID (STRING, required), Series_ID (STRING, ACTIVE FK to SERIES_MASTER.Series_ID), Location_Name (STRING, required), Location_Type (STRING), Era_ID (STRING, ACTIVE FK to ERA_MASTER.Era_ID), Location_Description (STRING), Architecture_Description (STRING), Environment_Description (STRING), Interior_Description (STRING), Exterior_Description (STRING), Default_Time_Of_Day (STRING), Default_Lighting_ID (STRING, ACTIVE FK to LIGHTING_MASTER.Lighting_ID), Default_Camera_Profile_ID (STRING, ACTIVE FK to CAMERA_PROFILE_MASTER.Camera_Profile_ID), Prompt_Location_Text (STRING), Continuity_Rules (STRING), Negative_Constraints (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### WARDROBE_MASTER

Reusable character wardrobe definitions.

Primary key: Wardrobe_ID. Fields: Wardrobe_ID (STRING, required), Character_ID (STRING, ACTIVE FK to CHARACTER_MASTER.Character_ID), Wardrobe_Name (STRING, required), Wardrobe_Type (STRING), Description (STRING), Top_Description (STRING), Bottom_Description (STRING), Accessory_Description (STRING), Footwear_Description (STRING), Primary_Color (STRING), Secondary_Color (STRING), Material_Texture (STRING), Era_ID (STRING, ACTIVE FK to ERA_MASTER.Era_ID), Usage_Context (STRING), Prompt_Wardrobe_Text (STRING), Continuity_Rules (STRING), Negative_Constraints (STRING), Is_Default (BOOLEAN), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### PROP_MASTER

Reusable story props and continuity rules.

Primary key: Prop_ID. Fields: Prop_ID (STRING, required), Series_ID (STRING, ACTIVE FK to SERIES_MASTER.Series_ID), Prop_Name (STRING, required), Prop_Type (STRING), Prop_Description (STRING), Owner_Character_ID (STRING, ACTIVE FK to CHARACTER_MASTER.Character_ID), Era_ID (STRING, ACTIVE FK to ERA_MASTER.Era_ID), Continuity_Required (BOOLEAN), Prompt_Prop_Text (STRING), Negative_Constraints (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### REFERENCE_ASSET_MASTER

Canonical registry for reusable reference files; Google Drive owns the actual file.

Primary key: Reference_Asset_ID. Fields: Reference_Asset_ID (STRING, required), Reference_Type (STRING), Asset_Name (STRING, required), Asset_Description (STRING), Drive_File_ID (STRING), Drive_URL (URL), Mime_Type (STRING), Width (INTEGER), Height (INTEGER), Aspect_Ratio (STRING), Version (STRING), Reference_Quality_Status (STRING), Background_Type (STRING), Checksum (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### REFERENCE_ENTITY_MAP

Connect canonical reference assets to reusable entities. Entity_ID is polymorphic.

Primary key: Reference_Entity_Map_ID. Fields: Reference_Entity_Map_ID (STRING, required), Reference_Asset_ID (STRING, ACTIVE FK to REFERENCE_ASSET_MASTER.Reference_Asset_ID), Entity_Type (STRING, required), Entity_ID (STRING, required), Reference_Role (STRING, required), Is_Primary (BOOLEAN), Priority (INTEGER), Usage_Instruction_Default (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### REFERENCE_PROVIDER_MAP

Track provider-specific synchronization separately from canonical assets.

Primary key: Reference_Provider_Map_ID. Fields: Reference_Provider_Map_ID (STRING, required), Reference_Asset_ID (STRING, ACTIVE FK to REFERENCE_ASSET_MASTER.Reference_Asset_ID), Provider_ID (STRING, ACTIVE FK to PROVIDER_MASTER.Provider_ID), Provider_Project_ID (STRING), Provider_Asset_ID (STRING), Provider_Asset_Name (STRING), Sync_Status (STRING), Last_Synced_At (TIMESTAMP), Sync_Error (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### VISUAL_STYLE_MASTER

Reusable visual style definitions.

Primary key: Visual_Style_ID. Fields: Visual_Style_ID (STRING, required), Style_Name (STRING, required), Style_Description (STRING), Realism_Level (STRING), Color_Profile (STRING), Default_Lighting_ID (STRING, ACTIVE FK to LIGHTING_MASTER.Lighting_ID), Texture_Profile (STRING), Cinematic_Keywords (STRING), Negative_Style (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### CAMERA_PROFILE_MASTER

Reusable camera language profiles.

Primary key: Camera_Profile_ID. Fields: Camera_Profile_ID (STRING, required), Profile_Name (STRING, required), Default_Shot_Type (STRING), Default_Angle (STRING), Default_Movement (STRING), Default_Lens (STRING), Composition_Rule (STRING), Depth_Of_Field (STRING), Prompt_Camera_Text (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### LIGHTING_MASTER

Reusable lighting definitions.

Primary key: Lighting_ID. Fields: Lighting_ID (STRING, required), Lighting_Name (STRING, required), Lighting_Description (STRING), Time_Of_Day (STRING), Mood (STRING), Prompt_Lighting_Text (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### VOICE_MASTER

Reusable voice definition; contains no API keys.

Primary key: Voice_ID. Fields: Voice_ID (STRING, required), Provider_ID (STRING, ACTIVE FK to PROVIDER_MASTER.Provider_ID), Provider_Voice_ID (STRING), Voice_Name (STRING, required), Language (STRING), Gender_Profile (STRING), Voice_Description (STRING), Speaking_Rate (DECIMAL), Stability (DECIMAL), Similarity (DECIMAL), Style (DECIMAL), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### PROMPT_TEMPLATE_MASTER

Versioned prompt templates; technical generation settings remain structured.

Primary key: Prompt_Template_ID. Fields: Prompt_Template_ID (STRING, required), Template_Name (STRING, required), Provider_ID (STRING, ACTIVE FK to PROVIDER_MASTER.Provider_ID), Model_ID (STRING, ACTIVE FK to MODEL_MASTER.Model_ID), Content_Type (STRING), Generation_Mode (STRING), Template_Version (STRING), System_Prompt (STRING), Prompt_Template (STRING), Negative_Template (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### PROVIDER_MASTER

Verified or explicitly intended generation providers; never store secrets.

Primary key: Provider_ID. Fields: Provider_ID (STRING, required), Provider_Name (STRING, required), Provider_Type (STRING), Execution_Mode (STRING), Billing_Mode (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### MODEL_MASTER

Provider model catalog. Capabilities belong in MODEL_CAPABILITY_MAP.

Primary key: Model_ID. Fields: Model_ID (STRING, required), Provider_ID (STRING, ACTIVE FK to PROVIDER_MASTER.Provider_ID), Model_Name (STRING, required), Model_Version (STRING), Generation_Type (STRING), Default_Duration_Sec (DECIMAL), Default_Aspect_Ratio (STRING), Default_Resolution (STRING), Status (STRING, required), Capability_Source (URL), Capabilities_Last_Verified_At (TIMESTAMP), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### MODEL_CAPABILITY_MAP

Extensible verified model capabilities; mode-specific combinations are serialized to preserve relationships.

Primary key: Model_Capability_ID. Fields: Model_Capability_ID (STRING, required), Model_ID (STRING, ACTIVE FK to MODEL_MASTER.Model_ID), Capability_Type (STRING, required), Capability_Value (STRING, required), Unit (STRING), Status (STRING, required), Source (URL), Verified_At (TIMESTAMP), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).
### SCENE_REFERENCE_MAP

Scene-to-reference relation. Scene_ID and Reference_Asset_ID are active foreign keys; no scene records are created.

Primary key: Scene_Reference_ID. Fields: Scene_Reference_ID (STRING, required), Scene_ID (STRING, DEFERRED FK to SCENES.Scene_ID), Reference_Asset_ID (STRING, ACTIVE FK to REFERENCE_ASSET_MASTER.Reference_Asset_ID), Reference_Role (STRING), Usage_Instruction (STRING), Sort_Order (INTEGER), Is_Required (BOOLEAN), Is_Primary (BOOLEAN), Prompt_Mention_Name (STRING), Validation_Status (STRING), Validation_Message (STRING), Status (STRING, required), Created_At (TIMESTAMP), Updated_At (TIMESTAMP).

## Transaction and scene relation tables (Phase B)

### EPISODE_MASTER

One creative episode per row. Primary key Episode_ID (`EPI_000001`); Series_ID references SERIES_MASTER; optional Previous_Episode_ID references EPISODE_MASTER. Series_ID + Episode_No is unique. Editorial status is DRAFT, PLANNED, SCRIPTING, SCRIPT_APPROVED, SCENE_PLANNING, READY_FOR_PRODUCTION, HOLD, or ARCHIVED. Fields: Episode_ID, Series_ID, Episode_No, Episode_Title, Episode_Synopsis, Episode_Objective, Opening_Hook, Main_Conflict, Ending_Hook, Story_Arc_Summary, Previous_Episode_ID, Continuity_From_Previous, Target_Duration_Sec, Primary_Language, Editorial_Priority, Episode_Status, Approved_At, Created_At, Updated_At.

### SCRIPT_MASTER

Versioned episode script. Primary key Script_ID (`SCR_000001`); Episode_ID references EPISODE_MASTER; optional Supersedes_Script_ID references SCRIPT_MASTER. Episode_ID + Script_Version is unique; at most one APPROVED script per episode. Fields: Script_ID, Episode_ID, Script_Version, Script_Title, Full_Script, Narrative_Direction, Estimated_Duration_Sec, Dialogue_Language, Generation_Source, Generation_Prompt_Version, Script_Checksum_SHA256, Supersedes_Script_ID, Script_Status, Review_Notes, Approved_At, Created_At, Updated_At.

### SCENES

One creative visual shot per row; excludes provider, job, and generated-asset state. Primary key Scene_ID (`SCN_000001`); Script_ID references SCRIPT_MASTER; Script_ID + Scene_No is unique. Fields: Scene_ID, Script_ID, Scene_No, Narrative_Beat_No, Scene_Title, Scene_Type, Scene_Purpose, Scene_Summary, Action_Description, Location_ID, Environment_Override, Time_Of_Day_Override, Scene_Mood, Visual_Style_ID, Camera_Profile_ID, Lighting_ID, Duration_Target_Sec, Aspect_Ratio_Override, Dialogue_Mode, Audio_Direction, Continuity_Source_Scene_ID, Scene_Start_State, Scene_End_State, Continuity_Notes, Negative_Constraints_Override, Scene_Definition_Status, Created_At, Updated_At.

### SCENE_CHARACTER_MAP

Links scene participants to CHARACTER_MASTER and optional WARDROBE_MASTER. Primary key Scene_Character_ID (`SCM_000001`); Scene_ID + Character_ID is unique. Fields: Scene_Character_ID, Scene_ID, Character_ID, Wardrobe_ID, Character_Role_In_Scene, Action_Instruction, Emotion_Instruction, Position_Instruction, Appearance_Override, Continuity_Override, Sort_Order, Status, Created_At, Updated_At.

### SCENE_DIALOGUE_LINES

Ordered dialogue or narration. Primary key Dialogue_Line_ID (`SDL_000001`); Scene_ID + Line_No is unique. Character_ID is required for CHARACTER speakers and blank for NARRATOR/SYSTEM. Fields: Dialogue_Line_ID, Scene_ID, Line_No, Speaker_Type, Character_ID, Dialogue_Text, Emotion, Speaking_Style_Override, Voice_ID_Override, Pause_After_Ms, Status, Created_At, Updated_At.

## Multi-provider tables (V2.1)

### PROVIDER_ACCOUNT_MASTER

Concrete provider project, workspace, or runtime identity. Auth_Profile_Key is only a logical secret reference such as GFLOW_DEV_AUTH; credentials never belong in Sheets.

Primary key: Provider_Account_ID (PVA_0001). Provider_ID references PROVIDER_MASTER.Provider_ID. Environment: DEV, TEST, PROD. Is_Default is a checkbox; at most one ACTIVE default is allowed per Provider_ID + Environment. Execution_Target may be WEB_UI, API, CLI, or MCP. Fields: Provider_Account_ID, Provider_ID, Account_Name, Environment, Provider_Project_ID, Provider_Workspace_ID, Region, Execution_Target, Auth_Profile_Key, Base_URL, Is_Default, Status, Created_At, Updated_At.

### MODEL_PARAMETER_MAP

Maps canonical automation parameters to semantic adapter keys, keeping scenes and orchestration independent of provider naming. Browser selectors, XPath, and coordinates belong in Worker/adapter code, never this table.

Primary key: Model_Parameter_ID (MPM_000001). Model_ID references MODEL_MASTER.Model_ID. Active rows are unique by Model_ID + Generation_Mode + Canonical_Parameter. Allowed_Values_JSON must be a JSON array when populated; Default_Value_JSON must be valid JSON. Fields: Model_Parameter_ID, Model_ID, Generation_Mode, Canonical_Parameter, Adapter_Parameter_Key, Parameter_Data_Type, Allowed_Values_JSON, Default_Value_JSON, Is_Required, Unit, Mapping_Notes, Source, Verified_At, Status, Created_At, Updated_At.

### GENERATION_PROFILE_MASTER

Reusable provider-neutral preset for future jobs/scenes. Future scene data should refer to Generation_Profile_ID rather than provider-specific model or project fields.

Primary key: Generation_Profile_ID (GPR_0001). Provider_Account_ID, Model_ID, and optional Prompt_Template_ID are foreign keys. The model provider must match the selected provider account; the profile environment must match the account environment. Generation_Mode must be supported in MODEL_CAPABILITY_MAP. Populated duration, aspect ratio, and resolution must be explicitly supported by active model capability data. Active defaults are unique by Purpose + Environment. Fields: Generation_Profile_ID, Profile_Name, Profile_Version, Purpose, Environment, Provider_Account_ID, Model_ID, Generation_Mode, Prompt_Template_ID, Default_Aspect_Ratio, Default_Duration_Sec, Default_Resolution, Default_Output_Count, Audio_Mode, Quality_Tier, Is_Default, Status, Created_At, Updated_At.

### PROVIDER_COST_MASTER

Effective-dated provider, account, or model pricing. No rows are added until rates are verified. Rate changes create new rows; historical values are retained.

Primary key: Provider_Cost_ID (PCT_000001). Provider_ID is required; Provider_Account_ID and Model_ID are optional foreign keys. Each cost row requires Cost_Rate, Effective_From, Source, and Verified_At. Currency is an uppercase three-letter currency code and is required except for CREDIT units. Effective_To, when present, must be later than Effective_From. Optional exact quote-scope fields are Generation_Profile_ID, Duration_Sec, Aspect_Ratio, Audio_Mode, and Output_Count. The legacy Standard Lite quote matches the exact active account, model, NON_ULTRA billing class, and TEXT_TO_VIDEO mode. Duration, aspect, audio, output count, quality tier, and resolution are not universal price keys for this official per-generation rate; output count multiplies the rate. Duplicate, stale, future-effective, non-CREDIT, or non-official rows cannot supply a quote. Fields: Provider_Cost_ID, Provider_ID, Provider_Account_ID, Model_ID, Billing_Plan_Name, Generation_Mode, Quality_Tier, Resolution, Cost_Unit, Cost_Rate, Currency, Effective_From, Effective_To, Source, Verified_At, Status, Created_At, Updated_At, Generation_Profile_ID, Duration_Sec, Aspect_Ratio, Audio_Mode, Output_Count.

## Provider-neutral generation rules

- Provider-specific account/project resolution belongs in PROVIDER_ACCOUNT_MASTER and provider adapters.
- MODEL_PARAMETER_MAP translates canonical parameters to semantic adapter keys; UI selectors are not database values.
- GENERATION_PROFILE_MASTER binds an account, same-provider model, supported mode, and optional prompt template.
- MODEL_CAPABILITY_MAP remains authoritative for supported modes and settings; profiles cannot override it.
- PROVIDER_COST_MASTER contains source-verified effective-dated rates only. Never hard-code prices into Worker/n8n behavior.
- Google Sheets displays dates in Asia/Bangkok. Database timestamps remain ISO 8601 UTC, for example 2026-10-15T14:01:04Z.

## Shared rules

- Stable ID text keys are generated by application or n8n logic, never spreadsheet formulas.
- Timestamps use ISO 8601 UTC. Boolean cells use native TRUE/FALSE values.
- Active foreign keys are documented and validated by scripts; Sheets does not provide SQL referential integrity.
- Deferred foreign keys remain blank until the referenced future table exists.
- REFERENCE_ENTITY_MAP.Entity_ID is polymorphic. Resolve it through Entity_Type. For an active Entity_Type + Entity_ID + Reference_Role, allow at most one Is_Primary=TRUE row.
- Google Drive owns actual reference files. Provider mappings are separate.
- Prompt_* values are curated canonical fragments, not automatic copies of raw descriptions.
- No credentials, tokens, passwords, API keys, or private keys belong in the database.
- The direct-credit quote resolver is internal to the ambiguous legacy replacement path. It never infers cost from historical usage; when no exact source-verified effective row exists, quote status is `QUOTE_UNAVAILABLE` and replacement decision remains HOLD.

