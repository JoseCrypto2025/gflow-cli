"""Record one exact TEST D2 attempt's post-run provider balance read."""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from gflow_cli.v2_runtime.phase_g import D2DailyCreditLedger, PhaseGError

_EXPECTED_ROOT = Path(r"D:\Automation Content\gflow-v2-d2-state")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--d2-run-id", required=True)
    parser.add_argument("--credits-before", required=True)
    parser.add_argument("--credits-after", required=True)
    args = parser.parse_args()
    try:
        state_root = args.state_root.resolve(strict=True)
        if state_root != _EXPECTED_ROOT.resolve(strict=False):
            raise ValueError("runtime root is not the authorized TEST runtime root")
        before = Decimal(args.credits_before)
        after = Decimal(args.credits_after)
        if not before.is_finite() or not after.is_finite():
            raise ValueError("provider balance is not finite")
        record = D2DailyCreditLedger(state_root).reconcile_test_session_attempt(
            args.d2_run_id,
            provider_credits_before=before,
            provider_credits_after=after,
        )
    except (OSError, ValueError, InvalidOperation, PhaseGError) as exc:
        print(f"D2_TEST_SESSION_RECONCILIATION=BLOCKED ({type(exc).__name__})")
        return 1
    print("D2_TEST_SESSION_RECONCILIATION=RECORDED")
    print(f"SESSION_STATUS={record['status']}")
    print(f"SESSION_SPENT_CREDITS={record['spent_credits']}")
    print(f"SESSION_RESERVED_CREDITS={record['reserved_credits']}")
    print(f"SESSION_STOPPED_REASON={record['stopped_reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
