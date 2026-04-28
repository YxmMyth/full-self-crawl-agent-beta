"""Firefox / Camoufox profile management.

The profile dir under `artifacts/_profiles/{domain}/` is layered:

  Tier 1 (auth, persistent identity):
    cookies.sqlite*, places.sqlite, webappsstore.sqlite, storage/, permissions.sqlite
    → never touched. Survives runs, survives mid-mission interrupts.

  Tier 2 (our config):
    user.js
    → idempotently overwritten on every launch.

  Tier 3 (volatile session state):
    sessionstore.jsonlz4, sessionstore-backups/, parent.lock, Cache/, cache2/, crashes/
    → cleaned at every launch. Stale Tier 3 files are what cause the 180s
       sessionstore-restore hang we hit on 2026-04-27.

  Tier 4 (Firefox metadata, self-managed):
    xulstore.json, times.json, compatibility.ini, addonStartup.json.lz4
    → not touched.

See: docs/postmortem-2026-04-27-deferred.md, docs/fix-plan-2026-04-27-P0.md
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.utils.logging import get_logger

logger = get_logger(__name__)


# Disable Firefox session restore.
#
# Tested against Mozilla's gecko-dev/browser/app/profile/firefox.js defaults.
# All five prefs together prevent Firefox from trying to restore tabs from a
# prior session — which on a 30-tab profile blocks launch_persistent_context
# past the 180s Playwright timeout.
SESSION_RESTORE_OFF: dict[str, Any] = {
    # 0 = blank page on startup, NOT "resume previous session" (3)
    "browser.startup.page": 0,
    # don't try to restore tabs after a kill/crash
    "browser.sessionstore.resume_from_crash": False,
    # zero auto-resumes after crashes
    "browser.sessionstore.max_resumed_crashes": 0,
    # defensive: don't honor a one-shot resume flag
    "browser.sessionstore.resume_session_once": False,
    # Playwright standard: don't enter safe mode after repeated crashes
    "toolkit.startup.max_resumed_crashes": -1,
}


def write_user_js(profile_dir: Path, prefs: dict[str, Any] | None = None) -> None:
    """Write user.js to the profile dir.

    Firefox reads user.js at startup, BEFORE session-restore is decided —
    making it earlier than `firefox_user_prefs` injection (which goes through
    the Juggler protocol after the browser is up). user.js is the canonical
    place to override defaults like sessionstore behavior.

    Idempotent — safe to call on every launch.
    """
    profile_dir.mkdir(parents=True, exist_ok=True)
    prefs = prefs if prefs is not None else SESSION_RESTORE_OFF
    lines = [
        f"user_pref({json.dumps(k)}, {json.dumps(v)});" for k, v in prefs.items()
    ]
    (profile_dir / "user.js").write_text("\n".join(lines) + "\n", encoding="utf-8")


# Tier 3 files & dirs to clean before every launch.
# Files: removed if present.
# Dirs: contents removed (dir kept so Firefox doesn't recreate w/ different perms).
_TIER3_FILES = (
    "parent.lock",
    "sessionstore.jsonlz4",
)
_TIER3_DIR_CONTENTS = (
    "sessionstore-backups",
)


def sanitize_profile(profile_dir: Path) -> None:
    """Clean Tier 3 (volatile session state) before launching.

    Survives prior unclean shutdown (kill -9, crash, Ctrl-C). Tier 1 (cookies
    & friends) is never touched, so login state is preserved.

    Cache/ and crashes/ are NOT cleaned — they're harmless and self-managed.
    Cleaning them just adds startup latency.
    """
    if not profile_dir.exists():
        return

    cleaned: list[str] = []

    for name in _TIER3_FILES:
        p = profile_dir / name
        if p.exists():
            try:
                p.unlink()
                cleaned.append(name)
            except Exception as e:
                logger.warning(f"Could not remove stale {name}: {e}")

    for name in _TIER3_DIR_CONTENTS:
        d = profile_dir / name
        if d.exists() and d.is_dir():
            removed = 0
            for f in d.iterdir():
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass
            if removed:
                cleaned.append(f"{name}/({removed} files)")

    if cleaned:
        logger.debug(
            f"Sanitized profile: cleared {', '.join(cleaned)}",
            extra={"profile_dir": str(profile_dir)},
        )
