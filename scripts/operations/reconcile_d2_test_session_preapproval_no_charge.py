"""Evidence-bound operator for a pre-approval TEST-session no-charge outcome."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from gflow_cli.v2_runtime.phase_g import D2DailyCreditLedger, PhaseGError
from gflow_cli.v2_runtime.phase_g_artifacts import sha256_file
from gflow_cli.v2_runtime.phase_g_test_session_no_charge import (
    assert_evidence_matches_ledger,
    load_preapproval_no_charge_evidence,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-state-root", type=Path, required=True)
    parser.add_argument("--d2-run-id", required=True)
    parser.add_argument("--expected-ledger-sha256", required=True)
    parser.add_argument("--expected-evidence-sha256", required=True)
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--evidence-json", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        root = args.runtime_state_root.resolve(strict=True)
        evidence, evidence_sha = load_preapproval_no_charge_evidence(args.evidence_json)
        ledger = D2DailyCreditLedger(root)
        if ledger.path.is_symlink() or not ledger.path.is_file():
            raise PhaseGError("PHASE_G_LEDGER_PATH_INVALID", "D2 ledger is missing or unsafe")
        ledger_sha = sha256_file(ledger.path)
        if ledger_sha != args.expected_ledger_sha256:
            raise PhaseGError("PHASE_G_LEDGER_CHANGED", "D2 ledger SHA differs from expectation")
        if evidence_sha != args.expected_evidence_sha256:
            raise PhaseGError(
                "PHASE_G_TEST_SESSION_EVIDENCE_HASH_MISMATCH",
                "evidence SHA differs from expectation",
            )
        if (
            evidence.get("D2_RUN_ID") != args.d2_run_id
            or evidence.get("CANONICAL_LEDGER_SHA256") != ledger_sha
            or evidence.get("IMPLEMENTATION_COMMIT") != args.implementation_commit
        ):
            raise PhaseGError(
                "PHASE_G_TEST_SESSION_EVIDENCE_MISMATCH",
                "evidence does not bind the requested run, ledger, and implementation",
            )
        assert_evidence_matches_ledger(ledger.path, d2_run_id=args.d2_run_id, evidence=evidence)
        if args.apply:
            record = ledger.reconcile_test_session_preapproval_no_charge(
                args.d2_run_id,
                expected_ledger_sha256=ledger_sha,
                evidence_sha256=evidence_sha,
                implementation_commit=args.implementation_commit,
                evidence=evidence,
            )
            print("D2_TEST_SESSION_NO_CHARGE=APPLIED")
            print(f"SESSION_STATUS={record['status']}")
            print(f"SESSION_SPENT_CREDITS={record['spent_credits']}")
            print(f"SESSION_RESERVED_CREDITS={record['reserved_credits']}")
            return 0
        print("D2_TEST_SESSION_NO_CHARGE=READY")
        print(f"LEDGER_SHA256={ledger_sha}")
        print(f"EVIDENCE_SHA256={evidence_sha}")
        return 0
    except (OSError, ValueError, PhaseGError) as exc:
        print(f"D2_TEST_SESSION_NO_CHARGE=BLOCKED ({type(exc).__name__})")
        return 1


if __name__ == "__main__":
    sys.exit(main())
