"""Redaction utilities for sensitive text in error messages, payloads, and logs."""

from __future__ import annotations

from gflow_cli.data.redaction import redact_error_detail

__all__ = ["redact_sensitive_diagnostic", "redact_sensitive_text"]


def redact_sensitive_text(text: str) -> str:
    """Scrub sensitive information (tokens, credentials, signed URLs) from free-text strings."""
    if not text:
        return ""
    return redact_error_detail(text)


def redact_sensitive_diagnostic(text: str) -> str:
    """Scrub a diagnostic source before its caller selects bounded excerpts."""
    if not text:
        return ""
    return redact_error_detail(text, max_chars=None)
