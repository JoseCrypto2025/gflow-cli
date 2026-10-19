"""Build, validate, and approve immutable prompt versions without provider calls."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .phase_e import PreflightResult, build_scene_prompt_payload
from .prompt_approval import (
    PromptApprovalIntegrityError,
    guard_scene_prompt_version_update,
    require_single_current_approval,
)
from .timestamps import format_utc_timestamp

PROMPT_VERSION_FIELDS = (
    "Scene_Prompt_ID",
    "Scene_ID",
    "Prompt_Version",
    "Generation_Profile_ID",
    "Compiled_Prompt_Text",
    "Compiled_Negative_Prompt",
    "Compiler_Version",
    "Source_Context_SHA256",
    "Prompt_SHA256",
    "Prompt_Status",
    "Validation_Message",
    "Approved_At",
    "Created_At",
    "Updated_At",
)


class PromptVersionLifecycleError(ValueError):
    """Safe, stable lifecycle failure; never embeds prompt text or API responses."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class PromptVersionStore(Protocol):
    """Narrow persistence contract for SCENE_PROMPT_VERSION only."""

    def read_prompt_rows(self) -> list[dict[str, object]]: ...

    def append_candidate(
        self,
        row: dict[str, object],
        *,
        expected_snapshot_sha256: str,
        fresh_phase_e: PreflightResult,
    ) -> dict[str, object]: ...

    def update_candidate(
        self,
        row: dict[str, object],
        *,
        expected_row_sha256: str,
        fresh_phase_e: PreflightResult,
    ) -> dict[str, object]: ...

    def approve_pair(
        self,
        previous: dict[str, object],
        candidate: dict[str, object],
        *,
        expected_previous_sha256: str,
        expected_candidate_sha256: str,
        fresh_phase_e: PreflightResult,
    ) -> tuple[dict[str, object], dict[str, object]]: ...


@dataclass(frozen=True, slots=True)
class PromptVersionPreview:
    scene_prompt_id: str
    prompt_version: int
    prompt_sha256: str
    source_context_sha256: str
    initial_status: str
    previous_approved_id: str
    snapshot_sha256: str
    candidate: Mapping[str, object]
    phase_e_result: PreflightResult


def prompt_row_sha256(row: Mapping[str, object]) -> str:
    """Hash one row in the exact canonical Sheets field order."""
    import hashlib
    import json

    encoded = json.dumps(
        [row.get(name, "") for name in PROMPT_VERSION_FIELDS],
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def prompt_snapshot_sha256(rows: Sequence[Mapping[str, object]]) -> str:
    """Hash the ordered prompt table snapshot used to allocate a new identity."""
    import hashlib
    import json

    ordered = [[row.get(name, "") for name in PROMPT_VERSION_FIELDS] for row in rows]
    encoded = json.dumps(ordered, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def current_approved_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    scene_id: str | None = None,
    generation_profile_id: str | None = None,
) -> list[dict[str, object]]:
    """Return current versions; the approved schema defines current by status."""
    return [
        dict(row)
        for row in rows
        if row.get("Prompt_Status") == "APPROVED"
        and (scene_id is None or row.get("Scene_ID") == scene_id)
        and (
            generation_profile_id is None
            or row.get("Generation_Profile_ID") == generation_profile_id
        )
    ]


class PromptVersionLifecycle:
    """Orchestrate Phase E and guarded persistence; never invokes Flow/provider APIs."""

    def __init__(
        self,
        store: PromptVersionStore,
        *,
        phase_e: Callable[[str, str], PreflightResult],
        compiler_version: str = "phase-e/1",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.phase_e = phase_e
        self.compiler_version = compiler_version
        self.clock = clock

    def preview(self, scene_id: str, generation_profile_id: str) -> PromptVersionPreview:
        """Construct a no-write draft from a fresh Phase E result and live prompt rows."""
        result = self._fresh_phase_e(scene_id, generation_profile_id)
        rows = self.store.read_prompt_rows()
        self._validate_table_rows(rows)
        matching = [
            row
            for row in rows
            if row.get("Scene_ID") == scene_id
            and row.get("Generation_Profile_ID") == generation_profile_id
        ]
        try:
            require_single_current_approval(
                matching, scene_id=scene_id, generation_profile_id=generation_profile_id
            )
        except PromptApprovalIntegrityError as exc:
            raise PromptVersionLifecycleError("CURRENT_APPROVAL_INVALID", str(exc)) from exc
        approved = current_approved_rows(
            matching, scene_id=scene_id, generation_profile_id=generation_profile_id
        )
        previous = approved[0]
        self._require_unique_ids_and_versions(rows)
        next_id_number = max((int(str(row["Scene_Prompt_ID"])[4:]) for row in rows), default=0) + 1
        next_version = (
            max(
                (
                    self._version_number(row.get("Prompt_Version"))
                    for row in rows
                    if row.get("Scene_ID") == scene_id
                ),
                default=0,
            )
            + 1
        )
        if next_id_number > 999999 or next_version < 1:
            raise PromptVersionLifecycleError(
                "IDENTITY_EXHAUSTED", "prompt identity space is exhausted"
            )
        scene_prompt_id = f"SPV_{next_id_number:06d}"
        created_at = format_utc_timestamp(self.clock() if self.clock else None, timespec="seconds")
        built = build_scene_prompt_payload(
            result,
            scene_prompt_id,
            next_version,
            compiler_version=self.compiler_version,
            created_at=created_at,
            updated_at=created_at,
        )
        candidate = {field: built.get(field, "") for field in PROMPT_VERSION_FIELDS}
        candidate.update(
            {
                "Prompt_Status": "DRAFT",
                "Validation_Message": "AWAITING_FRESH_PHASE_E_VALIDATION",
                "Approved_At": "",
            }
        )
        try:
            guard_scene_prompt_version_update(None, candidate)
        except PromptApprovalIntegrityError as exc:
            raise PromptVersionLifecycleError("CANDIDATE_INVALID", str(exc)) from exc
        self._require_payload(candidate, result)
        return PromptVersionPreview(
            scene_prompt_id=scene_prompt_id,
            prompt_version=next_version,
            prompt_sha256=str(candidate["Prompt_SHA256"]),
            source_context_sha256=str(candidate["Source_Context_SHA256"]),
            initial_status="DRAFT",
            previous_approved_id=str(previous["Scene_Prompt_ID"]),
            snapshot_sha256=prompt_snapshot_sha256(rows),
            candidate=candidate,
            phase_e_result=result,
        )

    def create_prompt_version(
        self,
        scene_id: str,
        generation_profile_id: str,
        *,
        expected_current_id: str,
    ) -> dict[str, object]:
        """Persist one fresh DRAFT candidate, then require exact append readback."""
        preview = self.preview(scene_id, generation_profile_id)
        if preview.previous_approved_id != expected_current_id:
            raise PromptVersionLifecycleError(
                "CONCURRENT_MODIFICATION", "current approved prompt changed"
            )
        try:
            persisted = self.store.append_candidate(
                dict(preview.candidate),
                expected_snapshot_sha256=preview.snapshot_sha256,
                fresh_phase_e=preview.phase_e_result,
            )
        except Exception as exc:
            if isinstance(exc, PromptVersionLifecycleError):
                raise
            raise PromptVersionLifecycleError(
                "CREATE_WRITE_FAILED", "prompt candidate write failed safely"
            ) from exc
        if persisted != dict(preview.candidate):
            raise PromptVersionLifecycleError(
                "CREATE_READBACK_MISMATCH", "prompt candidate readback differed"
            )
        return persisted

    def validate_prompt_version(self, scene_prompt_id: str) -> dict[str, object]:
        """Re-run Phase E; change DRAFT to VALIDATED only on exact text/hash match."""
        rows = self.store.read_prompt_rows()
        row = self._find_one(rows, scene_prompt_id)
        if row.get("Prompt_Status") != "DRAFT":
            raise PromptVersionLifecycleError(
                "INVALID_TRANSITION", "only a DRAFT prompt can be validated"
            )
        result = self._fresh_phase_e(str(row["Scene_ID"]), str(row["Generation_Profile_ID"]))
        self._require_payload(row, result)
        now = format_utc_timestamp(self.clock() if self.clock else None, timespec="seconds")
        proposed = {
            **row,
            "Prompt_Status": "VALIDATED",
            "Validation_Message": "FRESH_PHASE_E_EXACT_MATCH",
            "Updated_At": now,
        }
        try:
            guard_scene_prompt_version_update(row, proposed, fresh_phase_e=result)
        except PromptApprovalIntegrityError as exc:
            raise PromptVersionLifecycleError("VALIDATION_GUARD_FAILED", str(exc)) from exc
        updated = self.store.update_candidate(
            proposed, expected_row_sha256=prompt_row_sha256(row), fresh_phase_e=result
        )
        if updated != proposed:
            raise PromptVersionLifecycleError(
                "VALIDATION_READBACK_MISMATCH", "validated prompt readback differed"
            )
        return updated

    def approve_prompt_version(
        self, scene_prompt_id: str, *, expected_previous_id: str
    ) -> dict[str, object]:
        """Revalidate, atomically switch the two status rows, and verify one approval."""
        rows = self.store.read_prompt_rows()
        candidate = self._find_one(rows, scene_prompt_id)
        if candidate.get("Prompt_Status") != "VALIDATED":
            raise PromptVersionLifecycleError(
                "INVALID_TRANSITION", "only a VALIDATED prompt can be approved"
            )
        scene_id = str(candidate["Scene_ID"])
        profile_id = str(candidate["Generation_Profile_ID"])
        matching = [
            row
            for row in rows
            if row.get("Scene_ID") == scene_id and row.get("Generation_Profile_ID") == profile_id
        ]
        try:
            require_single_current_approval(
                matching, scene_id=scene_id, generation_profile_id=profile_id
            )
        except PromptApprovalIntegrityError as exc:
            raise PromptVersionLifecycleError("CURRENT_APPROVAL_INVALID", str(exc)) from exc
        previous = current_approved_rows(
            matching, scene_id=scene_id, generation_profile_id=profile_id
        )[0]
        if previous.get("Scene_Prompt_ID") != expected_previous_id:
            raise PromptVersionLifecycleError(
                "CONCURRENT_MODIFICATION", "current approved prompt changed"
            )
        result = self._fresh_phase_e(scene_id, profile_id)
        self._require_payload(candidate, result)
        now = format_utc_timestamp(self.clock() if self.clock else None, timespec="seconds")
        superseded = {**previous, "Prompt_Status": "SUPERSEDED", "Updated_At": now}
        approved = {
            **candidate,
            "Prompt_Status": "APPROVED",
            "Validation_Message": "FRESH_PHASE_E_EXACT_MATCH",
            "Approved_At": now,
            "Updated_At": now,
        }
        try:
            guard_scene_prompt_version_update(previous, superseded)
            guard_scene_prompt_version_update(candidate, approved, fresh_phase_e=result)
        except PromptApprovalIntegrityError as exc:
            raise PromptVersionLifecycleError("APPROVAL_GUARD_FAILED", str(exc)) from exc
        try:
            verified_previous, verified_candidate = self.store.approve_pair(
                superseded,
                approved,
                expected_previous_sha256=prompt_row_sha256(previous),
                expected_candidate_sha256=prompt_row_sha256(candidate),
                fresh_phase_e=result,
            )
        except Exception as exc:
            if isinstance(exc, PromptVersionLifecycleError):
                raise
            raise PromptVersionLifecycleError(
                "APPROVAL_WRITE_FAILED", "prompt approval write failed safely"
            ) from exc
        if verified_previous != superseded or verified_candidate != approved:
            raise PromptVersionLifecycleError(
                "APPROVAL_READBACK_MISMATCH", "approval readback differed"
            )
        final_rows = self.store.read_prompt_rows()
        try:
            require_single_current_approval(
                final_rows, scene_id=scene_id, generation_profile_id=profile_id
            )
        except PromptApprovalIntegrityError as exc:
            raise PromptVersionLifecycleError("CURRENT_APPROVAL_INVALID", str(exc)) from exc
        return verified_candidate

    def _fresh_phase_e(self, scene_id: str, generation_profile_id: str) -> PreflightResult:
        result = self.phase_e(scene_id, generation_profile_id)
        if (
            result.status != "PREFLIGHT_PASS"
            or not result.ready_for_provider_adapter
            or result.errors
            or result.scene_id != scene_id
            or result.generation_profile_id != generation_profile_id
        ):
            raise PromptVersionLifecycleError(
                "PHASE_E_FAILED", "fresh Phase E failed or identity differed"
            )
        if (
            not result.compiled_prompt
            or not _valid_sha256(result.prompt_sha256)
            or not _valid_sha256(result.source_context_sha256)
        ):
            raise PromptVersionLifecycleError(
                "PHASE_E_MALFORMED", "fresh Phase E payload is incomplete"
            )
        return result

    @staticmethod
    def _require_payload(row: Mapping[str, object], result: PreflightResult) -> None:
        if row.get("Prompt_SHA256") != result.prompt_sha256:
            raise PromptVersionLifecycleError(
                "PROMPT_HASH_MISMATCH", "prompt hash differs from fresh Phase E"
            )
        if row.get("Source_Context_SHA256") != result.source_context_sha256:
            raise PromptVersionLifecycleError(
                "SOURCE_CONTEXT_HASH_MISMATCH", "source context hash differs from fresh Phase E"
            )
        expected = {
            "Scene_ID": result.scene_id,
            "Generation_Profile_ID": result.generation_profile_id,
            "Compiled_Prompt_Text": result.compiled_prompt or "",
            "Compiled_Negative_Prompt": result.compiled_negative_prompt or "",
        }
        if any(row.get(field) != value for field, value in expected.items()):
            raise PromptVersionLifecycleError(
                "PROMPT_TEXT_MISMATCH", "prompt text or identity differs from fresh Phase E"
            )

    @staticmethod
    def _validate_table_rows(rows: Sequence[Mapping[str, object]]) -> None:
        for row in rows:
            if tuple(row.keys()) != PROMPT_VERSION_FIELDS:
                raise PromptVersionLifecycleError(
                    "SCHEMA_MISMATCH", "prompt row fields differ from local schema"
                )

    @classmethod
    def _require_unique_ids_and_versions(cls, rows: Sequence[Mapping[str, object]]) -> None:
        ids: set[str] = set()
        versions: set[tuple[str, int]] = set()
        for row in rows:
            scene_prompt_id = str(row.get("Scene_Prompt_ID", ""))
            if not _valid_prompt_id(scene_prompt_id):
                raise PromptVersionLifecycleError(
                    "INVALID_PROMPT_ID", "persisted prompt ID is invalid"
                )
            if scene_prompt_id in ids:
                raise PromptVersionLifecycleError("DUPLICATE_PROMPT_ID", "prompt ID is duplicated")
            ids.add(scene_prompt_id)
            version_key = (
                str(row.get("Scene_ID", "")),
                cls._version_number(row.get("Prompt_Version")),
            )
            if version_key in versions:
                raise PromptVersionLifecycleError(
                    "DUPLICATE_PROMPT_VERSION", "scene prompt version is duplicated"
                )
            versions.add(version_key)

    @staticmethod
    def _find_one(rows: Sequence[Mapping[str, object]], scene_prompt_id: str) -> dict[str, object]:
        if not _valid_prompt_id(scene_prompt_id):
            raise PromptVersionLifecycleError("INVALID_PROMPT_ID", "prompt ID is invalid")
        matches = [dict(row) for row in rows if row.get("Scene_Prompt_ID") == scene_prompt_id]
        if len(matches) != 1:
            raise PromptVersionLifecycleError(
                "PROMPT_NOT_FOUND", "prompt ID is missing or duplicated"
            )
        if tuple(matches[0].keys()) != PROMPT_VERSION_FIELDS:
            raise PromptVersionLifecycleError(
                "SCHEMA_MISMATCH", "prompt row fields differ from local schema"
            )
        return matches[0]

    @staticmethod
    def _version_number(value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise PromptVersionLifecycleError("INVALID_PROMPT_VERSION", "prompt version is invalid")
        try:
            number = int(value)  # Sheets returns integers as numbers.
        except (TypeError, ValueError) as exc:
            raise PromptVersionLifecycleError(
                "INVALID_PROMPT_VERSION", "prompt version is invalid"
            ) from exc
        if number < 1 or str(value).strip() not in (str(number), f"{number}.0"):
            raise PromptVersionLifecycleError("INVALID_PROMPT_VERSION", "prompt version is invalid")
        return number


def _valid_prompt_id(value: str) -> bool:
    import re

    return re.fullmatch(r"SPV_[0-9]{6}", value) is not None


def _valid_sha256(value: str | None) -> bool:
    import re

    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None
