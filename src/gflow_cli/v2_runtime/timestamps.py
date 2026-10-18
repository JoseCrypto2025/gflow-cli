"""Canonical UTC timestamp serialization and parsing for V2 persisted data."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Literal

_UTC_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
TimestampPrecision = Literal["auto", "seconds", "milliseconds", "microseconds"]


def format_utc_timestamp(
    value: datetime | None = None, *, timespec: TimestampPrecision = "seconds"
) -> str:
    """Format an aware instant as canonical RFC3339 UTC ending in one ``Z``."""
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    result = moment.astimezone(UTC).isoformat(timespec=timespec).replace("+00:00", "Z")
    if not _UTC_RFC3339.fullmatch(result):
        raise ValueError("timestamp could not be represented as canonical UTC RFC3339")
    return result


def parse_utc_timestamp(value: str) -> datetime:
    """Parse only canonical UTC RFC3339 values; reject local/offset/UTC-suffix variants."""
    if not _UTC_RFC3339.fullmatch(value):
        raise ValueError("timestamp must be canonical UTC RFC3339")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError("timestamp must be canonical UTC RFC3339") from exc
    offset = parsed.utcoffset()
    if parsed.tzinfo is None or offset is None or offset != timedelta(0):
        raise ValueError("timestamp must use UTC")
    return parsed.astimezone(UTC)


def utc_now(*, timespec: TimestampPrecision = "seconds") -> str:
    """Return the current UTC instant in canonical serialized form."""
    return format_utc_timestamp(datetime.now(UTC), timespec=timespec)
