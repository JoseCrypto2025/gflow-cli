from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from scripts.operations.run_legacy_replacement import (
    _has_clicked_consumed_replacement,
    is_non_subscriber_peak_window,
)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 10, 28, 13, 59, tzinfo=UTC), False),
        (datetime(2026, 10, 28, 14, 0, tzinfo=UTC), True),
        (datetime(2026, 10, 28, 16, 59, 59, tzinfo=UTC), True),
        (datetime(2026, 10, 28, 17, 0, tzinfo=UTC), False),
        (datetime(2026, 10, 28, 21, 0, tzinfo=timezone(timedelta(hours=7))), True),
    ],
)
def test_non_subscriber_peak_window_uses_utc_edges(now: datetime, expected: bool) -> None:
    assert is_non_subscriber_peak_window(now) is expected


def test_peak_window_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="aware"):
        is_non_subscriber_peak_window(datetime(2026, 10, 28, 14))


def test_only_exact_consumed_single_clicked_attempt_can_reconcile_during_peak() -> None:
    authorization = {
        "authorization_id": "LRA_" + "A" * 24,
        "authorization_status": "CONSUMED",
        "replacement_attempt_id": "D2_replacement-0001",
    }
    attempt = {
        "d2_run_id": "D2_replacement-0001",
        "generate_action_count": 1,
        "legacy_replacement": {"authorization_id": authorization["authorization_id"]},
    }

    assert _has_clicked_consumed_replacement(authorization, [attempt]) is True


@pytest.mark.parametrize(
    ("authorization", "attempts"),
    [
        (
            {
                "authorization_id": "LRA_" + "A" * 24,
                "authorization_status": "APPROVED",
                "replacement_attempt_id": None,
            },
            [],
        ),
        (
            {
                "authorization_id": "LRA_" + "A" * 24,
                "authorization_status": "CONSUMED",
                "replacement_attempt_id": "D2_replacement-0001",
            },
            [
                {
                    "d2_run_id": "D2_replacement-0001",
                    "generate_action_count": 0,
                    "legacy_replacement": {"authorization_id": "LRA_" + "A" * 24},
                }
            ],
        ),
        (
            {
                "authorization_id": "LRA_" + "A" * 24,
                "authorization_status": "CONSUMED",
                "replacement_attempt_id": "D2_replacement-0001",
            },
            [
                {
                    "d2_run_id": "D2_replacement-0001",
                    "generate_action_count": 1,
                    "legacy_replacement": {"authorization_id": "LRA_" + "B" * 24},
                }
            ],
        ),
    ],
)
def test_peak_window_does_not_allow_new_or_mismatched_attempts(
    authorization: dict[str, object], attempts: list[dict[str, object]]
) -> None:
    assert _has_clicked_consumed_replacement(authorization, attempts) is False
