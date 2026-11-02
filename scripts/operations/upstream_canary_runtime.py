"""Production port bindings and read-only preflight for upstream canary V1."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import cast

from gflow_cli.auth import profile_dir
from gflow_cli.v2_runtime.config import RuntimeSettings, load_runtime_settings
from gflow_cli.v2_runtime.google_drive_storage import GoogleDriveStorageAdapter
from gflow_cli.v2_runtime.google_sheets import (
    APPROVED_JOB_ARTIFACTS_FOLDER_ID,
    APPROVED_SPREADSHEET_ID,
    GoogleSheetsApiTransport,
    GoogleSheetsSnapshotSource,
    GoogleWorkspaceCredentialProvider,
)
from gflow_cli.v2_runtime.google_sheets_production_jobs import GoogleSheetsProductionJobStore
from gflow_cli.v2_runtime.google_sheets_transactions import (
    GoogleSheetsTransactionApiTransport,
    OptimisticGoogleSheetsJobStore,
)
from gflow_cli.v2_runtime.phase_e import InMemorySceneCatalog, Row, preflight_scene, sha256_json
from gflow_cli.v2_runtime.production_execution import CreditBudgetContext, ProductionCreditGuard
from gflow_cli.v2_runtime.production_jobs import ProductionRequest
from gflow_cli.v2_runtime.upstream_canary import (
    CanaryRequest,
    FreshCanaryEvidence,
    FreshCanaryReader,
    UpstreamCanaryProductionCaller,
)
from gflow_cli.v2_runtime.upstream_gflow_adapter import (
    UPSTREAM_COMMIT,
    UPSTREAM_VERSION,
    UpstreamGflowVideoAdapter,
)
from scripts.operations.run_production_scene import (
    _PROD_BROWSER_STATE_ROOT,
    _PROD_CREDITS_PROFILE,
    ProductionSceneOperatorError,
    _read_current_production_credits,
    _require_production_profile_home,
    _unique_row,
    _validate_preclaim,
    _validate_prod_account,
    _validated_quote,
)

UPSTREAM_PYTHON_EXECUTABLE = Path("/opt/gflow-upstream/0.92.1/venv/bin/python")
CANARY_ADAPTER_COMMIT = "928ac4a3541c129384bace514474bf3c14e6428a"
_PROD_ACCOUNT_ID = "PVA_0002"
_REQUIRED_PROMPT_ID = "SPV_000007"
_UPSTREAM_SOURCE_ROOT = Path("/opt/gflow-upstream/0.92.1/source")
_UPSTREAM_WHEEL = Path("/opt/gflow-upstream/0.92.1/dist/gflow_cli-0.92.1-py3-none-any.whl")
_UPSTREAM_RUNTIME_ROOT = Path("/opt/gflow-upstream/0.92.1")
_UPSTREAM_CLI_EXECUTABLE = _UPSTREAM_RUNTIME_ROOT / "venv/bin/gflow"


class ProductionCanaryFreshReader(FreshCanaryReader):
    """Read current approved workload, quote, identity, and project policy."""

    def __init__(
        self,
        snapshot_source: GoogleSheetsSnapshotSource,
        *,
        profile_credits_reader: Callable[[str], Decimal] = _read_current_production_credits,
    ) -> None:
        self._snapshot_source = snapshot_source
        self._profile_credits_reader = profile_credits_reader

    def read_fresh(self, request: CanaryRequest) -> FreshCanaryEvidence:
        snapshot = self._snapshot_source.read()
        if snapshot.spreadsheet_id != APPROVED_SPREADSHEET_ID:
            raise ProductionSceneOperatorError("live snapshot spreadsheet identity differs")
        tables: dict[str, list[Row]] = {
            name: [dict(row) for row in rows] for name, rows in snapshot.tables.items()
        }
        scene = _unique_row(tables.get("SCENES", []), "Scene_ID", request.scene_id, "locked scene")
        if scene.get("Script_ID") != request.script_id:
            raise ProductionSceneOperatorError("scene script identity differs")
        profile = _unique_row(
            tables.get("GENERATION_PROFILE_MASTER", []),
            "Generation_Profile_ID",
            request.generation_profile_id,
            "production generation profile",
        )
        if (
            request.generation_profile_id != "GPR_0002"
            or request.profile_name != _PROD_CREDITS_PROFILE
            or profile.get("Environment") != "PROD"
            or profile.get("Status") != "ACTIVE"
            or profile.get("Provider_Account_ID") != _PROD_ACCOUNT_ID
        ):
            raise ProductionSceneOperatorError(
                "generation profile is not bound to this PROD account"
            )

        preflight = preflight_scene(
            InMemorySceneCatalog(tables), request.scene_id, request.generation_profile_id, "PROD"
        )
        if (
            scene.get("Scene_Definition_Status") != "LOCKED"
            or preflight.status != "PREFLIGHT_PASS"
            or not preflight.ready_for_provider_adapter
            or preflight.references
        ):
            raise ProductionSceneOperatorError("fresh locked-scene Phase E did not pass")

        approved = [
            row
            for row in tables.get("SCENE_PROMPT_VERSION", [])
            if row.get("Scene_ID") == request.scene_id
            and row.get("Generation_Profile_ID") == request.generation_profile_id
            and row.get("Prompt_Status") == "APPROVED"
        ]
        if len(approved) != 1:
            raise ProductionSceneOperatorError(
                "exactly one approved prompt is required for this scene and profile"
            )
        prompt = _unique_row(approved, "Scene_Prompt_ID", request.prompt_id, "approved prompt")
        if request.prompt_id != _REQUIRED_PROMPT_ID:
            raise ProductionSceneOperatorError("canary prompt ID differs from the approved target")
        prompt_text = str(prompt.get("Compiled_Prompt_Text", ""))
        negative_prompt_text = str(prompt.get("Compiled_Negative_Prompt", ""))
        prompt_sha256 = str(prompt.get("Prompt_SHA256", ""))
        recomputed_prompt_sha256 = sha256_json(
            {"prompt": prompt_text, "negative_prompt": negative_prompt_text}
        )
        if (
            not prompt_text.strip()
            or not re.fullmatch(r"[0-9a-f]{64}", prompt_sha256)
            or prompt_text != (preflight.compiled_prompt or "")
            or negative_prompt_text != (preflight.compiled_negative_prompt or "")
            or recomputed_prompt_sha256 != prompt_sha256
            or preflight.prompt_sha256 != prompt_sha256
        ):
            raise ProductionSceneOperatorError(
                "canonical prompt payload or SHA-256 differs from fresh Phase E"
            )

        _validate_prod_account(tables, _PROD_ACCOUNT_ID, request.project_id)
        quote = _validated_quote(
            snapshot,
            tables,
            scene_id=request.scene_id,
            profile_id=request.generation_profile_id,
            account_id=_PROD_ACCOUNT_ID,
            effective_config=preflight.technical_configuration,
        )
        if quote.amount is None or not quote.source_version:
            raise ProductionSceneOperatorError("fresh canonical quote is unavailable or incomplete")
        production_request = ProductionRequest(
            request.script_id,
            "PROD",
            request.request_idempotency_key,
            (request.prompt_id,),
        )
        _validate_preclaim(
            tables=tables,
            preflight=preflight,
            request=production_request,
            quote=quote,
            scene_id=request.scene_id,
            profile_id=request.generation_profile_id,
            account_id=_PROD_ACCOUNT_ID,
            project_id=request.project_id,
            prompt_id=request.prompt_id,
            prompt_sha256=prompt_sha256,
        )

        available_credits = self._profile_credits_reader(request.account_email)
        technical = preflight.technical_configuration
        duration = Decimal(str(technical.get("duration_sec", "0")))
        output_count = int(str(technical.get("output_count", "0")))
        if duration != Decimal("8") or output_count != 1:
            raise ProductionSceneOperatorError("effective generation configuration differs")
        _require_production_profile_home()
        if (
            os.environ.get("GFLOW_CLI_HOME", "").replace("\\", "/")
            != _PROD_BROWSER_STATE_ROOT.as_posix()
        ):
            raise ProductionSceneOperatorError("production browser profile root differs")

        return FreshCanaryEvidence(
            prompt_status="APPROVED",
            prompt_text=prompt_text,
            prompt_sha256=prompt_sha256,
            negative_prompt_text=negative_prompt_text,
            scene_id=request.scene_id,
            prompt_id=request.prompt_id,
            generation_profile_id=request.generation_profile_id,
            profile_name=request.profile_name,
            auth_profile="GFLOW_PROD_AUTH",
            account_email=request.account_email,
            provider_account_id=_PROD_ACCOUNT_ID,
            project_id=request.project_id,
            model="veo-lite",
            aspect_ratio=str(technical.get("aspect_ratio", "")),
            duration_seconds=8,
            output_count=1,
            quote_credits=quote.amount,
            quote_input_digest=quote.input_digest,
            quote_source_version=quote.source_version,
            quote_read_at=quote.quoted_at,
            credit_budget=CreditBudgetContext(
                authorized_run_ceiling=request.ceo_credit_ceiling,
                provider_remaining_free=available_credits,
            ),
            profile_home=profile_dir(request.profile_name),
            identity_verified=True,
            scene_locked=True,
            project_policy_verified=True,
            profile_lease_released=True,
            adapter_commit=CANARY_ADAPTER_COMMIT,
            upstream_version=UPSTREAM_VERSION,
            upstream_commit=UPSTREAM_COMMIT,
        )


@dataclass(slots=True)
class ProductionCanaryRuntime:
    """Constructed production bindings; creating this object performs no writes."""

    settings: RuntimeSettings
    reader: ProductionCanaryFreshReader
    transaction_rows: OptimisticGoogleSheetsJobStore
    jobs: GoogleSheetsProductionJobStore
    credit_guard: ProductionCreditGuard
    adapter: UpstreamGflowVideoAdapter
    storage: GoogleDriveStorageAdapter
    caller: UpstreamCanaryProductionCaller
    sheets_reader: GoogleSheetsApiTransport
    sheets_writer: GoogleSheetsTransactionApiTransport

    def close(self) -> None:
        self.sheets_reader.close()
        self.sheets_writer.close()
        self.storage.close()


def bind_upstream_canary_caller(
    *,
    reader: FreshCanaryReader,
    jobs: GoogleSheetsProductionJobStore,
    credit_guard: ProductionCreditGuard,
    storage: GoogleDriveStorageAdapter,
    upstream_python_executable: Path | None = None,
) -> tuple[UpstreamCanaryProductionCaller, UpstreamGflowVideoAdapter]:
    """Bind already-constructed canonical ports to the pinned one-shot adapter."""
    executable = upstream_python_executable or UPSTREAM_PYTHON_EXECUTABLE
    required = (reader, jobs, credit_guard, storage)
    if any(port is None for port in required):
        raise ProductionSceneOperatorError("all production canary ports are required")
    if (
        not executable.is_absolute()
        or executable != UPSTREAM_PYTHON_EXECUTABLE
        or not executable.is_file()
    ):
        raise ProductionSceneOperatorError("isolated upstream Python executable is unavailable")
    adapter = UpstreamGflowVideoAdapter(python_executable=executable)
    caller = UpstreamCanaryProductionCaller(
        reader=reader,
        jobs=jobs,
        credit_guard=credit_guard,
        adapter=adapter,
        storage=storage,
    )
    return caller, adapter


def build_production_canary_runtime(
    *,
    upstream_python_executable: Path | None = None,
) -> ProductionCanaryRuntime:
    """Bind the existing production implementations without claiming work or reserving credits."""
    executable = upstream_python_executable or UPSTREAM_PYTHON_EXECUTABLE
    if (
        not executable.is_absolute()
        or executable != UPSTREAM_PYTHON_EXECUTABLE
        or not executable.is_file()
    ):
        raise ProductionSceneOperatorError("isolated upstream Python executable is unavailable")
    settings = load_runtime_settings()
    if settings.environment != "production" or settings.storage_provider != "google-drive":
        raise ProductionSceneOperatorError("production Google Drive runtime settings are required")
    _require_production_profile_home()
    credential_value = os.environ.get("GFLOW_GOOGLE_WORKSPACE_CREDENTIALS", "")
    if not credential_value:
        raise ProductionSceneOperatorError("Google Workspace credentials are not configured")

    repository_root = Path(__file__).resolve().parents[2]
    token_provider = GoogleWorkspaceCredentialProvider(
        Path(credential_value), repository_root=repository_root
    )
    sheets_reader = GoogleSheetsApiTransport(token_provider)
    sheets_writer = GoogleSheetsTransactionApiTransport(token_provider)
    snapshot_source = GoogleSheetsSnapshotSource(sheets_reader, repository_root / "schemas")
    transaction_rows = OptimisticGoogleSheetsJobStore(
        sheets_writer,
        repository_root / "schemas",
        writer_lock_root=settings.runtime_state_root,
    )
    jobs = GoogleSheetsProductionJobStore(transaction_rows)
    credit_guard = ProductionCreditGuard(settings.runtime_state_root)
    storage = GoogleDriveStorageAdapter(
        token_provider, approved_folder_id=APPROVED_JOB_ARTIFACTS_FOLDER_ID
    )
    reader = ProductionCanaryFreshReader(snapshot_source)
    caller, adapter = bind_upstream_canary_caller(
        reader=reader,
        jobs=jobs,
        credit_guard=credit_guard,
        storage=storage,
        upstream_python_executable=executable,
    )
    return ProductionCanaryRuntime(
        settings,
        reader,
        transaction_rows,
        jobs,
        credit_guard,
        adapter,
        storage,
        caller,
        sheets_reader,
        sheets_writer,
    )


def verify_zero_generate_preflight(
    runtime: ProductionCanaryRuntime, request: CanaryRequest
) -> FreshCanaryEvidence:
    """Verify the live readers/stores and executable without claiming or reserving work."""
    _verify_upstream_runtime_pin()
    evidence = runtime.reader.read_fresh(request)
    runtime.transaction_rows.read_rows("PRODUCTION_JOBS")
    runtime.transaction_rows.read_rows("SCENE_JOBS")
    upstream_env = os.environ.copy()
    upstream_env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(UPSTREAM_PYTHON_EXECUTABLE), "-m", "gflow_cli", "--version"],
        cwd=runtime.settings.temp_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env=upstream_env,
    )
    if result.returncode != 0 or not result.stdout.strip().endswith("version 0.92.1"):
        raise ProductionSceneOperatorError("isolated upstream version preflight failed")
    return evidence


def _verify_upstream_runtime_pin() -> None:
    manifest_path = _UPSTREAM_RUNTIME_ROOT / "runtime-manifest.json"
    try:
        manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionSceneOperatorError("upstream runtime manifest is unavailable") from exc
    if not isinstance(manifest_value, dict):
        raise ProductionSceneOperatorError("upstream runtime manifest is malformed")
    manifest = cast(dict[str, object], manifest_value)
    if (
        manifest.get("source_repository") != "https://github.com/ffroliva/gflow-cli"
        or manifest.get("source_commit") != UPSTREAM_COMMIT
        or manifest.get("package_version") != UPSTREAM_VERSION
        or manifest.get("source_tree_clean") is not True
    ):
        raise ProductionSceneOperatorError("upstream runtime source pin differs")
    source_commit = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={_UPSTREAM_SOURCE_ROOT}",
            "-C",
            str(_UPSTREAM_SOURCE_ROOT),
            "rev-parse",
            "HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    source_tree = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={_UPSTREAM_SOURCE_ROOT}",
            "-C",
            str(_UPSTREAM_SOURCE_ROOT),
            "rev-parse",
            "HEAD^{tree}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    source_status = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={_UPSTREAM_SOURCE_ROOT}",
            "-C",
            str(_UPSTREAM_SOURCE_ROOT),
            "status",
            "--porcelain",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if (
        source_commit.returncode != 0
        or source_commit.stdout.strip() != UPSTREAM_COMMIT
        or source_tree.returncode != 0
        or source_tree.stdout.strip() != manifest.get("source_tree_digest")
        or source_status.returncode != 0
        or source_status.stdout.strip()
    ):
        raise ProductionSceneOperatorError("upstream source checkout no longer matches its pin")
    expected_hashes = (
        (_UPSTREAM_WHEEL, "wheel_sha256"),
        (_UPSTREAM_CLI_EXECUTABLE, "cli_executable_sha256"),
    )
    for path, key in expected_hashes:
        if not path.is_file() or path.is_symlink():
            raise ProductionSceneOperatorError("pinned upstream runtime file is unavailable")
        if _sha256_file(path) != manifest.get(key):
            raise ProductionSceneOperatorError("pinned upstream runtime hash differs")
    if manifest.get("python_executable") != str(UPSTREAM_PYTHON_EXECUTABLE):
        raise ProductionSceneOperatorError("pinned upstream Python path differs")
    if manifest.get("cli_executable") != str(_UPSTREAM_CLI_EXECUTABLE):
        raise ProductionSceneOperatorError("pinned upstream CLI path differs")
    help_result = subprocess.run(
        [str(_UPSTREAM_CLI_EXECUTABLE), "video", "t2v", "--help"],
        cwd=_UPSTREAM_RUNTIME_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    required_options = (
        "PROMPT",
        "--profile",
        "--project",
        "--model",
        "--aspect",
        "--duration",
        "--count",
        "--output",
        "--json",
    )
    if help_result.returncode != 0 or any(
        option not in help_result.stdout for option in required_options
    ):
        raise ProductionSceneOperatorError("pinned upstream CLI contract differs")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
