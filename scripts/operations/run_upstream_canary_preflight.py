"""Run the read-only zero-Generate preflight for upstream canary V1."""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from gflow_cli.v2_runtime.upstream_canary import CanaryRequest
from scripts.operations.upstream_canary_runtime import (
    build_production_canary_runtime,
    verify_zero_generate_preflight,
)


def main() -> int:
    request = CanaryRequest(
        script_id="SCR_000001",
        scene_id="SCN_000001",
        prompt_id="SPV_000007",
        generation_profile_id="GPR_0002",
        profile_name="gflow-prod",
        account_email="singleava508@gmail.com",
        project_id="aafe9282-071f-4333-baa4-92b9f76a9c27",
        request_idempotency_key="upstream-canary-v1:SPV_000007:zero-generate-preflight",
        authorization_id="ZERO_GENERATE_PREFLIGHT_ONLY",
        authorized=False,
        ceo_credit_ceiling=Decimal("30"),
        staging_root=Path("/var/tmp/gflow/upstream-canary-preflight/staging"),
        verification_temp_dir=Path("/var/tmp/gflow/upstream-canary-preflight/verify"),
    )
    runtime = None
    try:
        runtime = build_production_canary_runtime()
        evidence = verify_zero_generate_preflight(runtime, request)
    except Exception as exc:  # noqa: BLE001 — report only the safe error class
        print(
            json.dumps(
                {
                    "status": "HOLD",
                    "error_type": type(exc).__name__,
                    "generate_actions": 0,
                    "jobs_created": False,
                    "reservations_created": False,
                },
                sort_keys=True,
            )
        )
        return 1
    finally:
        if runtime is not None:
            runtime.close()

    print(
        json.dumps(
            {
                "status": "PASS",
                "prompt_id": evidence.prompt_id,
                "prompt_sha256": evidence.prompt_sha256,
                "profile": evidence.profile_name,
                "account_match": evidence.identity_verified,
                "project_id": evidence.project_id,
                "project_policy_verified": evidence.project_policy_verified,
                "quote_credits": str(evidence.quote_credits),
                "quote_read_at": evidence.quote_read_at,
                "upstream_version": evidence.upstream_version,
                "upstream_commit": evidence.upstream_commit,
                "generate_actions": 0,
                "upstream_invocations": 0,
                "jobs_created": False,
                "reservations_created": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
