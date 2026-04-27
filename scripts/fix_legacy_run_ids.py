"""Fix the sloppy first migration: per-domain legacy run_ids + move scripts/.

First migration tagged all 3 domains' historical data with the same run_id
'legacy_20260427'. While DB PK (domain, model_type, run_id) doesn't collide,
conceptually run_id should uniquely identify a mission system-wide.
This script fixes that.

Also moves artifacts/{domain}/scripts/ into runs/legacy_{domain}_<date>/scripts/
for consistency with the per-run rule.

Idempotent — safe to run twice.

Run: python scripts/fix_legacy_run_ids.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DATE = time.strftime("%Y%m%d", time.localtime())
OLD_LEGACY = "legacy_20260427"  # the bad shared id from the first migration


def new_legacy_id(domain: str) -> str:
    return f"legacy_{domain}_{DATE}"


async def fix_db() -> None:
    import asyncpg
    from src.config import Config

    pool = await asyncpg.create_pool(Config.DATABASE_URL, min_size=1, max_size=2)

    async with pool.acquire() as conn:
        # Find which domains have rows tagged with the old shared legacy id
        domains = await conn.fetch("""
            SELECT DISTINCT l.domain
            FROM observations o
            JOIN locations l ON o.location_id = l.id
            WHERE o.run_id = $1
            UNION
            SELECT DISTINCT domain FROM models WHERE run_id = $1
            UNION
            SELECT DISTINCT domain FROM locations WHERE run_id = $1
            UNION
            SELECT DISTINCT (
                SELECT l.domain FROM locations l
                WHERE l.id = (
                    SELECT location_id FROM observations
                    WHERE run_id = $1
                    LIMIT 1
                )
            )
            FROM sessions WHERE run_id = $1
        """, OLD_LEGACY)

        domain_list = [r["domain"] for r in domains if r["domain"]]
        print(f"[db] domains with shared legacy id: {domain_list}")

        for domain in domain_list:
            new_id = new_legacy_id(domain)
            print(f"[db]   {domain}: {OLD_LEGACY} → {new_id}")

            # observations: tied to locations.domain via JOIN
            await conn.execute("""
                UPDATE observations
                SET run_id = $1
                WHERE run_id = $2
                  AND location_id IN (SELECT id FROM locations WHERE domain = $3)
            """, new_id, OLD_LEGACY, domain)

            # locations
            await conn.execute("""
                UPDATE locations SET run_id = $1
                WHERE run_id = $2 AND domain = $3
            """, new_id, OLD_LEGACY, domain)

            # models
            await conn.execute("""
                UPDATE models SET run_id = $1
                WHERE run_id = $2 AND domain = $3
            """, new_id, OLD_LEGACY, domain)

            # sessions: no domain column directly, but session_id is in trace.
            # Best effort: leave sessions alone if they can't be attributed.
            # (sessions table lacks a domain column — can't cleanly migrate)

        # Sanity: any rows still tagged with old legacy?
        for table in ("observations", "locations", "models"):
            n = await conn.fetchval(
                f"SELECT COUNT(*) FROM {table} WHERE run_id = $1", OLD_LEGACY
            )
            if n:
                print(f"[db] WARNING: {table} still has {n} rows with run_id={OLD_LEGACY}")

    await pool.close()


def fix_filesystem() -> None:
    artifacts = ROOT / "artifacts"
    if not artifacts.exists():
        return

    for domain_dir in artifacts.iterdir():
        if not domain_dir.is_dir() or domain_dir.name.startswith("_"):
            continue

        domain = domain_dir.name

        # 1. Rename runs/legacy_20260427/ → runs/legacy_<domain>_<date>/
        old_dir = domain_dir / "runs" / OLD_LEGACY
        new_dir = domain_dir / "runs" / new_legacy_id(domain)
        if old_dir.exists() and not new_dir.exists():
            print(f"[fs] {domain}: rename runs/{OLD_LEGACY}/ → runs/{new_legacy_id(domain)}/")
            old_dir.rename(new_dir)
        elif old_dir.exists() and new_dir.exists():
            # Both exist (should never happen but defensive)
            print(f"[fs] {domain}: both old and new dirs exist, skipping")

        # 2. Move scripts/ into the legacy run dir (it should be per-run)
        scripts_dir = domain_dir / "scripts"
        target_dir = domain_dir / "runs" / new_legacy_id(domain) / "scripts"
        if scripts_dir.exists() and scripts_dir.is_dir():
            try:
                target_dir.parent.mkdir(parents=True, exist_ok=True)
                if target_dir.exists():
                    print(f"[fs] {domain}: scripts/ already moved, skipping")
                else:
                    shutil.move(str(scripts_dir), str(target_dir))
                    print(f"[fs] {domain}: moved scripts/ → runs/{new_legacy_id(domain)}/scripts/")
            except Exception as e:
                print(f"[fs] {domain}: scripts/ move failed: {e}")


async def main() -> None:
    print(f"=== Fixing legacy run_ids (domain-qualified) ===\n")

    print("--- Filesystem ---")
    fix_filesystem()

    print("\n--- DB ---")
    await fix_db()

    print("\n=== Done ===")


if __name__ == "__main__":
    asyncio.run(main())
