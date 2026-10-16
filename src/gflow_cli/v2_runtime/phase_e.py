"""Deterministic, provider-neutral prompt compilation and data preflight.

This module consumes a read-only catalog snapshot. It deliberately has no
network, filesystem, browser, or provider-execution dependencies.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, TypeAlias, cast

from .assets import AssetReference
from .catalog import (
    AccountRecord,
    CapabilityRecord,
    ModelRecord,
    ProfileRecord,
    PromptTemplateRecord,
    ProviderRecord,
    resolve_profile,
)

Row: TypeAlias = Mapping[str, object]

TABLE_KEYS: dict[str, str] = {
    "SCENES": "Scene_ID",
    "SCRIPT_MASTER": "Script_ID",
    "EPISODE_MASTER": "Episode_ID",
    "SERIES_MASTER": "Series_ID",
    "SCENE_CHARACTER_MAP": "Scene_Character_ID",
    "SCENE_DIALOGUE_LINES": "Dialogue_Line_ID",
    "SCENE_REFERENCE_MAP": "Scene_Reference_ID",
    "CHARACTER_MASTER": "Character_ID",
    "WARDROBE_MASTER": "Wardrobe_ID",
    "LOCATION_MASTER": "Location_ID",
    "VISUAL_STYLE_MASTER": "Visual_Style_ID",
    "CAMERA_PROFILE_MASTER": "Camera_Profile_ID",
    "LIGHTING_MASTER": "Lighting_ID",
    "ERA_MASTER": "Era_ID",
    "REFERENCE_ASSET_MASTER": "Reference_Asset_ID",
    "REFERENCE_ENTITY_MAP": "Reference_Entity_Map_ID",
    "REFERENCE_PROVIDER_MAP": "Reference_Provider_Map_ID",
    "GENERATION_PROFILE_MASTER": "Generation_Profile_ID",
    "PROVIDER_ACCOUNT_MASTER": "Provider_Account_ID",
    "PROVIDER_MASTER": "Provider_ID",
    "MODEL_MASTER": "Model_ID",
    "MODEL_CAPABILITY_MAP": "Model_Capability_ID",
    "MODEL_PARAMETER_MAP": "Model_Parameter_ID",
    "PROMPT_TEMPLATE_MASTER": "Prompt_Template_ID",
}

PLACEHOLDERS = frozenset(
    {
        "SUBJECT",
        "REFERENCE_USAGE",
        "ACTION",
        "ENVIRONMENT",
        "EMOTION",
        "CAMERA",
        "LIGHTING",
        "VISUAL_STYLE",
        "CONTINUITY_RULES",
        "NEGATIVE_CONSTRAINTS",
    }
)
_PLACEHOLDER_RE = re.compile(r"\{([A-Z][A-Z0-9_]*)\}")
_VOLATILE_KEYS = frozenset(
    {"Created_At", "Updated_At", "Verified_At", "Capabilities_Last_Verified_At"}
)
_MODE_ROLES: dict[str, tuple[str, ...]] = {
    "TEXT_TO_VIDEO": (),
    "FIRST_FRAME_TO_VIDEO": ("START_FRAME",),
    "FIRST_LAST_FRAME_TO_VIDEO": ("START_FRAME", "END_FRAME"),
}


class SceneCatalog(Protocol):
    """Extension of the Runtime Foundation catalog for normalized table rows."""

    def rows(self, table_name: str) -> list[Row]: ...

    def provider(self, provider_id: str) -> ProviderRecord | None: ...
    def account(self, account_id: str) -> AccountRecord | None: ...
    def model(self, model_id: str) -> ModelRecord | None: ...
    def profile(self, profile_id: str) -> ProfileRecord | None: ...
    def prompt_template(self, prompt_template_id: str) -> PromptTemplateRecord | None: ...
    def capabilities(self, model_id: str) -> list[CapabilityRecord]: ...


@dataclass(frozen=True, slots=True, order=True)
class PreflightIssue:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ReferenceDescriptor:
    reference_asset_id: str
    reference_role: str
    drive_file_id: str
    version: str | None = None
    checksum: str | None = None
    usage_instruction: str | None = None
    prompt_mention_name: str | None = None
    is_primary: bool = False
    provider_sync_status: str | None = None

    def asset_reference(self) -> AssetReference:
        """Adapt the verified remote identity to the shared Runtime asset type."""
        return AssetReference(
            storage_provider="google-drive",
            asset_id=self.reference_asset_id,
            logical_key=f"references/{self.reference_asset_id}",
            remote_file_id=self.drive_file_id,
            checksum_sha256=self.checksum,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "reference_asset_id": self.reference_asset_id,
            "reference_role": self.reference_role,
            "drive_file_id": self.drive_file_id,
            "version": self.version,
            "checksum": self.checksum,
            "usage_instruction": self.usage_instruction,
            "prompt_mention_name": self.prompt_mention_name,
            "is_primary": self.is_primary,
            "provider_sync_status": self.provider_sync_status,
        }


@dataclass(frozen=True, slots=True)
class PreflightResult:
    status: str
    ready_for_provider_adapter: bool
    scene_id: str
    generation_profile_id: str
    resolved: Mapping[str, object] = field(default_factory=lambda: cast(dict[str, object], {}))
    technical_configuration: Mapping[str, object] = field(
        default_factory=lambda: cast(dict[str, object], {})
    )
    compiled_prompt: str | None = None
    compiled_negative_prompt: str | None = None
    source_context_sha256: str | None = None
    prompt_sha256: str | None = None
    references: tuple[ReferenceDescriptor, ...] = ()
    errors: tuple[PreflightIssue, ...] = ()
    warnings: tuple[PreflightIssue, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "ready_for_provider_adapter": self.ready_for_provider_adapter,
            "scene_id": self.scene_id,
            "generation_profile_id": self.generation_profile_id,
            "resolved": dict(self.resolved),
            "technical_configuration": dict(self.technical_configuration),
            "compiled": {
                "prompt": self.compiled_prompt,
                "negative_prompt": self.compiled_negative_prompt,
            },
            "hashes": {
                "source_context_sha256": self.source_context_sha256,
                "prompt_sha256": self.prompt_sha256,
            },
            "references": [ref.as_dict() for ref in self.references],
            "errors": [{"code": issue.code, "message": issue.message} for issue in self.errors],
            "warnings": [{"code": issue.code, "message": issue.message} for issue in self.warnings],
        }


def canonical_json(value: object) -> str:
    """Canonical UTF-8-ready JSON: stable keys, compact separators, no NaN."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _clean_row(row: Row) -> dict[str, object]:
    return {
        str(key): value
        for key, value in row.items()
        if key not in _VOLATILE_KEYS and not str(key).endswith("_At")
    }


def _semantic_config_row(row: Row) -> dict[str, object]:
    """Normalize JSON text columns before they contribute to source identity."""
    result = _clean_row(row)
    for key in ("Capability_Value", "Allowed_Values_JSON", "Default_Value_JSON"):
        value = result.get(key)
        if isinstance(value, str) and value.strip():
            try:
                result[key] = cast(object, json.loads(value))
            except json.JSONDecodeError:
                # Malformed configuration is separately surfaced as HOLD.
                result[key] = value
    return result


def _text(row: Row | None, key: str) -> str:
    if row is None:
        return ""
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _bool(value: object) -> bool:
    return value is True or str(value).strip().upper() == "TRUE"


def _active(row: Row | None) -> bool:
    return row is not None and _text(row, "Status").upper() == "ACTIVE"


def _num(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    if str(value).strip() == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _index(catalog: SceneCatalog, table: str, key: str | None = None) -> dict[str, Row]:
    primary = key or TABLE_KEYS[table]
    return {_text(row, primary): row for row in catalog.rows(table) if _text(row, primary)}


def _get(catalog: SceneCatalog, table: str, key: str | None) -> Row | None:
    if not key:
        return None
    return _index(catalog, table).get(key)


def _resolve_profile(catalog: SceneCatalog, profile_id: str, environment: str):
    """Delegate typed routing/capability checks to the Phase C resolver."""
    return resolve_profile(catalog, profile_id, environment, allow_unenumerable_capabilities=True)


def _capability_values(
    catalog: SceneCatalog, model_id: str, mode: str, kind: str
) -> tuple[set[str], bool]:
    """Read explicit capability values, including structured mode bundles."""
    found: set[str] = set()
    enumerable = False
    for row in catalog.rows("MODEL_CAPABILITY_MAP"):
        if _text(row, "Model_ID") != model_id or _text(row, "Status").upper() != "ACTIVE":
            continue
        cap_type = _text(row, "Capability_Type").upper()
        raw = _text(row, "Capability_Value")
        try:
            parsed = cast(object, json.loads(raw))
        except (json.JSONDecodeError, TypeError):
            parsed = raw
        if cap_type == kind:
            if kind == "GENERATION_MODE" and isinstance(parsed, dict):
                parsed_map = cast(Mapping[str, object], parsed)
                if (
                    parsed_map.get("supported") is not False
                    and str(parsed_map.get("mode", "")).upper() == mode
                ):
                    found.add(mode)
                    enumerable = True
            elif isinstance(parsed, list):
                enumerable = True
                found.update(str(v) for v in cast(list[object], parsed))
            elif isinstance(parsed, dict):
                parsed_map = cast(Mapping[str, object], parsed)
                target = parsed_map.get("values", parsed_map.get("supported_values"))
                if isinstance(target, list):
                    enumerable = True
                    found.update(str(v) for v in cast(list[object], target))
            elif isinstance(parsed, str) and parsed:
                found.add(parsed)
                enumerable = True
            elif isinstance(parsed, (int, float, bool)):
                found.add(str(parsed))
                enumerable = True
        if cap_type == "GENERATION_MODE" and isinstance(parsed, dict):
            parsed_map = cast(Mapping[str, object], parsed)
            bundled_mode = str(parsed_map.get("mode", "")).upper()
            if bundled_mode and bundled_mode != mode:
                continue
            if parsed_map.get("supported") is False:
                continue
            bundled_key = {
                "DURATION_SEC": "duration_sec",
                "ASPECT_RATIO": "aspect_ratios",
                "RESOLUTION": "resolutions",
                "OUTPUT_COUNT": "output_counts",
            }.get(kind)
            if bundled_key and bundled_key in parsed_map:
                value = parsed_map[bundled_key]
                if isinstance(value, list):
                    found.update(str(v) for v in cast(list[object], value))
                    enumerable = True
                elif isinstance(value, str) and value:
                    # Source prose is intentionally not treated as an allow-list.
                    if "not enumerated" in value.lower() or "both (official" in value.lower():
                        enumerable = False
                    else:
                        found.add(value)
                        enumerable = True
    return found, enumerable


def _validate_value(
    catalog: SceneCatalog,
    model_id: str,
    mode: str,
    kind: str,
    value: object,
    code: str,
    errors: list[PreflightIssue],
    warnings: list[PreflightIssue],
) -> None:
    if value is None or str(value).strip() == "":
        return
    allowed, enumerable = _capability_values(catalog, model_id, mode, kind)
    if not allowed:
        warnings.append(
            PreflightIssue(
                "CAPABILITY_NOT_MACHINE_ENUMERABLE",
                f"{kind} capability values are not machine-enumerable",
            )
        )
        errors.append(
            PreflightIssue(code, f"cannot safely validate {kind} against model capability data")
        )
        return
    normalized = str(value)
    if kind in {"DURATION_SEC", "OUTPUT_COUNT"}:
        parsed = _num(value)
        matches = parsed is not None and any(_num(item) == parsed for item in allowed)
    else:
        matches = normalized in allowed
    if not matches:
        errors.append(
            PreflightIssue(
                code, f"{kind} value {normalized!r} is not in the verified capability values"
            )
        )
    elif not enumerable:
        warnings.append(
            PreflightIssue(
                "CAPABILITY_NOT_MACHINE_ENUMERABLE", f"{kind} capability cannot be fully enumerated"
            )
        )


def _matches_parameter_type(value: object, data_type: str) -> bool:
    if data_type == "STRING":
        return isinstance(value, str)
    if data_type == "INTEGER":
        return isinstance(value, int) and not isinstance(value, bool)
    if data_type == "NUMBER":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )
    if data_type == "BOOLEAN":
        return isinstance(value, bool)
    if data_type == "ARRAY":
        return isinstance(value, list)
    if data_type == "OBJECT":
        return isinstance(value, dict)
    return False


def _validate_parameter_maps(
    catalog: SceneCatalog,
    model_id: str,
    mode: str,
    values: dict[str, object],
    errors: list[PreflightIssue],
) -> dict[str, object]:
    result: dict[str, object] = {}
    maps = [
        row
        for row in catalog.rows("MODEL_PARAMETER_MAP")
        if _text(row, "Model_ID") == model_id
        and _text(row, "Generation_Mode") == mode
        and _text(row, "Status").upper() == "ACTIVE"
    ]
    maps.sort(key=lambda row: (_text(row, "Canonical_Parameter"), _text(row, "Model_Parameter_ID")))
    for row in maps:
        canonical = _text(row, "Canonical_Parameter")
        key = _text(row, "Adapter_Parameter_Key")
        if not canonical or not key:
            continue
        value = values.get(canonical)
        try:
            allowed = (
                cast(object, json.loads(_text(row, "Allowed_Values_JSON")))
                if _text(row, "Allowed_Values_JSON")
                else None
            )
            default = (
                cast(object, json.loads(_text(row, "Default_Value_JSON")))
                if _text(row, "Default_Value_JSON")
                else None
            )
        except json.JSONDecodeError:
            errors.append(
                PreflightIssue(
                    "PARAMETER_VALUE_INVALID", f"{canonical} has malformed JSON configuration"
                )
            )
            continue
        if value is None:
            value = default
        if value is None and _bool(row.get("Is_Required")):
            errors.append(
                PreflightIssue(
                    "REQUIRED_PARAMETER_MAPPING_MISSING",
                    f"required canonical parameter {canonical} has no resolved value",
                )
            )
            continue
        if value is None:
            continue
        data_type = _text(row, "Parameter_Data_Type").upper()
        if not _matches_parameter_type(value, data_type):
            errors.append(
                PreflightIssue(
                    "PARAMETER_VALUE_INVALID",
                    f"{canonical} does not match parameter data type {data_type}",
                )
            )
            continue
        if allowed is not None and (
            not isinstance(allowed, list) or value not in cast(list[object], allowed)
        ):
            errors.append(
                PreflightIssue(
                    "PARAMETER_VALUE_INVALID", f"{canonical} violates its configured allowed values"
                )
            )
            continue
        result[key] = value
    by_canonical: dict[str, list[Row]] = {}
    for row in maps:
        by_canonical.setdefault(_text(row, "Canonical_Parameter"), []).append(row)
    for canonical, rows in by_canonical.items():
        if len(rows) > 1:
            errors.append(
                PreflightIssue(
                    "PARAMETER_VALUE_INVALID", f"multiple ACTIVE mappings exist for {canonical}"
                )
            )
    return result


def _scene_characters(catalog: SceneCatalog, scene_id: str) -> list[tuple[Row, Row, Row | None]]:
    mappings = [
        row
        for row in catalog.rows("SCENE_CHARACTER_MAP")
        if _text(row, "Scene_ID") == scene_id and _text(row, "Status").upper() == "ACTIVE"
    ]
    mappings.sort(
        key=lambda row: (
            _num(row.get("Sort_Order")) or 0,
            _text(row, "Scene_Character_ID"),
            _text(row, "Character_ID"),
        )
    )
    characters = _index(catalog, "CHARACTER_MASTER")
    wardrobes = _index(catalog, "WARDROBE_MASTER")
    resolved: list[tuple[Row, Row, Row | None]] = []
    for mapping in mappings:
        character = characters.get(_text(mapping, "Character_ID"))
        if character is None:
            continue
        wardrobe_id = _text(mapping, "Wardrobe_ID") or _text(character, "Default_Wardrobe_ID")
        resolved.append((mapping, character, wardrobes.get(wardrobe_id) if wardrobe_id else None))
    return resolved


def _select_entity_references(catalog: SceneCatalog, entities: list[tuple[str, str]]) -> list[Row]:
    result: list[Row] = []
    for entity_type, entity_id in entities:
        candidates = [
            row
            for row in catalog.rows("REFERENCE_ENTITY_MAP")
            if _text(row, "Entity_Type").upper() == entity_type
            and _text(row, "Entity_ID") == entity_id
            and _text(row, "Status").upper() == "ACTIVE"
        ]
        if not candidates:
            continue
        candidates.sort(
            key=lambda row: (
                0 if _bool(row.get("Is_Primary")) else 1,
                _num(row.get("Priority")) if _num(row.get("Priority")) is not None else math.inf,
                _text(row, "Reference_Asset_ID"),
                _text(row, "Reference_Entity_Map_ID"),
            )
        )
        best = candidates[0]
        best_rank = (
            0 if _bool(best.get("Is_Primary")) else 1,
            _num(best.get("Priority")) if _num(best.get("Priority")) is not None else math.inf,
        )
        tied = [
            row
            for row in candidates
            if (
                0 if _bool(row.get("Is_Primary")) else 1,
                _num(row.get("Priority")) if _num(row.get("Priority")) is not None else math.inf,
            )
            == best_rank
        ]
        if len({_text(row, "Reference_Asset_ID") for row in tied}) > 1:
            raise ValueError("REFERENCE_SELECTION_AMBIGUOUS")
        result.append(best)
    return result


def _continuity_cycle(catalog: SceneCatalog, scene_id: str) -> bool:
    """Detect a bounded pointer cycle without adding recursive context hops."""
    scenes = _index(catalog, "SCENES")
    seen: set[str] = set()
    current_id = scene_id
    for _ in range(len(scenes) + 1):
        if current_id in seen:
            return True
        seen.add(current_id)
        current = scenes.get(current_id)
        next_id = _text(current, "Continuity_Source_Scene_ID")
        if not next_id:
            return False
        current_id = next_id
    return True


def _resolve_references(
    catalog: SceneCatalog,
    scene: Row,
    mode: str,
    provider_id: str,
    entities: list[tuple[str, str]],
    require_usage_instruction: bool,
    errors: list[PreflightIssue],
) -> tuple[ReferenceDescriptor, ...]:
    scene_id = _text(scene, "Scene_ID")
    explicit = [
        row
        for row in catalog.rows("SCENE_REFERENCE_MAP")
        if _text(row, "Scene_ID") == scene_id and _text(row, "Status").upper() == "ACTIVE"
    ]
    explicit.sort(
        key=lambda row: (
            _num(row.get("Sort_Order")) if _num(row.get("Sort_Order")) is not None else math.inf,
            _text(row, "Scene_Reference_ID"),
            _text(row, "Reference_Asset_ID"),
        )
    )
    try:
        fallback = _select_entity_references(catalog, entities)
    except ValueError as exc:
        errors.append(
            PreflightIssue(
                str(exc), "equally eligible entity references cannot be selected uniquely"
            )
        )
        fallback = []
    explicit_roles = {_text(row, "Reference_Role").upper() for row in explicit}
    selections: list[tuple[Row, str, str, bool]] = []
    for row in explicit:
        selections.append(
            (
                row,
                _text(row, "Reference_Role").upper(),
                _text(row, "Usage_Instruction"),
                _bool(row.get("Is_Required")),
            )
        )
    for row in fallback:
        role = _text(row, "Reference_Role").upper()
        if role not in explicit_roles:
            selections.append((row, role, _text(row, "Usage_Instruction_Default"), False))
    assets = _index(catalog, "REFERENCE_ASSET_MASTER")
    provider_sync = [
        row
        for row in catalog.rows("REFERENCE_PROVIDER_MAP")
        if _text(row, "Provider_ID") == provider_id and _text(row, "Status").upper() == "ACTIVE"
    ]
    sync_by_asset = {
        _text(row, "Reference_Asset_ID"): _text(row, "Sync_Status") or _text(row, "Status")
        for row in provider_sync
    }
    refs: list[ReferenceDescriptor] = []
    roles = {role for _, role, _, _ in selections}
    required_roles = set(_MODE_ROLES.get(mode, ()))
    for required_role in sorted(required_roles):
        matches = [item for item in selections if item[1] == required_role]
        if len(matches) > 1:
            errors.append(
                PreflightIssue(
                    "REFERENCE_SELECTION_AMBIGUOUS",
                    f"generation mode requires exactly one {required_role} reference",
                )
            )
    if mode == "INGREDIENTS_TO_VIDEO" and not selections:
        errors.append(
            PreflightIssue(
                "REQUIRED_REFERENCE_MISSING",
                "INGREDIENTS_TO_VIDEO requires at least one resolved reference ingredient",
            )
        )
    for role in sorted(required_roles - roles):
        code = "START_FRAME_REQUIRED" if role == "START_FRAME" else "END_FRAME_REQUIRED"
        errors.append(PreflightIssue(code, f"generation mode requires a usable {role} reference"))
    if mode in {"EXTEND_VIDEO", "VIDEO_TO_VIDEO"}:
        errors.append(
            PreflightIssue(
                "SOURCE_VIDEO_INPUT_CONTRACT_MISSING",
                f"{mode} requires a source-video role that SCENE_REFERENCE_MAP cannot represent",
            )
        )
    for row, role, instruction, is_required in selections:
        asset_id = _text(row, "Reference_Asset_ID")
        asset = assets.get(asset_id)
        if asset is None:
            errors.append(
                PreflightIssue(
                    "REFERENCE_NOT_FOUND", f"reference asset {asset_id!r} does not resolve"
                )
            )
            continue
        if not _active(asset):
            errors.append(
                PreflightIssue("REFERENCE_INACTIVE", f"reference asset {asset_id} is not ACTIVE")
            )
        if _text(asset, "Reference_Quality_Status").upper() != "PASS":
            errors.append(
                PreflightIssue(
                    "REFERENCE_QUALITY_NOT_PASS",
                    f"reference asset {asset_id} quality status is not PASS",
                )
            )
        drive_id = _text(asset, "Drive_File_ID")
        if not drive_id:
            errors.append(
                PreflightIssue(
                    "REFERENCE_DRIVE_IDENTITY_MISSING",
                    f"reference asset {asset_id} has no persistent Drive file identity",
                )
            )
        if (is_required or role in required_roles or require_usage_instruction) and not instruction:
            errors.append(
                PreflightIssue(
                    "REFERENCE_USAGE_INSTRUCTION_MISSING",
                    f"required reference {asset_id} has no usage instruction",
                )
            )
        refs.append(
            ReferenceDescriptor(
                asset_id,
                role,
                drive_id,
                _text(asset, "Version") or None,
                _text(asset, "Checksum") or None,
                instruction or None,
                _text(row, "Prompt_Mention_Name") or _text(asset, "Asset_Name") or None,
                _bool(row.get("Is_Primary")),
                sync_by_asset.get(asset_id),
            )
        )
    return tuple(refs)


def _join(parts: list[str]) -> str:
    return "\n".join(item.strip() for item in parts if item and item.strip())


def _render(
    template: str,
    blocks: Mapping[str, str],
    errors: list[PreflightIssue],
    code: str,
    *,
    optional_empty: frozenset[str] = frozenset(),
) -> str:
    unknown = sorted(
        {name for name in _PLACEHOLDER_RE.findall(template) if name not in PLACEHOLDERS}
    )
    if unknown:
        errors.append(
            PreflightIssue(
                "PROMPT_TEMPLATE_UNKNOWN_PLACEHOLDER", f"unknown placeholders: {', '.join(unknown)}"
            )
        )
    unresolved = sorted(
        {
            name
            for name in _PLACEHOLDER_RE.findall(template)
            if name in PLACEHOLDERS
            and name not in optional_empty
            and not blocks.get(name, "").strip()
        }
    )
    if unresolved:
        errors.append(
            PreflightIssue(
                "PROMPT_PLACEHOLDER_UNRESOLVED",
                f"required placeholders have no resolved values: {', '.join(unresolved)}",
            )
        )
    rendered = _PLACEHOLDER_RE.sub(
        lambda match: blocks.get(match.group(1), match.group(0)), template
    )
    if _PLACEHOLDER_RE.search(rendered):
        errors.append(
            PreflightIssue(
                "PROMPT_PLACEHOLDER_UNRESOLVED", "unresolved placeholder remains after rendering"
            )
        )
    return rendered.strip()


def _capability_records(catalog: SceneCatalog, model_id: str) -> list[CapabilityRecord]:
    return [
        CapabilityRecord(
            _text(row, "Capability_Type"),
            _text(row, "Capability_Value"),
            None,
            _text(row, "Status") or "ACTIVE",
        )
        for row in catalog.rows("MODEL_CAPABILITY_MAP")
        if _text(row, "Model_ID") == model_id
    ]


def _profile_view(catalog: SceneCatalog) -> SceneCatalog:
    """Wrap raw table rows with the typed Runtime Foundation lookup methods."""

    class View:
        def rows(self, table_name: str) -> list[Row]:
            return catalog.rows(table_name)

        def provider(self, provider_id: str) -> ProviderRecord | None:
            row = _get(catalog, "PROVIDER_MASTER", provider_id)
            return (
                None
                if row is None
                else ProviderRecord(provider_id, _text(row, "Provider_Type"), _text(row, "Status"))
            )

        def account(self, account_id: str) -> AccountRecord | None:
            row = _get(catalog, "PROVIDER_ACCOUNT_MASTER", account_id)
            return (
                None
                if row is None
                else AccountRecord(
                    account_id,
                    _text(row, "Provider_ID"),
                    _text(row, "Environment"),
                    _text(row, "Status"),
                )
            )

        def model(self, model_id: str) -> ModelRecord | None:
            row = _get(catalog, "MODEL_MASTER", model_id)
            return (
                None
                if row is None
                else ModelRecord(model_id, _text(row, "Provider_ID"), _text(row, "Status"))
            )

        def profile(self, profile_id: str) -> ProfileRecord | None:
            row = _get(catalog, "GENERATION_PROFILE_MASTER", profile_id)
            if row is None:
                return None
            output_count = _num(row.get("Default_Output_Count"))
            return ProfileRecord(
                profile_id,
                _text(row, "Provider_Account_ID"),
                _text(row, "Model_ID"),
                _text(row, "Generation_Mode"),
                _text(row, "Environment"),
                _text(row, "Purpose"),
                _text(row, "Status"),
                _text(row, "Prompt_Template_ID") or None,
                _text(row, "Default_Aspect_Ratio") or None,
                _num(row.get("Default_Duration_Sec")),
                _text(row, "Default_Resolution") or None,
                int(output_count) if output_count is not None else None,
                _text(row, "Audio_Mode") or None,
                _text(row, "Quality_Tier") or None,
            )

        def prompt_template(self, prompt_template_id: str) -> PromptTemplateRecord | None:
            row = _get(catalog, "PROMPT_TEMPLATE_MASTER", prompt_template_id)
            if row is None:
                return None
            return PromptTemplateRecord(
                prompt_template_id,
                _text(row, "Status"),
                _text(row, "Provider_ID") or None,
                _text(row, "Model_ID") or None,
                _text(row, "Generation_Mode") or None,
            )

        def capabilities(self, model_id: str) -> list[CapabilityRecord]:
            return _capability_records(catalog, model_id)

    return View()  # type: ignore[return-value]


def preflight_scene(
    catalog: SceneCatalog,
    scene_id: str,
    generation_profile_id: str,
    environment: str,
) -> PreflightResult:
    """Resolve and compile one locked video Scene without performing I/O."""
    errors: list[PreflightIssue] = []
    warnings: list[PreflightIssue] = []
    resolved: dict[str, object] = {}
    technical: dict[str, object] = {}
    scene = _get(catalog, "SCENES", scene_id)
    if scene is None:
        errors.append(PreflightIssue("SCENE_NOT_FOUND", f"scene {scene_id} does not resolve"))
        return _result(scene_id, generation_profile_id, errors, warnings)
    if _text(scene, "Scene_Definition_Status").upper() != "LOCKED":
        errors.append(PreflightIssue("SCENE_NOT_LOCKED", "scene definition must be LOCKED"))
    if _text(scene, "Scene_Type").upper() != "GENERATED_VIDEO":
        errors.append(
            PreflightIssue(
                "SCENE_TYPE_NOT_GENERATABLE",
                "only GENERATED_VIDEO scenes are accepted by the video preflight",
            )
        )
    script = _get(catalog, "SCRIPT_MASTER", _text(scene, "Script_ID"))
    if script is None:
        errors.append(PreflightIssue("SCRIPT_NOT_FOUND", "scene Script_ID does not resolve"))
    elif _text(script, "Script_Status").upper() != "APPROVED":
        errors.append(PreflightIssue("SCRIPT_NOT_APPROVED", "script status must be APPROVED"))
    episode = _get(catalog, "EPISODE_MASTER", _text(script, "Episode_ID")) if script else None
    if script and episode is None:
        errors.append(PreflightIssue("EPISODE_NOT_FOUND", "script Episode_ID does not resolve"))
    series = _get(catalog, "SERIES_MASTER", _text(episode, "Series_ID")) if episode else None
    if episode and series is None:
        errors.append(PreflightIssue("SERIES_NOT_FOUND", "episode Series_ID does not resolve"))
    if not generation_profile_id.strip():
        errors.append(
            PreflightIssue("PROFILE_NOT_FOUND", "Generation_Profile_ID is required explicitly")
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    profile_row = _get(catalog, "GENERATION_PROFILE_MASTER", generation_profile_id)
    if profile_row is None:
        errors.append(
            PreflightIssue("PROFILE_NOT_FOUND", "explicit Generation_Profile_ID does not resolve")
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    if not _active(profile_row):
        errors.append(
            PreflightIssue("PROFILE_INACTIVE", "selected Generation Profile is not ACTIVE")
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    expected_environment = {"DEVELOPMENT": "DEV", "STAGING": "TEST", "PRODUCTION": "PROD"}.get(
        environment.upper(), environment.upper()
    )
    if _text(profile_row, "Environment").upper() != expected_environment:
        errors.append(
            PreflightIssue(
                "PROFILE_ENVIRONMENT_MISMATCH",
                "Generation Profile environment does not match runtime environment",
            )
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    account_row = _get(
        catalog, "PROVIDER_ACCOUNT_MASTER", _text(profile_row, "Provider_Account_ID")
    )
    if account_row is None:
        errors.append(
            PreflightIssue(
                "PROVIDER_ACCOUNT_NOT_FOUND", "profile Provider_Account_ID does not resolve"
            )
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    if not _active(account_row):
        errors.append(
            PreflightIssue("PROVIDER_ACCOUNT_INACTIVE", "selected Provider Account is not ACTIVE")
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    model_row = _get(catalog, "MODEL_MASTER", _text(profile_row, "Model_ID"))
    if model_row is None:
        errors.append(PreflightIssue("MODEL_NOT_FOUND", "profile Model_ID does not resolve"))
        return _result(scene_id, generation_profile_id, errors, warnings)
    if not _active(model_row):
        errors.append(PreflightIssue("MODEL_INACTIVE", "selected Model is not ACTIVE"))
        return _result(scene_id, generation_profile_id, errors, warnings)
    if _text(account_row, "Provider_ID") != _text(model_row, "Provider_ID"):
        errors.append(
            PreflightIssue(
                "PROVIDER_MISMATCH", "Provider Account and Model belong to different providers"
            )
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    if _text(account_row, "Environment").upper() != _text(profile_row, "Environment").upper():
        errors.append(
            PreflightIssue(
                "PROFILE_ENVIRONMENT_MISMATCH",
                "Provider Account and Generation Profile environments differ",
            )
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    provider_row = _get(catalog, "PROVIDER_MASTER", _text(model_row, "Provider_ID"))
    if provider_row is None or not _active(provider_row):
        errors.append(
            PreflightIssue("PROVIDER_NOT_FOUND", "resolved provider is missing or inactive")
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    template_id = _text(profile_row, "Prompt_Template_ID")
    template = _get(catalog, "PROMPT_TEMPLATE_MASTER", template_id)
    if not template_id or template is None:
        errors.append(
            PreflightIssue("TEMPLATE_NOT_FOUND", "profile must resolve an existing Prompt Template")
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    if not _active(template):
        errors.append(PreflightIssue("TEMPLATE_INACTIVE", "selected Prompt Template is not ACTIVE"))
        return _result(scene_id, generation_profile_id, errors, warnings)
    if _text(template, "Provider_ID") and _text(template, "Provider_ID") != _text(
        model_row, "Provider_ID"
    ):
        errors.append(
            PreflightIssue(
                "TEMPLATE_PROVIDER_MISMATCH",
                "Prompt Template provider does not match resolved provider",
            )
        )
    if _text(template, "Model_ID") and _text(template, "Model_ID") != _text(model_row, "Model_ID"):
        errors.append(
            PreflightIssue(
                "TEMPLATE_MODEL_MISMATCH", "Prompt Template model does not match resolved model"
            )
        )
    if _text(template, "Generation_Mode") and _text(template, "Generation_Mode") != _text(
        profile_row, "Generation_Mode"
    ):
        errors.append(
            PreflightIssue(
                "TEMPLATE_GENERATION_MODE_MISMATCH",
                "Prompt Template mode does not match Generation Profile",
            )
        )
    if errors:
        return _result(scene_id, generation_profile_id, errors, warnings)
    supported_modes, _mode_enumerable = _capability_values(
        catalog,
        _text(model_row, "Model_ID"),
        _text(profile_row, "Generation_Mode"),
        "GENERATION_MODE",
    )
    if _text(profile_row, "Generation_Mode") not in supported_modes:
        errors.append(
            PreflightIssue(
                "GENERATION_MODE_UNSUPPORTED",
                "selected model does not have an ACTIVE capability row for this generation mode",
            )
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    try:
        profile, provider, account, model = _resolve_profile(
            _profile_view(catalog), generation_profile_id, environment
        )
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()
        code = "PROFILE_RESOLUTION_FAILED"
        if "generation mode" in lowered:
            code = "GENERATION_MODE_UNSUPPORTED"
        elif "prompt template" in lowered and "provider" in lowered:
            code = "TEMPLATE_PROVIDER_MISMATCH"
        elif "prompt template" in lowered and "model" in lowered:
            code = "TEMPLATE_MODEL_MISMATCH"
        elif "prompt template" in lowered and "generation mode" in lowered:
            code = "TEMPLATE_GENERATION_MODE_MISMATCH"
        errors.append(PreflightIssue(code, message))
        return _result(scene_id, generation_profile_id, errors, warnings)
    account_row = _get(catalog, "PROVIDER_ACCOUNT_MASTER", profile.provider_account_id)
    template_id = profile.prompt_template_id or _text(profile_row, "Prompt_Template_ID")
    template = _get(catalog, "PROMPT_TEMPLATE_MASTER", template_id)
    if template is None:
        errors.append(
            PreflightIssue("TEMPLATE_NOT_FOUND", "profile must resolve an existing Prompt Template")
        )
        return _result(scene_id, generation_profile_id, errors, warnings)
    if not _active(template):
        errors.append(PreflightIssue("TEMPLATE_INACTIVE", "selected Prompt Template is not ACTIVE"))
    if _text(template, "Provider_ID") and _text(template, "Provider_ID") != provider.provider_id:
        errors.append(
            PreflightIssue(
                "TEMPLATE_PROVIDER_MISMATCH",
                "Prompt Template provider does not match resolved provider",
            )
        )
    if _text(template, "Model_ID") and _text(template, "Model_ID") != model.model_id:
        errors.append(
            PreflightIssue(
                "TEMPLATE_MODEL_MISMATCH", "Prompt Template model does not match resolved model"
            )
        )
    if (
        _text(template, "Generation_Mode")
        and _text(template, "Generation_Mode") != profile.generation_mode
    ):
        errors.append(
            PreflightIssue(
                "TEMPLATE_GENERATION_MODE_MISMATCH",
                "Prompt Template mode does not match Generation Profile",
            )
        )
    resolved.update(
        {
            "provider_id": provider.provider_id,
            "provider_account_id": account.provider_account_id,
            "model_id": model.model_id,
            "generation_mode": profile.generation_mode,
            "prompt_template_id": template_id,
            "environment": profile.environment,
        }
    )
    if account_row and _text(account_row, "Auth_Profile_Key"):
        # Logical reference only. Never resolve or expose secret material.
        resolved["auth_profile_configured"] = True
    duration = _num(scene.get("Duration_Target_Sec"))
    if duration is None:
        duration = profile.default_duration_sec
    aspect = _text(scene, "Aspect_Ratio_Override") or _text(series, "Default_Aspect_Ratio")
    if not aspect:
        errors.append(
            PreflightIssue(
                "ASPECT_RATIO_UNRESOLVED",
                "Scene override and Series default do not resolve an aspect ratio",
            )
        )
    resolution = profile.default_resolution or _text(
        _get(catalog, "MODEL_MASTER", model.model_id), "Default_Resolution"
    )
    output_count = profile.default_output_count
    technical.update(
        {
            "duration_sec": duration,
            "aspect_ratio": aspect or None,
            "resolution": resolution or None,
            "output_count": output_count,
            "audio_mode": profile.audio_mode,
            "quality_tier": profile.quality_tier,
            "generation_mode": profile.generation_mode,
        }
    )
    for value, kind, code in (
        (duration, "DURATION_SEC", "DURATION_UNSUPPORTED"),
        (aspect, "ASPECT_RATIO", "ASPECT_RATIO_UNSUPPORTED"),
        (resolution, "RESOLUTION", "RESOLUTION_UNSUPPORTED"),
        (output_count, "OUTPUT_COUNT", "PARAMETER_VALUE_INVALID"),
        (profile.audio_mode, "AUDIO_MODE", "PARAMETER_VALUE_INVALID"),
        (profile.quality_tier, "QUALITY_TIER", "PARAMETER_VALUE_INVALID"),
    ):
        if value is not None:
            _validate_value(
                catalog,
                model.model_id,
                profile.generation_mode,
                kind,
                value,
                code,
                errors,
                warnings,
            )
    scene_characters = _scene_characters(catalog, scene_id)
    for mapping in catalog.rows("SCENE_CHARACTER_MAP"):
        if _text(mapping, "Scene_ID") != scene_id or _text(mapping, "Status").upper() != "ACTIVE":
            continue
        character_id = _text(mapping, "Character_ID")
        character = _get(catalog, "CHARACTER_MASTER", character_id)
        if character is None:
            errors.append(
                PreflightIssue(
                    "CHARACTER_NOT_FOUND",
                    f"Scene Character references missing Character {character_id}",
                )
            )
        wardrobe_id = _text(mapping, "Wardrobe_ID") or _text(character, "Default_Wardrobe_ID")
        if wardrobe_id and _get(catalog, "WARDROBE_MASTER", wardrobe_id) is None:
            errors.append(
                PreflightIssue(
                    "WARDROBE_NOT_FOUND", f"resolved Wardrobe {wardrobe_id} does not exist"
                )
            )
    entities: list[tuple[str, str]] = []
    for _mapping, character, wardrobe in scene_characters:
        entities.append(("CHARACTER", _text(character, "Character_ID")))
        if wardrobe:
            entities.append(("WARDROBE", _text(wardrobe, "Wardrobe_ID")))
    location_id = _text(scene, "Location_ID")
    location = _get(catalog, "LOCATION_MASTER", location_id)
    if location_id and not _active(location):
        errors.append(PreflightIssue("LOCATION_NOT_FOUND", "Scene Location is missing or inactive"))
    if location:
        entities.append(("LOCATION", location_id))
    visual_id = _text(scene, "Visual_Style_ID") or _text(series, "Default_Visual_Style_ID")
    visual = _get(catalog, "VISUAL_STYLE_MASTER", visual_id)
    if visual:
        entities.append(("STYLE", visual_id))
    camera_id = (
        _text(scene, "Camera_Profile_ID")
        or _text(location, "Default_Camera_Profile_ID")
        or _text(series, "Default_Camera_Profile_ID")
    )
    camera = _get(catalog, "CAMERA_PROFILE_MASTER", camera_id)
    lighting_id = (
        _text(scene, "Lighting_ID")
        or _text(location, "Default_Lighting_ID")
        or _text(visual, "Default_Lighting_ID")
    )
    lighting = _get(catalog, "LIGHTING_MASTER", lighting_id)
    era_id = _text(location, "Era_ID") or _text(series, "Era_ID")
    era = _get(catalog, "ERA_MASTER", era_id)
    if era_id and not _active(era):
        errors.append(PreflightIssue("ERA_NOT_FOUND", "resolved Era is missing or inactive"))
    if visual_id and not _active(visual):
        errors.append(
            PreflightIssue("VISUAL_STYLE_NOT_FOUND", "resolved Visual Style is missing or inactive")
        )
    if camera_id and not _active(camera):
        errors.append(
            PreflightIssue(
                "CAMERA_PROFILE_NOT_FOUND", "resolved Camera Profile is missing or inactive"
            )
        )
    if lighting_id and not _active(lighting):
        errors.append(
            PreflightIssue("LIGHTING_NOT_FOUND", "resolved Lighting record is missing or inactive")
        )
    for _mapping, character, wardrobe in scene_characters:
        if not _active(character):
            errors.append(
                PreflightIssue(
                    "CHARACTER_NOT_FOUND",
                    f"Character {_text(character, 'Character_ID')} is missing or inactive",
                )
            )
        if wardrobe is not None and not _active(wardrobe):
            errors.append(
                PreflightIssue(
                    "WARDROBE_NOT_FOUND", f"Wardrobe {_text(wardrobe, 'Wardrobe_ID')} is inactive"
                )
            )
    references = _resolve_references(
        catalog,
        scene,
        profile.generation_mode,
        provider.provider_id,
        entities,
        "{REFERENCE_USAGE}" in _text(template, "Prompt_Template"),
        errors,
    )
    canonical_values: dict[str, object] = {
        "DURATION_SEC": duration,
        "ASPECT_RATIO": aspect or None,
        "RESOLUTION": resolution or None,
        "OUTPUT_COUNT": output_count,
        "AUDIO_ENABLED": profile.audio_mode not in {None, "NONE"},
        "REFERENCE_ASSETS": [ref.as_dict() for ref in references],
        "START_FRAME": [
            ref.drive_file_id for ref in references if ref.reference_role == "START_FRAME"
        ],
        "END_FRAME": [ref.drive_file_id for ref in references if ref.reference_role == "END_FRAME"],
        "QUALITY_TIER": profile.quality_tier,
    }
    adapter_parameters = _validate_parameter_maps(
        catalog, model.model_id, profile.generation_mode, canonical_values, errors
    )
    technical["canonical_parameters"] = {k: v for k, v in canonical_values.items() if v is not None}
    technical["adapter_parameters"] = adapter_parameters
    effective_language = (
        _text(script, "Dialogue_Language")
        or _text(episode, "Primary_Language")
        or _text(series, "Primary_Language")
    )
    if effective_language:
        resolved["language"] = effective_language
    subject_parts: list[str] = []
    action_parts = [_text(scene, "Action_Description")]
    emotion_parts = [_text(scene, "Scene_Mood")]
    continuity_parts = [
        _text(scene, "Scene_Start_State"),
        _text(scene, "Scene_End_State"),
        _text(scene, "Continuity_Notes"),
    ]
    negative_sources = [
        _text(era, "Negative_Constraints"),
    ]
    for mapping, character, wardrobe in scene_characters:
        identity = _text(character, "Prompt_Identity_Text")
        wardrobe_text = _text(wardrobe, "Prompt_Wardrobe_Text") if wardrobe else ""
        character_bits = [
            identity,
            wardrobe_text,
            _text(mapping, "Appearance_Override"),
            _text(mapping, "Position_Instruction"),
        ]
        if any(character_bits):
            subject_parts.append("; ".join(part for part in character_bits if part))
        action_parts.extend([_text(mapping, "Action_Instruction")])
        emotion_parts.extend([_text(mapping, "Emotion_Instruction")])
        continuity_parts.extend(
            [
                _text(mapping, "Continuity_Override"),
                _text(character, "Continuity_Rules"),
                _text(wardrobe, "Continuity_Rules"),
            ]
        )
        negative_sources.extend(
            [_text(character, "Negative_Constraints"), _text(wardrobe, "Negative_Constraints")]
        )
    env_parts = [
        _text(location, "Prompt_Location_Text"),
        _text(scene, "Environment_Override"),
        _text(scene, "Time_Of_Day_Override"),
    ]
    camera_text = _text(camera, "Prompt_Camera_Text")
    lighting_text = _text(lighting, "Prompt_Lighting_Text")
    style_parts = [
        _text(visual, key)
        for key in (
            "Style_Description",
            "Realism_Level",
            "Color_Profile",
            "Texture_Profile",
            "Cinematic_Keywords",
        )
    ]
    continuity_parts.extend([_text(location, "Continuity_Rules")])
    negative_sources.extend(
        [
            _text(location, "Negative_Constraints"),
            _text(visual, "Negative_Style"),
            _text(scene, "Negative_Constraints_Override"),
        ]
    )
    if _text(scene, "Continuity_Source_Scene_ID"):
        source_id = _text(scene, "Continuity_Source_Scene_ID")
        if _continuity_cycle(catalog, scene_id):
            errors.append(
                PreflightIssue(
                    "CONTINUITY_CYCLE_DETECTED", "continuity source chain contains a cycle"
                )
            )
        else:
            prior = _get(catalog, "SCENES", source_id)
            if prior is None:
                errors.append(
                    PreflightIssue(
                        "CONTINUITY_SCENE_NOT_FOUND", "continuity source scene does not resolve"
                    )
                )
            else:
                continuity_parts.append(_text(prior, "Scene_End_State"))
    dialogue_rows = sorted(
        [
            row
            for row in catalog.rows("SCENE_DIALOGUE_LINES")
            if _text(row, "Scene_ID") == scene_id and _text(row, "Status").upper() == "ACTIVE"
        ],
        key=lambda row: (
            _num(row.get("Line_No")) if _num(row.get("Line_No")) is not None else math.inf,
            _text(row, "Dialogue_Line_ID"),
        ),
    )
    for dialogue in dialogue_rows:
        # Dialogue is resolved into deterministic context for template-specific consumers;
        # the locked placeholder vocabulary has no dialogue slot, so prose is not injected.
        _ = (_text(dialogue, "Dialogue_Text"), _text(dialogue, "Emotion"), effective_language)
    usage_parts = [
        "; ".join(
            part
            for part in (
                ref.prompt_mention_name or ref.reference_asset_id,
                ref.usage_instruction or "",
            )
            if part
        )
        for ref in references
    ]
    negatives = list(
        dict.fromkeys(value.strip() for value in negative_sources if value and value.strip())
    )
    blocks = {
        "SUBJECT": _join(subject_parts),
        "REFERENCE_USAGE": _join(usage_parts),
        "ACTION": _join(action_parts),
        "ENVIRONMENT": _join(env_parts),
        "EMOTION": _join(emotion_parts),
        "CAMERA": camera_text,
        "LIGHTING": lighting_text,
        "VISUAL_STYLE": _join(style_parts),
        "CONTINUITY_RULES": _join(continuity_parts),
        "NEGATIVE_CONSTRAINTS": _join(negatives),
    }
    compiled = _render(_text(template, "Prompt_Template"), blocks, errors, "PROMPT_TEMPLATE")
    negative_template = _text(template, "Negative_Template")
    compiled_negative = (
        _render(
            negative_template,
            blocks,
            errors,
            "NEGATIVE_TEMPLATE",
            optional_empty=frozenset({"NEGATIVE_CONSTRAINTS"}),
        )
        if negative_template
        else ""
    )
    if not compiled:
        errors.append(PreflightIssue("COMPILED_PROMPT_EMPTY", "compiled positive prompt is empty"))
    if negatives:
        compiled_negative = _join([compiled_negative, _join(negatives)])
    if "{" in compiled or "}" in compiled:
        if _PLACEHOLDER_RE.search(compiled):
            errors.append(
                PreflightIssue(
                    "PROMPT_PLACEHOLDER_UNRESOLVED", "placeholder remains in compiled prompt"
                )
            )
    source_context = {
        "scene": _clean_row(scene),
        "script": _clean_row(script) if script else None,
        "episode": _clean_row(episode) if episode else None,
        "series": _clean_row(series) if series else None,
        "scene_characters": [
            {
                "mapping": _clean_row(mapping),
                "character": _clean_row(character),
                "wardrobe": _clean_row(wardrobe) if wardrobe else None,
            }
            for mapping, character, wardrobe in scene_characters
        ],
        "references": [ref.as_dict() for ref in references],
        "dialogue": [
            {
                "Dialogue_Line_ID": _text(row, "Dialogue_Line_ID"),
                "Line_No": row.get("Line_No"),
                "Speaker_Type": _text(row, "Speaker_Type"),
                "Character_ID": _text(row, "Character_ID"),
                "Dialogue_Text": _text(row, "Dialogue_Text"),
                "Emotion": _text(row, "Emotion"),
                "Speaking_Style_Override": _text(row, "Speaking_Style_Override"),
            }
            for row in dialogue_rows
        ],
        "location": _clean_row(location) if location else None,
        "visual_style": _clean_row(visual) if visual else None,
        "camera": _clean_row(camera) if camera else None,
        "lighting": _clean_row(lighting) if lighting else None,
        "era": _clean_row(era) if era else None,
        "profile": _clean_row(profile_row) if profile_row else None,
        "provider_account": {
            k: v for k, v in _clean_row(account_row or {}).items() if k not in {"Auth_Profile_Key"}
        },
        "provider": {"provider_id": provider.provider_id, "provider_type": provider.provider_type},
        "model": _clean_row(_get(catalog, "MODEL_MASTER", model.model_id) or {}),
        "capabilities": sorted(
            (
                _semantic_config_row(row)
                for row in catalog.rows("MODEL_CAPABILITY_MAP")
                if _text(row, "Model_ID") == model.model_id
                and _text(row, "Status").upper() == "ACTIVE"
            ),
            key=canonical_json,
        ),
        "parameter_maps": sorted(
            (
                _semantic_config_row(row)
                for row in catalog.rows("MODEL_PARAMETER_MAP")
                if _text(row, "Model_ID") == model.model_id
                and _text(row, "Generation_Mode") == profile.generation_mode
                and _text(row, "Status").upper() == "ACTIVE"
            ),
            key=canonical_json,
        ),
        "template": _clean_row(template),
        "blocks": blocks,
        "technical_configuration": technical,
    }
    try:
        source_hash = sha256_json(source_context)
        prompt_hash = sha256_json({"prompt": compiled, "negative_prompt": compiled_negative})
    except (TypeError, ValueError):
        source_hash = None
        prompt_hash = None
        errors.append(
            PreflightIssue("CONTEXT_HASH_FAILED", "canonical context serialization failed")
        )
    resolved.update(
        {
            "provider_id": provider.provider_id,
            "provider_account_id": account.provider_account_id,
            "model_id": model.model_id,
            "generation_mode": profile.generation_mode,
            "prompt_template_id": template_id,
        }
    )
    errors = sorted(set(errors))
    warnings = sorted(set(warnings))
    return PreflightResult(
        "PREFLIGHT_HOLD" if errors else "PREFLIGHT_PASS",
        not errors,
        scene_id,
        generation_profile_id,
        resolved,
        technical,
        compiled,
        compiled_negative,
        source_hash,
        prompt_hash,
        references,
        tuple(errors),
        tuple(warnings),
    )


def _result(
    scene_id: str, profile_id: str, errors: list[PreflightIssue], warnings: list[PreflightIssue]
) -> PreflightResult:
    return PreflightResult(
        "PREFLIGHT_HOLD",
        False,
        scene_id,
        profile_id,
        errors=tuple(sorted(set(errors))),
        warnings=tuple(sorted(set(warnings))),
    )


def build_scene_prompt_payload(
    result: PreflightResult,
    scene_prompt_id: str,
    prompt_version: int,
    *,
    compiler_version: str = "phase-e/1",
    created_at: str | None = None,
    updated_at: str | None = None,
) -> dict[str, object]:
    """Build (but never persist or approve) a schema-compatible prompt candidate."""
    if not result.ready_for_provider_adapter or result.status != "PREFLIGHT_PASS":
        raise ValueError("a SCENE_PROMPT_VERSION candidate requires successful preflight")
    if not re.fullmatch(r"SPV_[0-9]{6}", scene_prompt_id) or prompt_version < 1:
        raise ValueError("explicit valid Scene_Prompt_ID and positive Prompt_Version are required")
    payload: dict[str, object] = {
        "Scene_Prompt_ID": scene_prompt_id,
        "Scene_ID": result.scene_id,
        "Prompt_Version": prompt_version,
        "Generation_Profile_ID": result.generation_profile_id,
        "Compiled_Prompt_Text": result.compiled_prompt or "",
        "Compiled_Negative_Prompt": result.compiled_negative_prompt or "",
        "Compiler_Version": compiler_version,
        "Source_Context_SHA256": result.source_context_sha256 or "",
        "Prompt_SHA256": result.prompt_sha256 or "",
        "Prompt_Status": "VALIDATED",
        "Validation_Message": "PREFLIGHT_PASS; provider authentication and live health not checked",
    }
    if created_at:
        payload["Created_At"] = created_at
    if updated_at:
        payload["Updated_At"] = updated_at
    return payload


class InMemorySceneCatalog:
    """Isolated table-row catalog for tests and offline compiler clients."""

    def __init__(self, tables: Mapping[str, list[Row]]) -> None:
        self._tables = {name: [dict(row) for row in rows] for name, rows in tables.items()}

    def rows(self, table_name: str) -> list[Row]:
        return list(self._tables.get(table_name, []))

    def provider(self, provider_id: str) -> ProviderRecord | None:
        return _profile_view(self).provider(provider_id)

    def account(self, account_id: str) -> AccountRecord | None:
        return _profile_view(self).account(account_id)

    def model(self, model_id: str) -> ModelRecord | None:
        return _profile_view(self).model(model_id)

    def profile(self, profile_id: str) -> ProfileRecord | None:
        return _profile_view(self).profile(profile_id)

    def prompt_template(self, prompt_template_id: str) -> PromptTemplateRecord | None:
        return _profile_view(self).prompt_template(prompt_template_id)

    def capabilities(self, model_id: str) -> list[CapabilityRecord]:
        return _capability_records(self, model_id)
