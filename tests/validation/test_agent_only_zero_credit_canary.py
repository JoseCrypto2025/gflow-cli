from __future__ import annotations

from gflow_cli.v2_runtime.live_flow import GoogleFlowAgentOnlyPort
from scripts.operations.agent_only_zero_credit_canary import _sanitized_rpcids


def test_canary_keeps_only_rpcids_and_discards_other_url_parameters() -> None:
    assert _sanitized_rpcids(
        "https://flow.google.com/data/batchexecute?rpcids=WuwhI&prompt=private&token=secret"
    ) == ["WuwhI"]


def test_canary_ignores_rpcids_from_non_batchexecute_urls() -> None:
    assert _sanitized_rpcids("https://flow.google.com/project/123?rpcids=WuwhI") == []


def test_canary_service_worker_block_is_explicit_and_opt_in() -> None:
    base = GoogleFlowAgentOnlyPort(
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_account_id="PVA_0001",
        provider_project_id="34e60953-e534-46e9-ac65-993893e61065",
        project_name="AI Video Automation V2 - TEST",
        model_display_names={"MDL_0001": "Veo 3.1 - Lite"},
        block_service_workers=True,
    )
    normal = GoogleFlowAgentOnlyPort(
        auth_profile_key="GFLOW_V2_TEST_AUTH",
        provider_account_id="PVA_0001",
        provider_project_id="34e60953-e534-46e9-ac65-993893e61065",
        project_name="AI Video Automation V2 - TEST",
        model_display_names={"MDL_0001": "Veo 3.1 - Lite"},
    )

    assert base.block_service_workers is True
    assert normal.block_service_workers is False
