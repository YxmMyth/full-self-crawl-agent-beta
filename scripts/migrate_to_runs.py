"""One-time migration: per-domain artifacts → per-run subdirs + DB run_id.

Idempotent — safe to run twice.

What it does:
  1. DB schema:
     - ALTER observations ADD COLUMN run_id (if not exists)
     - ALTER models: drop PK, add run_id, recreate PK on (domain, model_type, run_id)
     - Indexes
  2. DB data:
     - UPDATE observations / locations / sessions / models
       SET run_id = 'legacy_<TS>' WHERE run_id IS NULL or empty
  3. Filesystem:
     - For each artifacts/{domain}/ that has subdirs (samples/, sessions/, etc.),
       move them into artifacts/{domain}/runs/legacy_<TS>/
     - Skip _profiles/ (domain-level utility, not run content)
     - Skip already-migrated domains (those with runs/ subdir already present)

Run: python scripts/migrate_to_runs.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

# Per-domain legacy id avoids the cross-domain "same string" confusion.
# Legacy id is generated per-domain in fix_filesystem(), not used here.
LEGACY_DATE = time.strftime("%Y%m%d", time.localtime())

# Use a denylist (skip these), not allowlist — anything not `runs/` and
# not a `_*` utility dir gets moved into the legacy run dir.
SKIP_TOP_LEVEL = {"runs"}


async def migrate_db() -> None:
    """Bypass db.connect() — it runs ensure_tables which fails on legacy schema."""
    import asyncpg
    from src.config import Config

    pool = await asyncpg.create_pool(Config.DATABASE_URL, min_size=1, max_size=2)
    async with pool.acquire() as conn:
        # observations.run_id column (if missing)
        col_exists = await conn.fetchval("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'observations' AND column_name = 'run_id'
        """)
        if not col_exists:
            print("[db] adding observations.run_id column...")
            await conn.execute("ALTER TABLE observations ADD COLUMN run_id TEXT")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_observations_run ON observations(run_id)")
        else:
            print("[db] observations.run_id already exists")

        # models: change PK to include run_id
        models_has_run_id = await conn.fetchval("""
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'models' AND column_name = 'run_id'
        """)
        if not models_has_run_id:
            print("[db] migrating models PK to include run_id...")
            await conn.execute("ALTER TABLE models ADD COLUMN run_id TEXT")
            # Set legacy run_id for existing rows BEFORE recreating PK
            await conn.execute("UPDATE models SET run_id = $1 WHERE run_id IS NULL", LEGACY_RUN_ID)
            await conn.execute("ALTER TABLE models ALTER COLUMN run_id SET NOT NULL")
            # Drop old PK, add new
            try:
                await conn.execute("ALTER TABLE models DROP CONSTRAINT models_pkey")
            except Exception as e:
                print(f"  (drop old PK skipped: {e})")
            await conn.execute("ALTER TABLE models ADD PRIMARY KEY (domain, model_type, run_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_models_domain ON models(domain)")
        else:
            print("[db] models.run_id already exists")

        # Backfill NULL/empty run_id with per-domain legacy ids.
        # observations/locations: tied to a domain via locations table.
        # models: domain is in the row directly.
        # sessions: no domain column — best effort: leave NULL or use a generic legacy id.

        # Backfill locations first (used by JOIN below)
        domains_loc = await conn.fetch("""
            SELECT DISTINCT domain FROM locations
            WHERE run_id IS NULL OR run_id = ''
        """)
        for r in domains_loc:
            domain = r["domain"]
            new_id = f"legacy_{domain}_{LEGACY_DATE}"
            n = await conn.fetchval("""
                UPDATE locations SET run_id = $1
                WHERE (run_id IS NULL OR run_id = '') AND domain = $2
                RETURNING (SELECT COUNT(*) FROM locations WHERE run_id = $1 AND domain = $2)
            """, new_id, domain)
            print(f"[db] locations({domain}): backfilled run_id={new_id}")

        # Backfill observations via locations.domain
        domains_obs = await conn.fetch("""
            SELECT DISTINCT l.domain FROM observations o
            JOIN locations l ON o.location_id = l.id
            WHERE o.run_id IS NULL OR o.run_id = ''
        """)
        for r in domains_obs:
            domain = r["domain"]
            new_id = f"legacy_{domain}_{LEGACY_DATE}"
            await conn.execute("""
                UPDATE observations SET run_id = $1
                WHERE (run_id IS NULL OR run_id = '')
                  AND location_id IN (SELECT id FROM locations WHERE domain = $2)
            """, new_id, domain)
            print(f"[db] observations({domain}): backfilled run_id={new_id}")

        # Backfill models (has domain column directly)
        domains_models = await conn.fetch("""
            SELECT DISTINCT domain FROM models
            WHERE run_id IS NULL OR run_id = ''
        """)
        for r in domains_models:
            domain = r["domain"]
            new_id = f"legacy_{domain}_{LEGACY_DATE}"
            await conn.execute("""
                UPDATE models SET run_id = $1
                WHERE (run_id IS NULL OR run_id = '') AND domain = $2
            """, new_id, domain)
            print(f"[db] models({domain}): backfilled run_id={new_id}")

        # Sessions: no domain column. Use generic legacy id, will be orphaned but
        # not catastrophic — sessions are mainly for trace lookup, not Model logic.
        n = await conn.fetchval(
            "SELECT COUNT(*) FROM sessions WHERE run_id IS NULL OR run_id = ''"
        )
        if n:
            generic = f"legacy_unknown_{LEGACY_DATE}"
            await conn.execute(
                "UPDATE sessions SET run_id = $1 WHERE run_id IS NULL OR run_id = ''",
                generic,
            )
            print(f"[db] sessions: backfilled {n} rows with generic run_id={generic}")

    await pool.close()


def migrate_filesystem() -> None:
    artifacts = ROOT / "artifacts"
    if not artifacts.exists():
        print("[fs] no artifacts/ dir, skip")
        return

    for entry in artifacts.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith("_"):
            # _profiles, _camoufox_test, etc. — domain-level utility
            continue

        domain = entry.name
        legacy_run_id = f"legacy_{domain}_{LEGACY_DATE}"
        runs_dir = entry / "runs"
        legacy_target = runs_dir / legacy_run_id

        # Move EVERYTHING at top level except `runs/` itself
        movable = [p for p in entry.iterdir() if p.is_dir() and p.name not in SKIP_TOP_LEVEL]
        if not movable:
            print(f"[fs] {domain}: nothing to migrate")
            continue

        legacy_target.mkdir(parents=True, exist_ok=True)
        for sub in movable:
            dst = legacy_target / sub.name
            if dst.exists():
                print(f"[fs] {domain}/{sub.name}: already present at runs/{legacy_run_id}/, skip")
                continue
            try:
                shutil.move(str(sub), str(dst))
                print(f"[fs] {domain}: moved {sub.name}/ → runs/{legacy_run_id}/{sub.name}/")
            except Exception as e:
                print(f"[fs] {domain}/{sub.name}: move failed: {e}")


async def main() -> None:
    print(f"=== Migration to run_id-scoped artifacts (legacy run_id = {LEGACY_RUN_ID}) ===\n")

    print("\n--- Filesystem migration ---")
    migrate_filesystem()

    print("\n--- DB migration ---")
    try:
        await migrate_db()
    except Exception as e:
        print(f"[db] ERROR: {e}")
        raise

    print("\n=== Migration complete ===")


if __name__ == "__main__":
    asyncio.run(main())
