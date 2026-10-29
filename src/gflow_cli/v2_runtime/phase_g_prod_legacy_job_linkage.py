from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import cast

from .ambiguous_legacy_recovery_policy import ambiguous_legacy_policy_id
from .phase_g_prod_legacy_reconciliation import (
    LEGACY_PROD_CONTRACT_VERSION,
    LegacyProdAttemptSources,
    ProdLegacyEvidenceClass,
    build_legacy_prod_attempt_envelope,
    scene_job_run_id,
)

REPAIR_CONTRACT_VERSION = "D2_PROD_LEGACY_JOB_LINKAGE_REPAIR_V1"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40,64}$")
_SNAPSHOT_SOURCES = {
    "D2_LEDGER",
    "CREDIT_LEDGER",
    "PRODUCTION_JOBS",
    "SCENE_JOBS",
    "SCENES",
    "SCENE_PROMPT_VERSION",
    "GENERATION_PROFILE_MASTER",
    "PROVIDER_ACCOUNT_MASTER",
}


class LegacyJobLinkageProofError(ValueError):
    """A canonical legacy job-linkage proof failed closed."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code


def build_legacy_job_linkage_repair_proof(
    sources: LegacyProdAttemptSources,
    *,
    attempt_id: str,
    production_job_id: str,
    scene_job_id: str,
    policy_event: Mapping[str, object],
    reconciliation_event: Mapping[str, object],
    verified_at: str,
    software_commit: str,
) -> dict[str, object]:
    """Build a proof only when canonical rows and durable prior audits agree uniquely."""
    _require_aware_timestamp(verified_at, "verification time")
    if not _COMMIT_RE.fullmatch(software_commit):
        raise LegacyJobLinkageProofError("INVALID_PROOF", "running software commit is invalid")
    attempt_rows = [row for row in sources.d2_attempts if row.get("d2_run_id") == attempt_id]
    scene_candidates = [row for row in sources.scene_jobs if scene_job_run_id(row) == attempt_id]
    if len(attempt_rows) != 1 or len(scene_candidates) != 1:
        raise LegacyJobLinkageProofError(
            "AMBIGUOUS", "D2 attempt or deterministic scene-job candidate is not unique"
        )
    attempt = dict(attempt_rows[0])
    scene_job = scene_candidates[0]
    if attempt.get("production_job_id") not in (None, "", production_job_id) or attempt.get(
        "scene_job_id"
    ) not in (None, "", scene_job_id):
        raise LegacyJobLinkageProofError(
            "CONFLICTING", "legacy D2 row already contains conflicting job linkage"
        )
    envelope = build_legacy_prod_attempt_envelope(sources, attempt_id)
    identity = envelope.identity
    if (
        not envelope.recovery_prerequisite_satisfied
        or identity is None
        or identity.d2_run_id != attempt_id
        or identity.production_job_id != production_job_id
        or identity.scene_job_id != scene_job_id
    ):
        raise LegacyJobLinkageProofError(
            "CONFLICTING", "canonical legacy recovery envelope does not prove the requested jobs"
        )

    production_rows = [
        row for row in sources.production_jobs if row.get("Production_Job_ID") == production_job_id
    ]
    reservation_rows = [
        row for row in sources.credit_reservations if row.get("attempt_id") == attempt_id
    ]
    scene_rows = [row for row in sources.scene_jobs if row.get("Scene_Job_ID") == scene_job_id]
    if len(production_rows) != 1 or len(reservation_rows) != 1 or len(scene_rows) != 1:
        raise LegacyJobLinkageProofError(
            "AMBIGUOUS", "exact canonical production job, scene job, or reservation is not unique"
        )
    production = production_rows[0]
    reservation = reservation_rows[0]
    credit_amount = _credit_amount(reservation.get("reserved_credits"))
    if (
        reservation.get("state") != "UNKNOWN_SUBMISSION_STATE"
        or credit_amount != Decimal("50")
        or reservation.get("environment") != "PROD"
        or reservation.get("provider_account_id") != identity.provider_account_id
        or reservation.get("provider_project_id") != identity.provider_project_id
        or reservation.get("generate_click_count") != 1
        or production.get("Job_Status") != "QUARANTINED"
        or scene_job.get("Job_Status") != "QUARANTINED"
    ):
        raise LegacyJobLinkageProofError(
            "CONFLICTING", "reservation or quarantined canonical job state differs"
        )

    _validate_policy_event(
        policy_event,
        attempt_id=attempt_id,
        production_job_id=production_job_id,
        scene_job_id=scene_job_id,
    )
    reconciliation_sha256 = _validate_reconciliation_event(
        reconciliation_event,
        attempt_id=attempt_id,
        production_job_id=production_job_id,
        scene_job_id=scene_job_id,
        scene_id=identity.scene_id,
        prompt_id=identity.scene_prompt_id,
        prompt_sha256=identity.prompt_sha256,
        profile_id=identity.generation_profile_id,
        account_id=identity.provider_account_id,
        project_id=identity.provider_project_id,
    )
    policy = _as_mapping(policy_event.get("policy"))
    policy_sha256 = str(policy_event.get("event_sha256", ""))
    if policy.get("reconciliation_audit_sha256") != reconciliation_sha256:
        raise LegacyJobLinkageProofError(
            "CONFLICTING", "applied policy and reconciliation audit links disagree"
        )

    timestamp_compatible = _timestamps_compatible(attempt, production, scene_job)
    if not timestamp_compatible:
        raise LegacyJobLinkageProofError(
            "CONFLICTING", "persisted job and attempt timestamps are incompatible"
        )

    snapshot_hashes: dict[str, str] = {}
    snapshot_times: dict[str, str] = {}
    for name, snapshot in sources.source_snapshots.items():
        snapshot_hashes[name] = snapshot.sha256
        snapshot_times[name] = snapshot.observed_at
    if set(snapshot_hashes) != _SNAPSHOT_SOURCES:
        raise LegacyJobLinkageProofError(
            "INVALID_PROOF", "the independent legacy source snapshot set is incomplete"
        )

    relevant_rows = {
        "D2_ATTEMPT": _without_job_linkage(attempt),
        "CREDIT_RESERVATION": dict(reservation),
        "PRODUCTION_JOB": dict(production),
        "SCENE_JOB": dict(scene_job),
        "SCENE": _unique(sources.scenes, "Scene_ID", identity.scene_id),
        "PROMPT_VERSION": _unique(
            sources.scene_prompt_versions, "Scene_Prompt_ID", identity.scene_prompt_id
        ),
        "GENERATION_PROFILE": _unique(
            sources.generation_profiles, "Generation_Profile_ID", identity.generation_profile_id
        ),
        "PROVIDER_ACCOUNT": _unique(
            sources.provider_accounts, "Provider_Account_ID", identity.provider_account_id
        ),
    }
    correlations = {
        "SCENE_ID_MATCH": identity.scene_id == attempt.get("scene_id"),
        "PROMPT_ID_MATCH": identity.scene_prompt_id == attempt.get("scene_prompt_id"),
        "PROMPT_HASH_MATCH": identity.prompt_sha256 == attempt.get("prompt_sha256"),
        "GENERATION_PROFILE_MATCH": identity.generation_profile_id
        == attempt.get("generation_profile_id"),
        "PROVIDER_ACCOUNT_MATCH": identity.provider_account_id
        == attempt.get("provider_account_id"),
        "FLOW_PROJECT_MATCH": identity.provider_project_id == attempt.get("provider_project_id"),
        "ENVIRONMENT_MATCH": (
            production.get("Execution_Environment") == "PROD"
            and sources.runtime_namespace == "PROD"
            and sources.d2_ledger_path.replace("\\", "/").startswith("/var/lib/gflow/production/")
        ),
        "RESERVATION_LINK_MATCH": (
            reservation.get("attempt_id") == attempt_id
            and reservation.get("state") == "UNKNOWN_SUBMISSION_STATE"
            and credit_amount == Decimal("50")
        ),
        "TIMESTAMP_COMPATIBLE": timestamp_compatible,
        "POLICY_AUDIT_MATCH": policy.get("policy_id") == ambiguous_legacy_policy_id(attempt_id)
        and policy.get("attempt_id") == attempt_id,
        "RECONCILIATION_AUDIT_MATCH": reconciliation_sha256
        == policy.get("reconciliation_audit_sha256"),
    }
    if not all(correlations.values()):
        raise LegacyJobLinkageProofError(
            "CONFLICTING", "one or more authoritative legacy linkage correlations disagree"
        )

    return {
        "contract_version": REPAIR_CONTRACT_VERSION,
        "linkage_confidence": "PROVEN",
        "attempt_id": attempt_id,
        "production_job_id": production_job_id,
        "scene_job_id": scene_job_id,
        "unique_match_counts": {
            "d2_attempt": len(attempt_rows),
            "production_job": len(production_rows),
            "scene_job": len(scene_candidates),
            "credit_reservation": len(reservation_rows),
        },
        "correlations": correlations,
        "source_snapshot_sha256": snapshot_hashes,
        "source_snapshot_observed_at": snapshot_times,
        "relevant_source_sha256": {name: _sha256(value) for name, value in relevant_rows.items()},
        "policy_audit_sha256": policy_sha256,
        "reconciliation_audit_sha256": reconciliation_sha256,
        "target_attempt_sha256": _sha256(_without_job_linkage(attempt)),
        "verified_at": verified_at,
        "software_commit": software_commit,
    }


def _validate_policy_event(
    event: Mapping[str, object],
    *,
    attempt_id: str,
    production_job_id: str,
    scene_job_id: str,
) -> None:
    policy = _as_mapping(event.get("policy"))
    event_sha256 = event.get("event_sha256")
    previous = event.get("previous_event_sha256")
    unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
    expected = hashlib.sha256(str(previous).encode("ascii") + _canonical(unsigned)).hexdigest()
    replacement = _as_mapping(policy.get("replacement"))
    if (
        event.get("event_type") != "POLICY_APPLIED"
        or event.get("policy_id") != ambiguous_legacy_policy_id(attempt_id)
        or not isinstance(event_sha256, str)
        or not _HASH_RE.fullmatch(event_sha256)
        or event_sha256 != expected
        or policy.get("policy_version") != "AMBIGUOUS_LEGACY_RECOVERY_POLICY_V1"
        or policy.get("attempt_id") != attempt_id
        or policy.get("production_job_id") != production_job_id
        or policy.get("scene_job_id") != scene_job_id
        or policy.get("reservation_id") != attempt_id
        or policy.get("reservation_credits") != 50
        or policy.get("reservation_state") != "UNKNOWN_SUBMISSION_STATE"
        or policy.get("billing_state") != "UNKNOWN"
        or policy.get("production_job_target") != "QUARANTINED"
        or policy.get("scene_job_target") != "QUARANTINED"
        or policy.get("automatic_retry") != "PROHIBITED"
        or policy.get("automatic_resume") != "PROHIBITED"
        or policy.get("generate_authorized") is not False
        or policy.get("reservation_mutation_authorized") is not False
        or replacement.get("eligible_after_quarantine") is not True
        or replacement.get("authorized") is not False
        or replacement.get("attempt_creation_authorized") is not False
        or replacement.get("predecessor_attempt_id") != attempt_id
    ):
        raise LegacyJobLinkageProofError(
            "CONFLICTING", "the applied recovery-policy audit does not match this legacy case"
        )


def _validate_reconciliation_event(
    event: Mapping[str, object],
    *,
    attempt_id: str,
    production_job_id: str,
    scene_job_id: str,
    scene_id: str,
    prompt_id: str,
    prompt_sha256: str,
    profile_id: str,
    account_id: str,
    project_id: str,
) -> str:
    digest = event.get("event_sha256")
    unsigned = {key: value for key, value in event.items() if key != "event_sha256"}
    provenance = _as_mapping(event.get("evidence_provenance"))
    identities = {
        "attempt_id": (attempt_id, ProdLegacyEvidenceClass.PERSISTED_DIRECT.value),
        "production_job_id": (
            production_job_id,
            ProdLegacyEvidenceClass.DERIVED_CANONICAL_JOIN.value,
        ),
        "scene_job_id": (scene_job_id, ProdLegacyEvidenceClass.DERIVED_CANONICAL_JOIN.value),
        "scene_id": (scene_id, ProdLegacyEvidenceClass.PERSISTED_DIRECT.value),
        "prompt_id": (prompt_id, ProdLegacyEvidenceClass.DERIVED_CANONICAL_JOIN.value),
        "prompt_sha256": (prompt_sha256, ProdLegacyEvidenceClass.PERSISTED_DIRECT.value),
        "generation_profile_id": (
            profile_id,
            ProdLegacyEvidenceClass.PERSISTED_DIRECT.value,
        ),
        "provider_account_id": (account_id, ProdLegacyEvidenceClass.PERSISTED_DIRECT.value),
        "flow_project_id": (project_id, ProdLegacyEvidenceClass.PERSISTED_DIRECT.value),
    }
    if (
        event.get("contract_version") != LEGACY_PROD_CONTRACT_VERSION
        or event.get("legacy_attempt_id") != attempt_id
        or event.get("result") != "AMBIGUOUS_AFTER_RECONCILIATION"
        or event.get("mutation_performed") is not False
        or event.get("reservation_action") != "UNCHANGED"
        or not isinstance(digest, str)
        or not _HASH_RE.fullmatch(digest)
        or digest != _sha256(unsigned)
        or any(
            not _provenance_matches(provenance, name, value, provenance_class)
            for name, (value, provenance_class) in identities.items()
        )
    ):
        raise LegacyJobLinkageProofError(
            "CONFLICTING", "the prior reconciliation audit does not prove the requested lineage"
        )
    return digest


def _timestamps_compatible(
    attempt: Mapping[str, object],
    production: Mapping[str, object],
    scene_job: Mapping[str, object],
) -> bool:
    try:
        ordered = [
            _parse_timestamp(production.get("Requested_At") or production.get("Created_At")),
            _parse_timestamp(scene_job.get("Created_At")),
            _parse_timestamp(scene_job.get("Started_At")),
            _parse_timestamp(attempt.get("started_at")),
            _parse_timestamp(attempt.get("updated_at")),
        ]
    except ValueError:
        return False
    return ordered == sorted(ordered)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is not timezone-aware")
    return parsed


def _require_aware_timestamp(value: str, label: str) -> None:
    try:
        _parse_timestamp(value)
    except ValueError as exc:
        raise LegacyJobLinkageProofError("INVALID_PROOF", f"{label} is invalid") from exc


def _credit_amount(value: object) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return amount if amount.is_finite() else None


def _unique(rows: tuple[Mapping[str, object], ...], key: str, value: str) -> dict[str, object]:
    matches = [row for row in rows if row.get(key) == value]
    if len(matches) != 1:
        raise LegacyJobLinkageProofError("AMBIGUOUS", f"{key} does not resolve exactly once")
    return dict(matches[0])


def _without_job_linkage(attempt: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in attempt.items()
        if key not in {"production_job_id", "scene_job_id"}
    }


def _as_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return cast(Mapping[str, object], value)


def _provenance_matches(
    provenance: Mapping[str, object],
    name: str,
    value: str,
    provenance_class: str,
) -> bool:
    entry = _as_mapping(provenance.get(name))
    return entry.get("value") == value and entry.get("provenance_class") == provenance_class


def _canonical(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _sha256(value: object) -> str:
    if isinstance(value, Mapping):
        encoded = _canonical(cast(Mapping[str, object], value))
    else:
        encoded = _canonical({"value": value})
    return hashlib.sha256(encoded).hexdigest()
