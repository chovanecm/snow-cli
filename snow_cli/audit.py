"""Audit logging for MCP tool calls.

Writes one JSON object per line to ~/.snow-run/audit.log.
Each entry records the tool name, parameters (with sensitive values redacted),
outcome, and timestamp.
"""
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


# Fields whose values should never appear in the audit log
_REDACTED_PARAMS = frozenset({"password", "pwd", "secret", "token", "client_secret", "access_token", "refresh_token"})
_REDACT_MARKER = "***REDACTED***"


def _redact(params: Dict[str, Any]) -> Dict[str, Any]:
    """Return a shallow copy of *params* with sensitive values replaced."""
    out = {}
    for key, value in params.items():
        if key.lower() in _REDACTED_PARAMS:
            out[key] = _REDACT_MARKER
        else:
            out[key] = value
    return out


def _get_log_path() -> Path:
    return Path.home() / ".snow-run" / "audit.log"


def log_tool_call(
    tool_name: str,
    params: Dict[str, Any],
    outcome: str,
    error: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """Append an audit entry as a JSON line to the audit log.

    Args:
        tool_name: MCP tool that was called.
        params: Parameters passed to the tool (will be redacted).
        outcome: "success" or "error".
        error: Error message, if any.
        duration_ms: Wall-clock time of the call in milliseconds.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "params": _redact(params),
        "outcome": outcome,
    }
    if error:
        entry["error"] = error
    if duration_ms is not None:
        entry["duration_ms"] = duration_ms

    try:
        log_path = _get_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=True) + "\n")
    except Exception:
        # Audit logging must never break the tool itself
        pass
