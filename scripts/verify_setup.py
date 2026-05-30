"""One-shot setup verification: DB connectivity, schema, LLM gateway.

Run:  .venv/Scripts/python.exe scripts/verify_setup.py
Exits 0 if everything passes; prints a clear PASS/FAIL per check.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config  # noqa: E402

EXPECTED_TABLES = {"locations", "observations", "sessions", "models"}


async def check_db() -> bool:
    import asyncpg

    print("\n[1] Database ----------------------------------------")
    print(f"    DATABASE_URL = {Config.DATABASE_URL}")
    try:
        conn = await asyncpg.connect(Config.DATABASE_URL)
    except Exception as e:
        print(f"    FAIL connect: {e!r}")
        return False
    try:
        ver = await conn.fetchval("SELECT version();")
        print(f"    server: {ver.split(',')[0]}")
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public';"
        )
        tables = {r["tablename"] for r in rows}
        print(f"    tables present: {sorted(tables)}")
        missing = EXPECTED_TABLES - tables
        if missing:
            print(f"    FAIL missing tables: {sorted(missing)}")
            return False
        print("    PASS — all 4 World Model tables present")
        return True
    finally:
        await conn.close()


async def check_llm() -> bool:
    from openai import AsyncOpenAI

    print("\n[2] LLM gateway -------------------------------------")
    print(f"    LLM_BASE_URL = {Config.LLM_BASE_URL}")
    print(f"    LLM_MODEL    = {Config.LLM_MODEL}")
    client = AsyncOpenAI(api_key=Config.LLM_API_KEY, base_url=Config.LLM_BASE_URL)
    try:
        resp = await client.chat.completions.create(
            model=Config.LLM_MODEL,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=20,
        )
        content = (resp.choices[0].message.content or "").strip()
        print(f"    response: {content!r}")
        print("    PASS — gateway reachable, model responded")
        return True
    except Exception as e:
        print(f"    FAIL: {e!r}")
        return False
    finally:
        await client.close()


async def main() -> int:
    print("=" * 54)
    print("Full-Self-Crawl-Agent — setup verification")
    print("=" * 54)
    db_ok = await check_db()
    llm_ok = await check_llm()
    print("\n" + "=" * 54)
    print(f"  DB  : {'PASS' if db_ok else 'FAIL'}")
    print(f"  LLM : {'PASS' if llm_ok else 'FAIL'}")
    print("=" * 54)
    return 0 if (db_ok and llm_ok) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
