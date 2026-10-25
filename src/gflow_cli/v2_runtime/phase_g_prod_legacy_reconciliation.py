"""Read-only compatibility for legacy PROD attempts with provenance-bound identity joins."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import cast

from .phase_g_prod_reconciliation import (
    ProdAttemptIdentity,
    ProdReconciliationEvidence,
    ProdReconciliationResult,
    classify_prod_reconciliation,
    prod_evidence_from_attempt,
)

LEGACY_PROD_CONTRACT_VERSION = "D2_PROD_LEGACY_ATTEMPT_RECOVERY_V1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_AUDIT_SCHEMA_VERSION = 1
_AUDIT_LOCK = threading.RLock()
_SOURCE_FIELDS = {
    "production_jobs": ("PRODUCTION_JOBS", "Production_Job_ID"),
    "scene_jobs": ("SCENE_JOBS", "Scene_Job_ID"),
    "scenes": ("SCENES", "Scene_ID"),
    "scene_prompt_versions": ("SCENE_PROMPT_VERSION", "Scene_Prompt_ID"),
    "generation_profiles": ("GENERATION_PROFILE_MASTER", "Generation_Profile_ID"),
    "provider_accounts": ("PROVIDER_ACCOUNT_MASTER", "Provider_Account_ID"),
    "d2_attempts": ("D2_LEDGER", "d2_run_id"),
    "credit_reservations": ("CREDIT_LEDGER", "attempt_id"),
}
_ATTEMPT_EVIDENCE_FIELDS = (
    "generate_action_count",
    "approval_action_count",
    "submission_state",
    "baseline_captured",
    "baseline_pending_count",
    "baseline_execution_ids",
    "baseline_media_ids",
    "baseline_confirmation_ids",
    "provider_submit_rpc_observed",
    "provider_submit_rpc_id",
    "provider_submit_http_method",
    "provider_submit_observed_at",
    "provider_submit_request_count",
    "provider_submit_dispatch_window_completed",
    "provider_submit_project_id",
    "provider_submit_response_observed",
    "provider_submit_response_status",
    "provider_submit_generation_record_observed",
    "provider_submit_refusal_detected",
    "provider_submit_refusal_code",
    "provider_submit_request_failed",
    "provider_execution_id",
    "provider_media_id",
    "provider_media_ids",
    "confirmation_id",
    "observed_confirmation_id",
    "generate_action_at",
)


class ProdLegacyEvidenceClass(StrEnum):
    PERSISTED_DIRECT = "PERSISTED_DIRECT"
    DERIVED_CANONICAL_JOIN = "DERIVED_CANONICAL_JOIN"
    DIAGNOSTIC_ONLY = "DIAGNOSTIC_ONLY"
    UNAVAILABLE = "UNAVAILABLE"


class ProdLegacyJobDisposition(StrEnum):
    LEGACY_PREMATURE_RUNNING = "LEGACY_PREMATURE_RUNNING"
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True, slots=True)
class LegacySourceSnapshot:
    """Identity and observation metadata for one independently read source snapshot."""

    source: str
    sha256: str
    observed_at: str

    def __post_init__(self) -> None:
        if not self.source.strip() or not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("source snapshot identity or SHA-256 is invalid")
        _require_aware_timestamp(self.observed_at, "source snapshot observation time")


@dataclass(frozen=True, slots=True)
class LegacyProdAttemptSources:
    """Read-only copies of the canonical source rows needed to join one legacy attempt."""

    production_jobs: tuple[Mapping[str, object], ...]
    scene_jobs: tuple[Mapping[str, object], ...]
    scenes: tuple[Mapping[str, object], ...]
    scene_prompt_versions: tuple[Mapping[str, object], ...]
    generation_profiles: tuple[Mapping[str, object], ...]
    provider_accounts: tuple[Mapping[str, object], ...]
    d2_attempts: tuple[Mapping[str, object], ...]
    credit_reservations: tuple[Mapping[str, object], ...]
    source_snapshots: Mapping[str, LegacySourceSnapshot]
    runtime_namespace: str
    d2_ledger_path: str

    def with_rows(
        self, field_name: str, rows: tuple[Mapping[str, object], ...]
    ) -> LegacyProdAttemptSources:
        if field_name not in _SOURCE_FIELDS:
            raise ValueError("unknown legacy source row set")
        return replace(self, **{field_name: rows})

    def with_values(self, **values: object) -> LegacyProdAttemptSources:
        return replace(self, **values)


@dataclass(frozen=True, slots=True)
class LegacyEvidenceSource:
    source: str
    record_key: str
    snapshot_sha256: str
    snapshot_observed_at: str


@dataclass(frozen=True, slots=True)
class LegacyProdEvidenceField:
    value: object
    provenance_class: ProdLegacyEvidenceClass
    sources: tuple[LegacyEvidenceSource, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "value": _thaw_json(self.value),
            "provenance_class": self.provenance_class.value,
            "sources": [asdict(source) for source in self.sources],
        }


@dataclass(frozen=True, slots=True)
class LegacyProdAttemptEnvelope:
    identity: ProdAttemptIdentity | None
    identity_status: str
    reasons: tuple[str, ...]
    fields: tuple[tuple[str, LegacyProdEvidenceField], ...]
    attempt_record: Mapping[str, object]
    production_job_status: str | None
    scene_job_status: str | None
    job_disposition: ProdLegacyJobDisposition

    def field(self, name: str) -> LegacyProdEvidenceField:
        for field_name, evidence in self.fields:
            if field_name == name:
                return evidence
        raise KeyError(name)

    @property
    def sha256(self) -> str:
        encoded = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return {
            "identity": asdict(self.identity) if self.identity is not None else None,
            "identity_status": self.identity_status,
            "reasons": list(self.reasons),
            "fields": {name: evidence.as_dict() for name, evidence in self.fields},
            "production_job_status": self.production_job_status,
            "scene_job_status": self.scene_job_status,
            "job_disposition": self.job_disposition.value,
        }


@dataclass(frozen=True, slots=True)
class LegacyProdReconciliationDecision:
    envelope: LegacyProdAttemptEnvelope
    result: ProdReconciliationResult
    job_disposition: ProdLegacyJobDisposition
    audit_event: Mapping[str, object]


def build_legacy_prod_attempt_envelope(
    sources: LegacyProdAttemptSources, d2_run_id: str
) -> LegacyProdAttemptEnvelope:
    """Join legacy identity without modifying any source row or synthesizing event times."""
    reasons: list[str] = []
    fields: dict[str, LegacyProdEvidenceField] = {}
    for _, (snapshot_key, _) in _SOURCE_FIELDS.items():
        _snapshot(sources, snapshot_key, reasons)

    d2_rows = [row for row in sources.d2_attempts if row.get("d2_run_id") == d2_run_id]
    if len(d2_rows) != 1:
        reasons.append("D2_ATTEMPT_MISSING_OR_DUPLICATE")
        attempt: Mapping[str, object] = {}
    else:
        attempt = dict(d2_rows[0])

    scene_candidates = [row for row in sources.scene_jobs if _scene_job_run_id(row) == d2_run_id]
    if len(scene_candidates) != 1:
        reasons.append("SCENE_JOB_ATTEMPT_LINK_MISSING_OR_DUPLICATE")
        scene_job: Mapping[str, object] = {}
    else:
        scene_job = scene_candidates[0]

    production_id = _nonempty(scene_job.get("Production_Job_ID"))
    scene_job_id = _nonempty(scene_job.get("Scene_Job_ID"))
    prompt_id = _nonempty(scene_job.get("Scene_Prompt_ID"))
    production_job = _unique_row(
        sources.production_jobs, "Production_Job_ID", production_id, reasons, "PRODUCTION_JOB"
    )
    prompt = _unique_row(
        sources.scene_prompt_versions,
        "Scene_Prompt_ID",
        prompt_id,
        reasons,
        "SCENE_PROMPT",
    )
    scene_id = _nonempty(attempt.get("scene_id"))
    if _nonempty(prompt.get("Scene_ID")) != scene_id:
        reasons.append("SCENE_PROMPT_SCENE_MISMATCH")
    _unique_row(sources.scenes, "Scene_ID", scene_id, reasons, "SCENE")
    profile_id = _nonempty(attempt.get("generation_profile_id"))
    profile = _unique_row(
        sources.generation_profiles,
        "Generation_Profile_ID",
        profile_id,
        reasons,
        "GENERATION_PROFILE",
    )
    account_id = _nonempty(attempt.get("provider_account_id"))
    account = _unique_row(
        sources.provider_accounts,
        "Provider_Account_ID",
        account_id,
        reasons,
        "PROVIDER_ACCOUNT",
    )
    credit = _unique_row(
        sources.credit_reservations,
        "attempt_id",
        d2_run_id,
        reasons,
        "CREDIT_RESERVATION",
    )

    project_id = _nonempty(attempt.get("provider_project_id"))
    prompt_hash = _nonempty(attempt.get("prompt_sha256"))
    if not _SHA256_RE.fullmatch(prompt_hash):
        reasons.append("PROMPT_HASH_INVALID")
    if prompt.get("Prompt_SHA256") != prompt_hash:
        reasons.append("PROMPT_HASH_MISMATCH")
    if prompt.get("Prompt_Status") != "APPROVED":
        reasons.append("PROMPT_NOT_APPROVED")
    if _nonempty(prompt.get("Generation_Profile_ID")) != profile_id:
        reasons.append("PROMPT_PROFILE_MISMATCH")
    if _nonempty(profile.get("Provider_Account_ID")) != account_id:
        reasons.append("PROFILE_ACCOUNT_MISMATCH")
    if _nonempty(account.get("Provider_Project_ID")) != project_id:
        reasons.append("ACCOUNT_PROJECT_MISMATCH")

    env_sources = (
        _nonempty(production_job.get("Execution_Environment")),
        _nonempty(profile.get("Environment")),
        _nonempty(account.get("Environment")),
        _nonempty(credit.get("environment")),
        _nonempty(sources.runtime_namespace),
    )
    if not _production_d2_path(sources.d2_ledger_path):
        reasons.append("D2_LEDGER_NOT_IN_PRODUCTION_NAMESPACE")
    if any(environment != "PROD" for environment in env_sources):
        reasons.append("PROD_ENVIRONMENT_SOURCES_DISAGREE_OR_MISSING")
    if attempt.get("execution_environment") not in (None, "", "PROD"):
        reasons.append("PERSISTED_ENVIRONMENT_CONFLICT")
    if _nonempty(credit.get("provider_account_id")) != account_id:
        reasons.append("CREDIT_ACCOUNT_MISMATCH")
    if _nonempty(credit.get("provider_project_id")) != project_id:
        reasons.append("CREDIT_PROJECT_MISMATCH")
    if credit.get("production_job_id") not in (None, "", production_id):
        reasons.append("CREDIT_PRODUCTION_JOB_MISMATCH")
    if credit.get("scene_job_id") not in (None, "", scene_job_id):
        reasons.append("CREDIT_SCENE_JOB_MISMATCH")
    if _nonempty(attempt.get("scene_prompt_id")) != prompt_id:
        reasons.append("D2_PROMPT_MISMATCH")
    if _nonempty(scene_job.get("Provider_Execution_ID")) and _nonempty(
        scene_job.get("Provider_Execution_ID")
    ) != _nonempty(attempt.get("provider_execution_id")):
        reasons.append("SCENE_JOB_EXECUTION_MISMATCH")

    _add_field(
        fields,
        "attempt_id",
        d2_run_id if len(d2_rows) == 1 else None,
        ProdLegacyEvidenceClass.PERSISTED_DIRECT
        if len(d2_rows) == 1
        else ProdLegacyEvidenceClass.UNAVAILABLE,
        sources,
        "D2_LEDGER",
        f"attempts[d2_run_id={d2_run_id}].d2_run_id",
    )
    _add_field(
        fields,
        "production_job_id",
        production_id or None,
        ProdLegacyEvidenceClass.DERIVED_CANONICAL_JOIN,
        sources,
        "SCENE_JOBS",
        f"Scene_Job_ID={scene_job_id}.Production_Job_ID",
        corroborating=(
            ("PRODUCTION_JOBS", f"Production_Job_ID={production_id}.Production_Job_ID"),
        ),
    )
    _add_field(
        fields,
        "scene_job_id",
        scene_job_id or None,
        ProdLegacyEvidenceClass.DERIVED_CANONICAL_JOIN,
        sources,
        "SCENE_JOBS",
        f"Scene_Job_ID={scene_job_id}.Scene_Job_ID+Attempt_No={scene_job.get('Attempt_No')}",
    )
    _add_field(
        fields,
        "scene_id",
        scene_id or None,
        ProdLegacyEvidenceClass.PERSISTED_DIRECT,
        sources,
        "D2_LEDGER",
        f"attempts[d2_run_id={d2_run_id}].scene_id",
        corroborating=(
            ("SCENE_PROMPT_VERSION", f"Scene_Prompt_ID={prompt_id}.Scene_ID"),
            ("SCENES", f"Scene_ID={scene_id}.Scene_ID"),
        ),
    )
    _add_field(
        fields,
        "prompt_id",
        prompt_id or None,
        ProdLegacyEvidenceClass.DERIVED_CANONICAL_JOIN,
        sources,
        "SCENE_JOBS",
        f"Scene_Job_ID={scene_job_id}.Scene_Prompt_ID",
        corroborating=(("SCENE_PROMPT_VERSION", f"Scene_Prompt_ID={prompt_id}.Scene_Prompt_ID"),),
    )
    _add_field(
        fields,
        "prompt_sha256",
        prompt_hash or None,
        ProdLegacyEvidenceClass.PERSISTED_DIRECT,
        sources,
        "D2_LEDGER",
        f"attempts[d2_run_id={d2_run_id}].prompt_sha256",
        corroborating=(("SCENE_PROMPT_VERSION", f"Scene_Prompt_ID={prompt_id}.Prompt_SHA256"),),
    )
    _add_field(
        fields,
        "generation_profile_id",
        profile_id or None,
        ProdLegacyEvidenceClass.PERSISTED_DIRECT,
        sources,
        "D2_LEDGER",
        f"attempts[d2_run_id={d2_run_id}].generation_profile_id",
        corroborating=(
            ("SCENE_PROMPT_VERSION", f"Scene_Prompt_ID={prompt_id}.Generation_Profile_ID"),
            (
                "GENERATION_PROFILE_MASTER",
                f"Generation_Profile_ID={profile_id}.Generation_Profile_ID",
            ),
        ),
    )
    _add_field(
        fields,
        "provider_account_id",
        account_id or None,
        ProdLegacyEvidenceClass.PERSISTED_DIRECT,
        sources,
        "D2_LEDGER",
        f"attempts[d2_run_id={d2_run_id}].provider_account_id",
        corroborating=(
            (
                "GENERATION_PROFILE_MASTER",
                f"Generation_Profile_ID={profile_id}.Provider_Account_ID",
            ),
            ("PROVIDER_ACCOUNT_MASTER", f"Provider_Account_ID={account_id}.Provider_Account_ID"),
        ),
    )
    _add_field(
        fields,
        "flow_project_id",
        project_id or None,
        ProdLegacyEvidenceClass.PERSISTED_DIRECT,
        sources,
        "D2_LEDGER",
        f"attempts[d2_run_id={d2_run_id}].provider_project_id",
        corroborating=(
            ("PROVIDER_ACCOUNT_MASTER", f"Provider_Account_ID={account_id}.Provider_Project_ID"),
        ),
    )
    environment_sources = tuple(
        source
        for source in (
            _source_ref(
                sources,
                "PRODUCTION_JOBS",
                f"Production_Job_ID={production_id}.Execution_Environment",
            ),
            _source_ref(
                sources,
                "GENERATION_PROFILE_MASTER",
                f"Generation_Profile_ID={profile_id}.Environment",
            ),
            _source_ref(
                sources,
                "PROVIDER_ACCOUNT_MASTER",
                f"Provider_Account_ID={account_id}.Environment+Provider_Project_ID",
            ),
            _source_ref(
                sources,
                "CREDIT_LEDGER",
                f"attempt_id={d2_run_id}.environment+provider_account_id+provider_project_id",
            ),
            _source_ref(sources, "D2_LEDGER", sources.d2_ledger_path),
        )
        if source is not None
    )
    fields["execution_environment"] = LegacyProdEvidenceField(
        "PROD"
        if not any(
            reason.startswith("PROD_") or reason == "D2_LEDGER_NOT_IN_PRODUCTION_NAMESPACE"
            for reason in reasons
        )
        else None,
        ProdLegacyEvidenceClass.DERIVED_CANONICAL_JOIN
        if not any(
            reason.startswith("PROD_") or reason == "D2_LEDGER_NOT_IN_PRODUCTION_NAMESPACE"
            for reason in reasons
        )
        else ProdLegacyEvidenceClass.UNAVAILABLE,
        environment_sources,
    )

    for field_name in _ATTEMPT_EVIDENCE_FIELDS:
        value = attempt.get(field_name)
        if field_name == "generate_action_at" and not isinstance(value, str):
            _add_field(
                fields,
                field_name,
                None,
                ProdLegacyEvidenceClass.UNAVAILABLE,
                sources,
                "D2_LEDGER",
                f"attempts[d2_run_id={d2_run_id}].{field_name} (absent)",
            )
        else:
            _add_field(
                fields,
                field_name,
                value,
                ProdLegacyEvidenceClass.PERSISTED_DIRECT
                if field_name in attempt
                else ProdLegacyEvidenceClass.UNAVAILABLE,
                sources,
                "D2_LEDGER",
                f"attempts[d2_run_id={d2_run_id}].{field_name}",
            )
    _add_field(
        fields,
        "credit_reservation_state",
        credit.get("state"),
        ProdLegacyEvidenceClass.PERSISTED_DIRECT,
        sources,
        "CREDIT_LEDGER",
        f"attempt_id={d2_run_id}.state",
    )
    _add_field(
        fields,
        "reserved_credits",
        credit.get("reserved_credits"),
        ProdLegacyEvidenceClass.PERSISTED_DIRECT,
        sources,
        "CREDIT_LEDGER",
        f"attempt_id={d2_run_id}.reserved_credits",
    )
    _add_field(
        fields,
        "production_job_status",
        production_job.get("Job_Status"),
        ProdLegacyEvidenceClass.DIAGNOSTIC_ONLY,
        sources,
        "PRODUCTION_JOBS",
        f"Production_Job_ID={production_id}.Job_Status",
    )
    _add_field(
        fields,
        "scene_job_status",
        scene_job.get("Job_Status"),
        ProdLegacyEvidenceClass.DIAGNOSTIC_ONLY,
        sources,
        "SCENE_JOBS",
        f"Scene_Job_ID={scene_job_id}.Job_Status",
    )
    _add_field(
        fields,
        "scene_job_provider_execution_id",
        _nonempty(scene_job.get("Provider_Execution_ID")) or None,
        ProdLegacyEvidenceClass.PERSISTED_DIRECT
        if _nonempty(scene_job.get("Provider_Execution_ID"))
        else ProdLegacyEvidenceClass.UNAVAILABLE,
        sources,
        "SCENE_JOBS",
        f"Scene_Job_ID={scene_job_id}.Provider_Execution_ID",
    )
    _add_field(
        fields,
        "production_job_updated_at",
        production_job.get("Updated_At"),
        ProdLegacyEvidenceClass.DIAGNOSTIC_ONLY,
        sources,
        "PRODUCTION_JOBS",
        f"Production_Job_ID={production_id}.Updated_At",
    )
    _add_field(
        fields,
        "scene_job_updated_at",
        scene_job.get("Updated_At"),
        ProdLegacyEvidenceClass.DIAGNOSTIC_ONLY,
        sources,
        "SCENE_JOBS",
        f"Scene_Job_ID={scene_job_id}.Updated_At",
    )
    _add_field(
        fields,
        "attempt_updated_at",
        attempt.get("updated_at"),
        ProdLegacyEvidenceClass.DIAGNOSTIC_ONLY,
        sources,
        "D2_LEDGER",
        f"attempts[d2_run_id={d2_run_id}].updated_at",
    )

    job_status = _nonempty(production_job.get("Job_Status"))
    scene_status = _nonempty(scene_job.get("Job_Status"))
    execution_present = bool(
        _nonempty(attempt.get("provider_execution_id"))
        or _nonempty(scene_job.get("Provider_Execution_ID"))
    )
    disposition = (
        ProdLegacyJobDisposition.LEGACY_PREMATURE_RUNNING
        if job_status == "RUNNING" and scene_status == "RUNNING" and not execution_present
        else ProdLegacyJobDisposition.UNCHANGED
    )

    identity: ProdAttemptIdentity | None = None
    if not reasons:
        try:
            identity = ProdAttemptIdentity(
                production_job_id=production_id,
                scene_job_id=scene_job_id,
                d2_run_id=d2_run_id,
                provider_account_id=account_id,
                provider_project_id=project_id,
                scene_id=scene_id,
                scene_prompt_id=prompt_id,
                generation_profile_id=profile_id,
                prompt_sha256=prompt_hash,
            )
        except ValueError:
            reasons.append("LEGACY_IDENTITY_INVALID")
    status = (
        "IDENTITY_VALIDATED"
        if identity is not None and not reasons
        else "LEGACY_IDENTITY_AMBIGUOUS"
    )
    return LegacyProdAttemptEnvelope(
        identity=identity,
        identity_status=status,
        reasons=tuple(dict.fromkeys(reasons)),
        fields=tuple(fields.items()),
        attempt_record=cast(Mapping[str, object], _freeze_json(attempt)),
        production_job_status=job_status or None,
        scene_job_status=scene_status or None,
        job_disposition=disposition,
    )


def reconcile_legacy_prod_same_attempt(
    sources: LegacyProdAttemptSources,
    d2_run_id: str,
    *,
    observed_at: str,
    software_commit: str,
    evidence: ProdReconciliationEvidence | None = None,
) -> LegacyProdReconciliationDecision:
    """Classify a verified legacy envelope and create a separate audit event model."""
    _require_aware_timestamp(observed_at, "reconciliation observation time")
    if not _COMMIT_RE.fullmatch(software_commit):
        raise ValueError("software commit must be a full hexadecimal commit ID")
    envelope = build_legacy_prod_attempt_envelope(sources, d2_run_id)
    result = _classify_legacy_envelope(envelope, evidence)
    disposition = envelope.job_disposition
    if result in {
        ProdReconciliationResult.SUBMISSION_PROVEN_EXECUTION_FOUND,
        ProdReconciliationResult.OUTPUT_ALREADY_EXISTS,
    }:
        disposition = ProdLegacyJobDisposition.UNCHANGED
        envelope = replace(envelope, job_disposition=disposition)
    event: dict[str, object] = {
        "contract_version": LEGACY_PROD_CONTRACT_VERSION,
        "legacy_attempt_id": d2_run_id,
        "envelope_sha256": envelope.sha256,
        "evidence_provenance": {name: field.as_dict() for name, field in envelope.fields},
        "result": result.value,
        "mutation_performed": False,
        "reservation_action": "UNCHANGED",
        "job_disposition": disposition.value,
        "historical_event_time": (
            envelope.field("generate_action_at").value
            if envelope.field("generate_action_at").provenance_class
            is ProdLegacyEvidenceClass.PERSISTED_DIRECT
            else None
        ),
        "reconciliation_observed_at": observed_at,
        "software_commit": software_commit,
    }
    event["event_sha256"] = _digest(event)
    return LegacyProdReconciliationDecision(envelope, result, disposition, event)


def write_legacy_prod_reconciliation_audit(
    runtime_state_root: Path, audit_event: Mapping[str, object]
) -> dict[str, object]:
    """Atomically append one idempotent event to a separate audit file, never the D2 row."""
    if not runtime_state_root.is_absolute() or runtime_state_root == Path(
        runtime_state_root.anchor
    ):
        raise ValueError("runtime state root must be absolute and non-root")
    if runtime_state_root.is_symlink():
        raise ValueError("runtime state root may not be a symlink")
    event = dict(audit_event)
    event_digest = event.get("event_sha256")
    expected = _digest({key: value for key, value in event.items() if key != "event_sha256"})
    if event.get("contract_version") != LEGACY_PROD_CONTRACT_VERSION or event_digest != expected:
        raise ValueError("legacy reconciliation audit event is invalid")
    root = runtime_state_root / "phase-g"
    path = root / "prod-legacy-reconciliation-audit.json"
    lock_path = root / ".prod-legacy-reconciliation-audit.lock"
    with _LegacyAuditLock(root, lock_path):
        if root.is_symlink() or path.is_symlink() or lock_path.is_symlink():
            raise ValueError("legacy reconciliation audit path may not be a symlink")
        payload = _read_audit(path)
        events = cast(list[dict[str, object]], payload["events"])
        idempotency_key = event.get("event_idempotency_key")
        if isinstance(idempotency_key, str):
            prior_by_key = next(
                (row for row in events if row.get("event_idempotency_key") == idempotency_key),
                None,
            )
            if prior_by_key is not None:
                if (
                    prior_by_key.get("legacy_attempt_id") != event.get("legacy_attempt_id")
                    or prior_by_key.get("classification_event_sha256")
                    != event.get("classification_event_sha256")
                    or prior_by_key.get("reservation_action") != event.get("reservation_action")
                ):
                    raise ValueError("legacy reconciliation audit idempotency conflict")
                return dict(prior_by_key)
        previous = next((row for row in events if row.get("event_sha256") == event_digest), None)
        if previous is not None:
            if previous != event:
                raise ValueError("legacy reconciliation audit digest conflict")
            return dict(previous)
        events.append(event)
        _write_audit(path, root, payload)
        return dict(event)


def find_legacy_prod_reconciliation_audit(
    runtime_state_root: Path, event_sha256: str
) -> dict[str, object] | None:
    """Read one exact event from the separate legacy audit file."""
    if not runtime_state_root.is_absolute() or runtime_state_root == Path(
        runtime_state_root.anchor
    ):
        raise ValueError("runtime state root must be absolute and non-root")
    path = runtime_state_root / "phase-g" / "prod-legacy-reconciliation-audit.json"
    if path.is_symlink():
        raise ValueError("legacy reconciliation audit path may not be a symlink")
    payload = _read_audit(path)
    events = cast(list[dict[str, object]], payload["events"])
    matches = [row for row in events if row.get("event_sha256") == event_sha256]
    if len(matches) > 1:
        raise ValueError("legacy reconciliation audit event is duplicated")
    if not matches:
        return None
    event = matches[0]
    expected = _digest({key: value for key, value in event.items() if key != "event_sha256"})
    if expected != event_sha256:
        raise ValueError("legacy reconciliation audit event digest is invalid")
    return dict(event)


def legacy_reservation_release_audit_event(
    classification_event: Mapping[str, object],
    *,
    released_at: str,
    software_commit: str,
) -> dict[str, object]:
    """Build a separate post-mutation audit event linked to its proof classification."""
    _require_aware_timestamp(released_at, "reservation release time")
    if not _COMMIT_RE.fullmatch(software_commit):
        raise ValueError("software commit must be a full hexadecimal commit ID")
    if (
        classification_event.get("contract_version") != LEGACY_PROD_CONTRACT_VERSION
        or classification_event.get("result") != ProdReconciliationResult.PROVEN_NOT_SUBMITTED.value
        or not isinstance(classification_event.get("event_sha256"), str)
    ):
        raise ValueError("a positive legacy non-submission event is required")
    event: dict[str, object] = {
        "contract_version": LEGACY_PROD_CONTRACT_VERSION,
        "event_type": "RESERVATION_RELEASE",
        "legacy_attempt_id": classification_event.get("legacy_attempt_id"),
        "envelope_sha256": classification_event.get("envelope_sha256"),
        "evidence_provenance": classification_event.get("evidence_provenance"),
        "result": ProdReconciliationResult.PROVEN_NOT_SUBMITTED.value,
        "mutation_performed": True,
        "reservation_action": "RELEASED",
        "event_idempotency_key": f"reservation-release:{classification_event['event_sha256']}",
        "classification_event_sha256": classification_event["event_sha256"],
        "historical_event_time": classification_event.get("historical_event_time"),
        "reconciliation_observed_at": released_at,
        "software_commit": software_commit,
    }
    event["event_sha256"] = _digest(event)
    return event


def _classify_legacy_envelope(
    envelope: LegacyProdAttemptEnvelope,
    evidence: ProdReconciliationEvidence | None,
) -> ProdReconciliationResult:
    identity = envelope.identity
    attempt = cast(dict[str, object], _thaw_json(envelope.attempt_record))
    if identity is None or envelope.identity_status != "IDENTITY_VALIDATED":
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    if (
        type(attempt.get("generate_action_count")) is not int
        or attempt.get("generate_action_count") != 1
        or type(attempt.get("approval_action_count")) is not int
        or attempt.get("approval_action_count") != 0
        or attempt.get("baseline_captured") is not True
        or attempt.get("submission_state")
        not in {
            "UNKNOWN_SUBMISSION_STATE",
            "SUBMISSION_ACKNOWLEDGED",
            "GENERATION_PENDING",
            "GENERATION_SUCCEEDED",
            "D2_SUCCEEDED",
            "OUTPUT_ALREADY_EXISTS",
        }
    ):
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    if evidence is not None:
        if evidence.identity != identity:
            return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
        if envelope.field("generate_action_at").provenance_class is not (
            ProdLegacyEvidenceClass.PERSISTED_DIRECT
        ):
            return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
        modern_attempt = _modern_view(envelope)
        return classify_prod_reconciliation(identity, evidence, persisted_attempt=modern_attempt)

    persisted = prod_evidence_from_attempt(identity, attempt)
    if persisted is None:
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    baseline_execution_ids = _string_tuple(attempt.get("baseline_execution_ids"))
    baseline_media_ids = _string_tuple(attempt.get("baseline_media_ids"))
    baseline_confirmation_ids = _string_tuple(attempt.get("baseline_confirmation_ids"))
    if (
        baseline_execution_ids is None
        or baseline_media_ids is None
        or baseline_confirmation_ids is None
        or attempt.get("baseline_pending_count") != 0
    ):
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    scene_job_execution_id = envelope.field("scene_job_provider_execution_id").value
    if isinstance(scene_job_execution_id, str) and scene_job_execution_id:
        if scene_job_execution_id in baseline_execution_ids:
            return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
        if persisted.provider_execution_id not in {None, scene_job_execution_id}:
            return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
        return ProdReconciliationResult.SUBMISSION_PROVEN_EXECUTION_FOUND
    if persisted.provider_submit_refusal_code is not None:
        if envelope.field("generate_action_at").provenance_class is not (
            ProdLegacyEvidenceClass.PERSISTED_DIRECT
        ):
            return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
        return classify_prod_reconciliation(
            identity, persisted, persisted_attempt=_modern_view(envelope)
        )
    if persisted.provider_submit_project_id not in {None, identity.provider_project_id}:
        return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
    if persisted.provider_execution_id:
        if (
            persisted.provider_execution_id in baseline_execution_ids
            or persisted.submission_state
            not in {
                "SUBMISSION_ACKNOWLEDGED",
                "GENERATION_PENDING",
                "GENERATION_SUCCEEDED",
                "D2_SUCCEEDED",
            }
        ):
            return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
        return (
            ProdReconciliationResult.SUBMISSION_PROVEN_PENDING
            if persisted.submission_state == "GENERATION_PENDING"
            else ProdReconciliationResult.SUBMISSION_PROVEN_EXECUTION_FOUND
        )
    if persisted.provider_media_ids:
        if (
            len(persisted.provider_media_ids) != 1
            or persisted.provider_media_ids[0] in baseline_media_ids
            or persisted.submission_state
            not in {"GENERATION_SUCCEEDED", "D2_SUCCEEDED", "OUTPUT_ALREADY_EXISTS"}
        ):
            return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
        return ProdReconciliationResult.OUTPUT_ALREADY_EXISTS
    if persisted.confirmation_id:
        if persisted.confirmation_id in baseline_confirmation_ids:
            return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
        if envelope.field("generate_action_at").provenance_class is not (
            ProdLegacyEvidenceClass.PERSISTED_DIRECT
        ):
            return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
        return classify_prod_reconciliation(
            identity, persisted, persisted_attempt=_modern_view(envelope)
        )
    if (
        persisted.provider_submit_response_observed
        and persisted.provider_submit_generation_record_observed
        and persisted.provider_submit_response_status is not None
        and 200 <= persisted.provider_submit_response_status < 300
        and persisted.provider_submit_request_count == 1
        and persisted.provider_submit_dispatch_window_completed
        and persisted.provider_submit_project_id == identity.provider_project_id
    ):
        if envelope.field("generate_action_at").provenance_class is not (
            ProdLegacyEvidenceClass.PERSISTED_DIRECT
        ):
            return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION
        return classify_prod_reconciliation(
            identity, persisted, persisted_attempt=_modern_view(envelope)
        )
    return ProdReconciliationResult.AMBIGUOUS_AFTER_RECONCILIATION


def _modern_view(envelope: LegacyProdAttemptEnvelope) -> dict[str, object]:
    """Build an ephemeral classifier view; never persist derived values into source rows."""
    attempt = cast(dict[str, object], _thaw_json(envelope.attempt_record))
    identity = envelope.identity
    assert identity is not None
    attempt.update(
        {
            "production_job_id": identity.production_job_id,
            "scene_job_id": identity.scene_job_id,
            "d2_run_id": identity.d2_run_id,
            "provider_account_id": identity.provider_account_id,
            "provider_project_id": identity.provider_project_id,
            "scene_id": identity.scene_id,
            "scene_prompt_id": identity.scene_prompt_id,
            "generation_profile_id": identity.generation_profile_id,
            "prompt_sha256": identity.prompt_sha256,
            "execution_environment": "PROD",
        }
    )
    return attempt


def _scene_job_run_id(row: Mapping[str, object]) -> str | None:
    scene_job_id = _nonempty(row.get("Scene_Job_ID"))
    attempt_no = row.get("Attempt_No")
    if not re.fullmatch(r"SJB_[0-9]{6}", scene_job_id):
        return None
    if isinstance(attempt_no, bool):
        return None
    if isinstance(attempt_no, str) and re.fullmatch(r"[0-9]+", attempt_no.strip()):
        attempt_no = int(attempt_no.strip())
    if not isinstance(attempt_no, int) or attempt_no < 1:
        return None
    value = f"PRODUCTION_SCENE_D2_V1:{scene_job_id}:{attempt_no}"
    return f"D2_{hashlib.sha256(value.encode('utf-8')).hexdigest()[:32]}"


def _unique_row(
    rows: Sequence[Mapping[str, object]],
    key: str,
    value: str,
    reasons: list[str],
    label: str,
) -> Mapping[str, object]:
    if not value:
        reasons.append(f"{label}_LINK_MISSING")
        return {}
    matches = [row for row in rows if row.get(key) == value]
    if len(matches) != 1:
        reasons.append(f"{label}_MISSING_OR_DUPLICATE")
        return {}
    return matches[0]


def _add_field(
    fields: dict[str, LegacyProdEvidenceField],
    name: str,
    value: object,
    provenance_class: ProdLegacyEvidenceClass,
    sources: LegacyProdAttemptSources,
    source_name: str,
    record_key: str,
    *,
    corroborating: tuple[tuple[str, str], ...] = (),
) -> None:
    source_refs = tuple(
        source
        for source in (
            _source_ref(sources, source_name, record_key),
            *(
                _source_ref(sources, other_name, other_key)
                for other_name, other_key in corroborating
            ),
        )
        if source is not None
    )
    fields[name] = LegacyProdEvidenceField(_freeze_json(value), provenance_class, source_refs)


def _source_ref(
    sources: LegacyProdAttemptSources, source_name: str, record_key: str
) -> LegacyEvidenceSource | None:
    snapshot = sources.source_snapshots.get(source_name)
    if snapshot is None:
        return None
    return LegacyEvidenceSource(
        source=snapshot.source,
        record_key=record_key,
        snapshot_sha256=snapshot.sha256,
        snapshot_observed_at=snapshot.observed_at,
    )


def _snapshot(
    sources: LegacyProdAttemptSources, source_name: str, reasons: list[str]
) -> LegacySourceSnapshot | None:
    snapshot = sources.source_snapshots.get(source_name)
    if snapshot is None:
        reasons.append(f"SOURCE_SNAPSHOT_MISSING:{source_name}")
        return None
    if snapshot.source != source_name:
        reasons.append(f"SOURCE_SNAPSHOT_NAME_MISMATCH:{source_name}")
    return snapshot


def _production_d2_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower().split("/")
    return (
        len(normalized) >= 3
        and "production" in normalized[:-2]
        and normalized[-2:]
        == [
            "phase-g",
            "d2-credit-ledger.json",
        ]
    )


def _nonempty(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _string_tuple(value: object) -> tuple[str, ...] | None:
    if isinstance(value, list):
        raw_values = cast(list[object], value)
    elif isinstance(value, tuple):
        raw_values = cast(tuple[object, ...], value)
    else:
        return None
    values: tuple[object, ...] = tuple(raw_values)
    if any(not isinstance(item, str) or not item for item in values):
        return None
    return tuple(item for item in values if isinstance(item, str))


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        source = cast(Mapping[object, object], value)
        return MappingProxyType({str(key): _freeze_json(item) for key, item in source.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in cast(list[object], value))
    if isinstance(value, tuple):
        return tuple(_freeze_json(item) for item in cast(tuple[object, ...], value))
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        source = cast(Mapping[object, object], value)
        return {str(key): _thaw_json(item) for key, item in source.items()}
    if isinstance(value, list):
        return [_thaw_json(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in cast(tuple[object, ...], value)]
    return value


def _require_aware_timestamp(value: str, description: str) -> datetime:
    if not value:
        raise ValueError(f"{description} is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{description} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{description} must include a timezone")
    return parsed


def _digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _read_audit(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"schema_version": _AUDIT_SCHEMA_VERSION, "events": []}
    try:
        value: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("legacy reconciliation audit is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError("legacy reconciliation audit has an invalid shape")
    payload = cast(dict[str, object], value)
    if payload.get("schema_version") != _AUDIT_SCHEMA_VERSION:
        raise ValueError("legacy reconciliation audit has an invalid shape")
    raw_events: object = payload.get("events")
    if not isinstance(raw_events, list):
        raise ValueError("legacy reconciliation audit has an invalid shape")
    events: list[object] = cast(list[object], raw_events)
    if any(not isinstance(item, dict) for item in events):
        raise ValueError("legacy reconciliation audit has an invalid shape")
    return payload


def _write_audit(path: Path, root: Path, payload: Mapping[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve(strict=True)
    if path.resolve(strict=False).parent != resolved_root:
        raise ValueError("legacy reconciliation audit path escaped its root")
    fd, temp_name = tempfile.mkstemp(prefix=".legacy-prod-reconciliation-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class _LegacyAuditLock:
    def __init__(self, root: Path, lock_path: Path) -> None:
        self.root = root
        self.lock_path = lock_path
        self.fd: int | None = None

    def __enter__(self) -> _LegacyAuditLock:
        with _AUDIT_LOCK:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink() or self.lock_path.is_symlink():
                raise ValueError("legacy reconciliation audit lock may not be a symlink")
            self.fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        if os.name == "nt":
            import msvcrt

            if os.fstat(self.fd).st_size == 0:
                os.write(self.fd, b"0")
            os.lseek(self.fd, 0, os.SEEK_SET)
            msvcrt.locking(self.fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(cast(int, self.fd), fcntl.LOCK_EX)
        return self

    def __exit__(self, *_args: object) -> None:
        if self.fd is not None:
            if os.name == "nt":
                import msvcrt

                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
