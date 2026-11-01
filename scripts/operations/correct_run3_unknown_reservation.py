"""Apply the CEO-authorized, one-time Run 3 reservation accounting correction."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import cast

from gflow_cli.v2_runtime.production_execution import ProductionCreditGuard

_RUNTIME_ROOT = Path("/var/lib/gflow")
_ATTEMPT_ID = "D2_5d54c068c457e7cf6fbc7a5319f312ce"
_PRODUCTION_JOB_ID = "PRD_000003"
_SCENE_JOB_ID = "SJB_000003"
_BEFORE_ROW_SHA256 = "daa32f2e98254231ee28b6bdab02e210fe8a0f261ed1b894860e0e88873c5514"
_BEFORE_LEDGER_SHA256 = "01f8b0c1fba8c6fef332389f623ecd942d0636e049e59f1da309122689eb6e2b"
_QUOTE_INPUT_DIGEST = "8c07afd5d56aefc154eb367434003b513e5af8ffcd6fd71a997f807d9c79daf7"
_QUOTE_SOURCE_VERSION = (
    "PCT_000001:2026-10-28T15:09:16Z:"
    "ee1074ae02f4ed3a9fc49759d4502815605b9479f4ce311d945713bdc3b380cc"
)
_D2_LEDGER_SHA256 = "d7a185da57e01e99d095bad51d6660d48399a1c5c4f901dea927b85ebfbd3943"
_D2_ROW_SHA256 = "e3a94a18bbe0e996e1108a570dce2289784139f35406089bac3a9417c323b97b"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_row_sha256(row: dict[str, object]) -> str:
    return _sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _ledger_sha256(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(encoded + b"\n")


def _verify_d2_lineage() -> None:
    path = _RUNTIME_ROOT / "production" / "phase-g" / "d2-credit-ledger.json"
    if path.is_symlink() or not path.is_file():
        raise SystemExit("HOLD: Run 3 D2 forensic ledger is unavailable or unsafe")
    raw = path.read_bytes()
    if _sha256(raw) != _D2_LEDGER_SHA256:
        raise SystemExit("HOLD: D2 ledger changed since lineage verification")
    document = json.loads(raw)
    if not isinstance(document, dict) or not isinstance(document.get("attempts"), list):
        raise SystemExit("HOLD: D2 ledger shape is invalid")
    attempts = cast(list[dict[str, object]], document["attempts"])
    matches = [row for row in attempts if row.get("d2_run_id") == _ATTEMPT_ID]
    if len(matches) != 1:
        raise SystemExit("HOLD: Run 3 D2 attempt is not unique")
    row = matches[0]
    digest = _sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if (
        digest != _D2_ROW_SHA256
        or row.get("execution_environment") != "PROD"
        or row.get("production_job_id") != _PRODUCTION_JOB_ID
        or row.get("scene_job_id") != _SCENE_JOB_ID
        or row.get("scene_id") != "SCN_000001"
        or row.get("scene_prompt_id") != "SPV_000007"
        or row.get("generation_profile_id") != "GPR_0002"
        or row.get("provider_account_id") != "PVA_0002"
        or row.get("provider_project_id") != "aafe9282-071f-4333-baa4-92b9f76a9c27"
        or row.get("generate_action_count") != 1
        or row.get("submission_state") != "UNKNOWN_SUBMISSION_STATE"
        or row.get("provider_billing_outcome") != "UNKNOWN"
        or row.get("terminal_state") is not None
        or row.get("prod_reconciliation_result") != "AMBIGUOUS_AFTER_RECONCILIATION"
    ):
        raise SystemExit("HOLD: Run 3 D2 lineage or forensic hold differs")
    for key, value in row.items():
        if key.startswith("forensic_closure") and value not in (None, "", {}):
            raise SystemExit("HOLD: a D2 forensic closure already exists")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the single digest-guarded 50-to-10 correction",
    )
    args = parser.parse_args()
    _verify_d2_lineage()
    guard = ProductionCreditGuard(_RUNTIME_ROOT)
    before = guard._read()  # noqa: SLF001 - this operator verifies the locked ledger contract
    rows = cast(list[dict[str, object]], before["attempts"])
    matches = [row for row in rows if row.get("attempt_id") == _ATTEMPT_ID]
    if len(matches) != 1:
        raise SystemExit("HOLD: Run 3 reservation is not unique")
    target = matches[0]
    before_ledger_digest = _ledger_sha256(before)
    before_row_digest = _canonical_row_sha256(target)
    if (
        before_ledger_digest != _BEFORE_LEDGER_SHA256
        or before_row_digest != _BEFORE_ROW_SHA256
        or target.get("production_job_id") != _PRODUCTION_JOB_ID
        or target.get("scene_job_id") != _SCENE_JOB_ID
        or target.get("reserved_credits") != "50"
        or target.get("state") != "UNKNOWN_SUBMISSION_STATE"
        or target.get("billing_outcome") not in (None, "", "UNKNOWN")
        or target.get("generate_click_count") != 1
    ):
        raise SystemExit("HOLD: exact authorized Run 3 reservation proof does not match")
    print(f"RESERVATION_ROW_ID={_ATTEMPT_ID}")
    print(f"BEFORE_AMOUNT={target['reserved_credits']}")
    print(f"BEFORE_STATE={target['state']}")
    print(f"BEFORE_ROW_DIGEST={before_row_digest}")
    print(f"BEFORE_LEDGER_DIGEST={before_ledger_digest}")
    if not args.apply:
        print("MODE=DRY_RUN")
        return 0
    result = guard.correct_legacy_unknown_reservation(
        attempt_id=_ATTEMPT_ID,
        production_job_id=_PRODUCTION_JOB_ID,
        scene_job_id=_SCENE_JOB_ID,
        expected_row_sha256=_BEFORE_ROW_SHA256,
        expected_ledger_sha256=_BEFORE_LEDGER_SHA256,
        quote_input_digest=_QUOTE_INPUT_DIGEST,
        quote_source_version=_QUOTE_SOURCE_VERSION,
    )
    after = guard._read()  # noqa: SLF001 - verify durable result and exact row delta
    after_rows = cast(list[dict[str, object]], after["attempts"])
    after_target = next(row for row in after_rows if row.get("attempt_id") == _ATTEMPT_ID)
    before_by_id = {str(row["attempt_id"]): row for row in rows}
    after_by_id = {str(row["attempt_id"]): row for row in after_rows}
    changed = [key for key in before_by_id if before_by_id[key] != after_by_id.get(key)]
    if (
        changed != [_ATTEMPT_ID]
        or result.get("reserved_credits") != "10"
        or after_target.get("state") != "UNKNOWN_SUBMISSION_STATE"
        or after_target.get("billing_outcome") != target.get("billing_outcome")
    ):
        raise SystemExit("HOLD: post-correction invariants did not verify")
    history = cast(list[dict[str, object]], after_target["reservation_correction_history"])
    audit = history[-1]
    print(f"AFTER_AMOUNT={after_target['reserved_credits']}")
    print(f"AFTER_STATE={after_target['state']}")
    print(f"AFTER_ROW_DIGEST={_canonical_row_sha256(after_target)}")
    print(f"AFTER_LEDGER_DIGEST={_ledger_sha256(after)}")
    print(f"AUDIT_RECORD_ID={audit['audit_record_id']}")
    print(f"ROWS_CHANGED={len(changed)}")
    print("PROVIDER_BILLING_OUTCOME=UNKNOWN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
