from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import gflow_cli.v2_runtime.disk_guard as disk_guard
from gflow_cli.v2_runtime.assets import AssetReference
from gflow_cli.v2_runtime.catalog import (
    AccountRecord,
    CapabilityRecord,
    ModelRecord,
    ProfileRecord,
    PromptTemplateRecord,
    ProviderRecord,
    resolve_profile,
)
from gflow_cli.v2_runtime.config import load_runtime_settings
from gflow_cli.v2_runtime.disk_guard import check_free_space
from gflow_cli.v2_runtime.errors import (
    AdapterUnavailableError,
    AssetVerificationError,
    CatalogResolutionError,
    ConfigurationError,
    UnsafePathError,
)
from gflow_cli.v2_runtime.legacy_api import LegacyVideoRequest, plan_legacy_request
from gflow_cli.v2_runtime.lifecycle import upload_verify_then_cleanup
from gflow_cli.v2_runtime.providers import ContractOnlyAdapter, ProviderRouter, ServiceType
from gflow_cli.v2_runtime.storage import AssetMetadata, GoogleDriveStorageProvider
from gflow_cli.v2_runtime.verification import verify_local_file, verify_remote_metadata
from gflow_cli.v2_runtime.workspace import cleanup_workspace, create_workspace


class Catalog:
    provider_row = ProviderRecord("PRV_0001", "VIDEO_GENERATION", "ACTIVE")
    account_row = AccountRecord("PVA_0001", "PRV_0001", "DEV", "ACTIVE")
    model_row = ModelRecord("MDL_0001", "PRV_0001", "ACTIVE")
    profile_row = ProfileRecord(
        "GPR_0001",
        "PVA_0001",
        "MDL_0001",
        "TEXT_TO_VIDEO",
        "DEV",
        "test",
        "ACTIVE",
        prompt_template_id="PTM_0001",
        default_aspect_ratio="16:9",
        default_duration_sec=8,
        default_resolution="720p",
        default_output_count=1,
        audio_mode="NATIVE",
        quality_tier="STANDARD",
    )
    prompt_row = PromptTemplateRecord("PTM_0001", "ACTIVE", "PRV_0001", "MDL_0001", "TEXT_TO_VIDEO")
    caps = [
        CapabilityRecord("GENERATION_MODE", "TEXT_TO_VIDEO"),
        CapabilityRecord("ASPECT_RATIO", "16:9", "TEXT_TO_VIDEO"),
        CapabilityRecord("DURATION_SEC", "8", "TEXT_TO_VIDEO"),
        CapabilityRecord("RESOLUTION", "720p", "TEXT_TO_VIDEO"),
        CapabilityRecord("OUTPUT_COUNT", "1", "TEXT_TO_VIDEO"),
        CapabilityRecord("AUDIO_MODE", "NATIVE", "TEXT_TO_VIDEO"),
        CapabilityRecord("QUALITY_TIER", "STANDARD", "TEXT_TO_VIDEO"),
    ]

    def provider(self, provider_id: str) -> ProviderRecord | None:
        return self.provider_row

    def account(self, account_id: str) -> AccountRecord | None:
        return self.account_row

    def model(self, model_id: str) -> ModelRecord | None:
        return self.model_row

    def profile(self, profile_id: str) -> ProfileRecord | None:
        return self.profile_row

    def prompt_template(self, prompt_template_id: str) -> PromptTemplateRecord | None:
        return self.prompt_row

    def capabilities(self, model_id: str) -> list[CapabilityRecord]:
        return self.caps


def test_environment_settings_reject_windows_paths_and_require_production_roots() -> None:
    with pytest.raises(ConfigurationError):
        load_runtime_settings({"TEMP_ROOT": r"C:\\temp"})
    with pytest.raises(ConfigurationError):
        load_runtime_settings({"RUNTIME_ENV": "production"})


def test_environment_settings_resolve_development_defaults() -> None:
    settings = load_runtime_settings({"RUNTIME_ENV": "development", "MIN_FREE_DISK_GB": "20"})
    assert settings.environment == "development" and settings.min_free_disk_gb == 20


def test_production_environment_rejects_windows_drive_roots() -> None:
    with pytest.raises(ConfigurationError):
        load_runtime_settings(
            {
                "RUNTIME_ENV": "production",
                "STORAGE_PROVIDER": "google-drive",
                "RUNTIME_STATE_ROOT": r"D:\\state",
                "TEMP_ROOT": r"D:\\temp",
                "LOG_ROOT": r"D:\\logs",
            }
        )


def test_workspace_layout_and_cleanup_safety(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path / "temp", "JOB_1")
    assert set(workspace.directories) == {
        "input",
        "generated",
        "audio",
        "ffmpeg",
        "validated",
        "logs",
    }
    old = datetime.now(UTC) - timedelta(days=5)
    assert not cleanup_workspace(tmp_path / "temp", "JOB_1", "RUNNING", old)
    assert workspace.root.exists()
    assert not cleanup_workspace(tmp_path / "temp", "JOB_1", "SUCCESS", old)
    assert cleanup_workspace(
        tmp_path / "temp", "JOB_1", "SUCCESS", old, remote_assets_verified=True
    )
    failed = create_workspace(tmp_path / "temp", "JOB_2")
    assert not cleanup_workspace(tmp_path / "temp", "JOB_2", "FAILED", datetime.now(UTC))
    assert cleanup_workspace(tmp_path / "temp", "JOB_2", "FAILED", old)
    assert not failed.root.exists()
    assert not cleanup_workspace(tmp_path / "temp", "JOB_3", "UNKNOWN", old)
    with pytest.raises(UnsafePathError):
        cleanup_workspace(tmp_path / "temp", "", "FAILED", old)
    with pytest.raises(UnsafePathError):
        create_workspace(tmp_path / "temp", "../outside")


def test_workspace_refuses_symlinked_job_target(tmp_path: Path) -> None:
    root = tmp_path / "temp"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "JOB_1").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        cleanup_workspace(root, "JOB_1", "FAILED", datetime.now(UTC) - timedelta(days=5))


def test_workspace_refuses_filesystem_root(tmp_path: Path) -> None:
    with pytest.raises(UnsafePathError):
        create_workspace(Path(tmp_path.anchor), "JOB_1")


def test_asset_verification_checks_size_and_hash(tmp_path: Path) -> None:
    path = tmp_path / "asset.bin"
    path.write_bytes(b"video")
    ref = AssetReference("drive", "asset-1", "jobs/1/video.mp4", size_bytes=5)
    result = verify_local_file(path, ref)
    assert result.exists and result.size_bytes == 5 and result.checksum_sha256
    verify_remote_metadata(ref, AssetMetadata(True, 5))


def test_google_drive_adapter_is_a_no_io_contract_placeholder(tmp_path: Path) -> None:
    provider = GoogleDriveStorageProvider()
    reference = AssetReference("google-drive", "asset-1", "ref/1.png")
    with pytest.raises(AdapterUnavailableError):
        provider.resolve_asset(reference)
    assert list(tmp_path.iterdir()) == []


class FakeStorage:
    def __init__(self, metadata: AssetMetadata | None = None, fail_upload: bool = False) -> None:
        self.metadata = metadata or AssetMetadata(True, 5)
        self.fail_upload = fail_upload

    def upload_asset(self, source: Path, logical_key: str) -> AssetReference:
        if self.fail_upload:
            raise RuntimeError("upload failed")
        return AssetReference(
            "mock", "asset-1", logical_key, remote_file_id="remote-1", size_bytes=5
        )

    def get_metadata(self, reference: AssetReference) -> AssetMetadata:
        return self.metadata


def test_upload_remote_verify_then_cleanup(tmp_path: Path) -> None:
    local = tmp_path / "final.mp4"
    local.write_bytes(b"video")
    ref = upload_verify_then_cleanup(FakeStorage(), local, "final/1.mp4")
    assert ref.remote_file_id == "remote-1" and not local.exists()


@pytest.mark.parametrize(
    ("storage", "error_type"),
    [
        (FakeStorage(fail_upload=True), RuntimeError),
        (FakeStorage(AssetMetadata(True, 4)), AssetVerificationError),
    ],
)
def test_upload_or_remote_verification_failure_retains_local_file(
    tmp_path: Path, storage: FakeStorage, error_type: type[Exception]
) -> None:
    local = tmp_path / "final.mp4"
    local.write_bytes(b"video")
    with pytest.raises(error_type):
        upload_verify_then_cleanup(storage, local, "final/1.mp4")
    assert local.exists()


def test_disk_guard_pass_and_hold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        disk_guard.shutil, "disk_usage", lambda _path: SimpleNamespace(free=100, total=500)
    )
    assert check_free_space(tmp_path, 50, 25, "JOB_1").allowed
    held = check_free_space(tmp_path, 50, 60, "JOB_2")
    assert not held.allowed and held.total_bytes == 500 and held.job_id == "JOB_2"
    assert held.status == "DISK_GUARD_HOLD"
    assert check_free_space(tmp_path, 50, 25, "JOB_1").status == "DISK_GUARD_PASS"


def test_profile_resolution_checks_provider_and_model_capabilities() -> None:
    catalog = Catalog()
    profile, provider, _account, _model = resolve_profile(catalog, "GPR_0001", "DEV")
    assert profile.generation_profile_id == "GPR_0001"
    assert provider.provider_type == "VIDEO_GENERATION"
    catalog.profile_row = ProfileRecord(
        "GPR_0001", "PVA_0001", "MDL_0002", "TEXT_TO_VIDEO", "DEV", "test", "ACTIVE"
    )
    catalog.model_row = ModelRecord("MDL_0002", "PRV_0002", "ACTIVE")
    with pytest.raises(CatalogResolutionError):
        resolve_profile(catalog, "GPR_0001", "DEV")


def test_profile_resolution_rejects_prompt_template_mismatch() -> None:
    catalog = Catalog()
    catalog.prompt_row = PromptTemplateRecord(
        "PTM_0001", "ACTIVE", "PRV_0002", "MDL_0001", "TEXT_TO_VIDEO"
    )
    with pytest.raises(CatalogResolutionError):
        resolve_profile(catalog, "GPR_0001", "DEV")


def test_router_checks_provider_service_type_and_contract_adapter_never_executes() -> None:
    router = ProviderRouter(
        Catalog(), {"VIDEO_GENERATION": ContractOnlyAdapter("VIDEO_GENERATION")}, "DEV"
    )
    route, _adapter = router.route("GPR_0001", ServiceType.VIDEO_GENERATION)
    assert route.model_id == "MDL_0001"
    with pytest.raises(CatalogResolutionError):
        router.route("GPR_0001", ServiceType.TTS)
    with pytest.raises(AdapterUnavailableError):
        ContractOnlyAdapter("VIDEO_GENERATION").prepare_request("GPR_0001", {})


def test_legacy_overlong_duration_requires_explicit_scene_plan() -> None:
    plan = plan_legacy_request(LegacyVideoRequest("A scene", 30, "16:9"), {4, 6, 8})
    assert not plan.accepted and plan.disposition == "requires_scene_plan"
    within = plan_legacy_request(LegacyVideoRequest("A scene", 8, "16:9"), {4, 6, 8})
    assert within.disposition == "profile_resolution_required"
