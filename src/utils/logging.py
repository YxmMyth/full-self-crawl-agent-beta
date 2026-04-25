"""Structured logging setup for the entire system."""

from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any


class JsonFormatter(logging.Formatter):
    """JSON-line formatter for structured log output."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "module": record.name,
            "msg": record.getMessage(),
        }
        # Merge extra structured fields (set via logger.info("msg", extra={...}))
        for key in ("step", "tool", "session_id", "domain", "tokens",
                     "url", "error", "duration_ms", "outcome"):
            val = getattr(record, key, None)
            if val is not None:
                entry[key] = val
        if record.exc_info and record.exc_info[0] is not None:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """Human-readable colored formatter for terminal output."""

    COLORS = {
        "DEBUG": "\033[36m",     # cyan
        "INFO": "\033[32m",      # green
        "WARNING": "\033[33m",   # yellow
        "ERROR": "\033[31m",     # red
        "CRITICAL": "\033[35m",  # magenta
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelname, "")
        ts = time.strftime("%H:%M:%S", time.localtime(record.created))
        # Shorten module name: "src.agent.session" → "agent.session"
        name = record.name.replace("src.", "", 1)
        prefix = f"{color}{ts} {record.levelname:<7}{self.RESET} [{name}]"
        msg = record.getMessage()

        # Append key extra fields inline
        extras = []
        for key in ("tool", "session_id", "step", "url"):
            val = getattr(record, key, None)
            if val is not None:
                extras.append(f"{key}={val}")
        if extras:
            msg = f"{msg}  ({', '.join(extras)})"

        return f"{prefix} {msg}"


_initialized = False


def setup(level: str = "INFO", json_output: bool = False) -> None:
    """Initialize root logger. Call once at startup.

    Args:
        level: Log level name (DEBUG, INFO, WARNING, ERROR).
        json_output: If True, use JSON formatter. Otherwise human-readable console.
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    # Force UTF-8 stdout/stderr on Windows so unicode (CJK, emoji, ✓/✗) prints
    # don't crash with GBK codec errors. Affects every print() in the process.
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, TypeError):
            pass

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else ConsoleFormatter())
    root.addHandler(handler)

    # Quiet noisy third-party loggers
    for name in ("httpx", "openai", "asyncpg", "playwright", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger. Use as: logger = get_logger(__name__)"""
    return logging.getLogger(name)
