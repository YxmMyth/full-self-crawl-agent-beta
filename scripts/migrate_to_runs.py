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

LEGACY_RUN_ID = f"legacy_{time.strftime('%Y%m%d', time.localtime())}"
PER_RUN_SUBDIRS = {"samples", "sessions", "verification", "research", "workspace", "transcripts"}


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

        # Backfill all NULL/empty run_id with LEGACY_RUN_ID
        for table in ("observations", "locations", "sessions", "models"):
            n = await conn.fetchval(f"""
                SELECT COUNT(*) FROM {table}
                WHERE run_id IS NULL OR run_id = ''
            """)
            if n:
                print(f"[db] backfilling {n} rows in {table} → run_id={LEGACY_RUN_ID}")
                await conn.execute(
                    f"UPDATE {table} SET run_id = $1 WHERE run_id IS NULL OR run_id = ''",
                    LEGACY_RUN_ID,
                )

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
        runs_dir = entry / "runs"
        legacy_target = runs_dir / LEGACY_RUN_ID

        # Find any per-run subdirs at the top level
        present = [p for p in entry.iterdir() if p.is_dir() and p.name in PER_RUN_SUBDIRS]
        if not present:
            print(f"[fs] {domain}: nothing to migrate")
            continue

        legacy_target.mkdir(parents=True, exist_ok=True)
        for sub in present:
            dst = legacy_target / sub.name
            if dst.exists():
                print(f"[fs] {domain}/{sub.name}: already present at runs/{LEGACY_RUN_ID}/, skip")
                continue
            try:
                shutil.move(str(sub), str(dst))
                print(f"[fs] {domain}: moved {sub.name}/ → runs/{LEGACY_RUN_ID}/{sub.name}/")
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
