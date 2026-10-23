"""Create one exact-lane TEST D2 session authorization from CEO evidence."""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from datetime import UTC, datetime, time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from gflow_cli.v2_runtime.phase_g import (
    D2DailyCreditLedger,
    D2TestSessionAuthorization,
    PhaseGError,
)

_EXPECTED_BRANCH = "automation-v2"
_EXPECTED_ROOT = Path(r"D:\Automation Content\gflow-v2-d2-state")


def _git_value(*args: str) -> str:
    result = subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True, encoding="utf-8"
    )
    return result.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument("--expected-ledger-sha256", required=True)
    parser.add_argument("--authorization-evidence", type=Path, required=True)
    parser.add_argument("--implementation-commit", required=True)
    parser.add_argument("--starting-provider-credits", required=True)
    args = parser.parse_args()

    try:
        evidence = args.authorization_evidence.read_text(encoding="utf-8")
        starting_credits = Decimal(args.starting_provider_credits)
        if (
            "CEO AUTHORIZATION" not in evidence
            or "40 FLOW CREDITS MAXIMUM" not in evidence
            or starting_credits < 0
            or not starting_credits.is_finite()
        ):
            raise ValueError("authorization evidence or starting balance is invalid")
        state_root = args.state_root.resolve(strict=True)
        if state_root != _EXPECTED_ROOT.resolve(strict=False):
            raise ValueError("runtime root is not the authorized TEST runtime root")
        if _git_value("branch", "--show-current") != _EXPECTED_BRANCH:
            raise ValueError("current branch is not automation-v2")
        if _git_value("rev-parse", "HEAD") != args.implementation_commit:
            raise ValueError("implementation commit does not match current HEAD")
        if _git_value("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}") != (
            "origin/automation-v2"
        ):
            raise ValueError("branch is not tracking origin/automation-v2")
        remote_head = _git_value("ls-remote", "origin", "refs/heads/automation-v2").split()
        if not remote_head or remote_head[0] != args.implementation_commit:
            raise ValueError("implementation commit is not secured on origin")
        if _git_value("status", "--porcelain", "--untracked-files=no"):
            raise ValueError("tracked worktree must be clean before authorization")

        now = datetime.now(ZoneInfo("Asia/Bangkok"))
        end_of_day = datetime.combine(now.date(), time(23, 59, 59), tzinfo=now.tzinfo)
        session_id = f"D2TS_{uuid4().hex[:16]}"
        authorization = D2TestSessionAuthorization(
            session_id=session_id,
            provider_account_id="PVA_0001",
            provider_project_id="34e60953-e534-46e9-ac65-993893e61065",
            auth_profile_key="GFLOW_V2_TEST_AUTH",
            runtime_state_root=state_root,
            starting_provider_credits=starting_credits,
            authorization_evidence_sha256=hashlib.sha256(
                args.authorization_evidence.read_bytes()
            ).hexdigest(),
            implementation_commit=args.implementation_commit,
            expires_at=end_of_day.astimezone(UTC).isoformat(),
        )
        ledger = D2DailyCreditLedger(state_root)
        record = ledger.open_test_session(
            authorization,
            expected_ledger_sha256=args.expected_ledger_sha256,
        )
    except (
        OSError,
        ValueError,
        InvalidOperation,
        subprocess.CalledProcessError,
        PhaseGError,
    ) as exc:
        print(f"D2_TEST_SESSION_AUTHORIZATION=BLOCKED ({type(exc).__name__})")
        return 1

    print("D2_TEST_SESSION_AUTHORIZATION=OPENED")
    print(f"SESSION_ID={session_id}")
    print(f"AUTHORIZED_NEW_CREDIT_CAP={record['authorized_new_credits']}")
    print(f"EXPIRES_AT={record['expires_at']}")
    print(f"AUTHORIZATION_EVIDENCE_SHA256={record['authorization_evidence_sha256']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
