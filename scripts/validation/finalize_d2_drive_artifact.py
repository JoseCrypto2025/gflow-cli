"""Finalize one D2 Drive upload after a separate connector redownload."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from gflow_cli.v2_runtime.phase_g_run import finalize_remote_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-state-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--remote-download", type=Path, required=True)
    parser.add_argument("--remote-file-id", required=True)
    parser.add_argument("--remote-filename", required=True)
    args = parser.parse_args()
    attempt = finalize_remote_artifact(
        args.runtime_state_root,
        args.run_id,
        remote_download=args.remote_download,
        remote_file_id=args.remote_file_id,
        remote_filename=args.remote_filename,
    )
    print(
        json.dumps(
            {
                "d2_run_id": attempt["d2_run_id"],
                "submission_state": attempt["submission_state"],
                "remote_file_id": attempt["remote_file_id"],
                "remote_filename": attempt["remote_filename"],
                "remote_size_bytes": attempt["remote_size_bytes"],
                "remote_redownload_sha256": attempt["remote_redownload_sha256"],
                "remote_verified": attempt["remote_verified"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
