"""PostgreSQL async CRUD for the World Model.

Uses asyncpg for all database operations.
See: docs/WorldModel设计.md §七
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from src.config import Config
from src.utils.logging import get_logger
from src.world_model.model import Location, Observation, Session, SiteWorldModel

logger = get_logger(__name__)

_pool: asyncpg.Pool | None = None
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# ── Connection management ────────────────────────────────


async def connect(database_url: str | None = None) -> asyncpg.Pool:
    """Create connection pool and ensure tables exist."""
    global _pool
    if _pool is not None:
        return _pool

    url = database_url or Config.DATABASE_URL
    if not url:
        raise RuntimeError(
            "DATABASE_URL not configured. Set it in .env or environment."
        )

    _pool = await asyncpg.create_pool(url, min_size=2, max_size=10)
    await ensure_tables()
    logger.info("Database connected", extra={"url": url.split("@")[-1]})  # log host only
    return _pool


async def ensure_tables() -> None:
    """Create tables if they don't exist (idempotent)."""
    pool = _get_pool()
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(schema_sql)
    logger.debug("Database tables ensured")


async def close() -> None:
    """Close the connection pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("Database connection closed")


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database not connected. Call connect() first.")
    return _pool


# ── Locations CRUD ───────────────────────────────────────


async def create_location(
    domain: str,
    pattern: str,
    run_id: str | None = None,
    how_to_reach: str | None = None,
) -> Location:
    """Create a new location. ID = domain::pattern."""
    pool = _get_pool()
    loc_id = f"{domain}::{pattern}"
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO locations (id, run_id, domain, pattern, how_to_reach, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (id) DO UPDATE SET
                how_to_reach = COALESCE(EXCLUDED.how_to_reach, locations.how_to_reach),
                updated_at = EXCLUDED.updated_at
            """,
            loc_id, run_id, domain, pattern, how_to_reach, now, now,
        )

    return Location(
        id=loc_id, run_id=run_id, domain=domain, pattern=pattern,
        how_to_reach=how_to_reach, created_at=now, updated_at=now,
    )


async def get_location(location_id: str) -> Location | None:
    """Get a location by ID, without observations."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM locations WHERE id = $1", location_id
        )
    if row is None:
        return None
    return _row_to_location(row)


async def list_locations(domain: str) -> list[Location]:
    """List all locations for a domain, without observations."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM locations WHERE domain = $1 ORDER BY created_at", domain
        )
    return [_row_to_location(r) for r in rows]


async def update_location(
    location_id: str,
    how_to_reach: str | None = None,
) -> None:
    """Update a location's mutable fields."""
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE locations SET how_to_reach = COALESCE($2, how_to_reach), updated_at = $3
            WHERE id = $1
            """,
            location_id, how_to_reach, now,
        )


async def find_or_create_location(
    domain: str,
    pattern: str,
    run_id: str | None = None,
    how_to_reach: str | None = None,
) -> Location:
    """Find existing location by domain::pattern, or create it.

    Used by Recording Agent's create_observation — auto find-or-create.
    """
    loc_id = f"{domain}::{pattern}"
    existing = await get_location(loc_id)
    if existing is not None:
        return existing
    return await create_location(domain, pattern, run_id, how_to_reach)


# ── Observations CRUD ────────────────────────────────────


async def create_observation(
    location_id: str,
    raw: dict[str, Any],
    agent_step: int | None = None,
) -> Observation:
    """Create a new observation linked to a location."""
    pool = _get_pool()
    raw_json = json.dumps(raw, ensure_ascii=False)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO observations (location_id, agent_step, raw)
            VALUES ($1, $2, $3::jsonb)
            RETURNING id, created_at
            """,
            location_id, agent_step, raw_json,
        )

    return Observation(
        id=row["id"],
        location_id=location_id,
        agent_step=agent_step,
        raw=raw,
        created_at=row["created_at"],
    )


async def update_observation(observation_id: int, raw: dict[str, Any]) -> None:
    """Update an observation's raw content."""
    pool = _get_pool()
    raw_json = json.dumps(raw, ensure_ascii=False)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE observations SET raw = $2::jsonb WHERE id = $1",
            observation_id, raw_json,
        )


async def delete_observation(observation_id: int) -> None:
    """Delete an observation."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM observations WHERE id = $1", observation_id
        )


async def list_observations_by_location(location_id: str) -> list[Observation]:
    """List all observations for a location, ordered by creation time."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM observations WHERE location_id = $1 ORDER BY created_at",
            location_id,
        )
    return [_row_to_observation(r) for r in rows]


async def list_observations_by_domain(domain: str) -> list[Observation]:
    """List all observations for a domain (across all locations)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT o.* FROM observations o
            JOIN locations l ON o.location_id = l.id
            WHERE l.domain = $1
            ORDER BY o.created_at
            """,
            domain,
        )
    return [_row_to_observation(r) for r in rows]


# ── Sessions CRUD ────────────────────────────────────────


async def create_session(
    session_id: str,
    run_id: str | None = None,
    direction: str | None = None,
) -> Session:
    """Create a new session record."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (id, run_id, direction)
            VALUES ($1, $2, $3)
            """,
            session_id, run_id, direction,
        )
    return Session(id=session_id, run_id=run_id, direction=direction)


async def update_session(
    session_id: str,
    ended_at: datetime | None = None,
    outcome: str | None = None,
    steps_taken: int | None = None,
    trajectory_summary: str | None = None,
) -> None:
    """Update a session's mutable fields (called when session ends)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE sessions SET
                ended_at = COALESCE($2, ended_at),
                outcome = COALESCE($3, outcome),
                steps_taken = COALESCE($4, steps_taken),
                trajectory_summary = COALESCE($5, trajectory_summary)
            WHERE id = $1
            """,
            session_id, ended_at, outcome, steps_taken, trajectory_summary,
        )


async def get_session(session_id: str) -> Session | None:
    """Get a session by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM sessions WHERE id = $1", session_id
        )
    if row is None:
        return None
    return _row_to_session(row)


# ── Models CRUD ──────────────────────────────────────────


async def upsert_model(domain: str, model_type: str, content: str) -> None:
    """Insert or update a model document (semantic or procedural)."""
    pool = _get_pool()
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO models (domain, model_type, content, updated_at)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (domain, model_type) DO UPDATE SET
                content = EXCLUDED.content,
                updated_at = EXCLUDED.updated_at
            """,
            domain, model_type, content, now,
        )


async def load_model(domain: str, model_type: str) -> str:
    """Load a model document. Returns empty string if not found."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT content FROM models WHERE domain = $1 AND model_type = $2",
            domain, model_type,
        )
    return row["content"] if row else ""


async def load_both_models(domain: str) -> tuple[str, str]:
    """Load both semantic and procedural models. Returns ("", "") if none."""
    semantic = await load_model(domain, "semantic")
    procedural = await load_model(domain, "procedural")
    return semantic, procedural


# ── Aggregate load ───────────────────────────────────────


async def load_world_model(domain: str) -> SiteWorldModel:
    """Load the complete World Model for a domain.

    Includes all locations with their observations, plus both models.
    Returns a valid SiteWorldModel even if the domain is new (empty).
    """
    locations = await list_locations(domain)

    # Attach observations to each location
    for loc in locations:
        loc.observations = await list_observations_by_location(loc.id)

    semantic, procedural = await load_both_models(domain)

    wm = SiteWorldModel(
        domain=domain,
        locations=locations,
        semantic_model=semantic,
        procedural_model=procedural,
    )

    logger.info(
        f"Loaded World Model for {domain}: "
        f"{len(locations)} locations, {wm.observation_count} observations, "
        f"semantic={'yes' if semantic else 'empty'}, procedural={'yes' if procedural else 'empty'}",
        extra={"domain": domain},
    )
    return wm


# ── Row conversion helpers ───────────────────────────────


def _row_to_location(row: asyncpg.Record) -> Location:
    return Location(
        id=row["id"],
        run_id=row["run_id"],
        domain=row["domain"],
        pattern=row["pattern"],
        how_to_reach=row["how_to_reach"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _row_to_observation(row: asyncpg.Record) -> Observation:
    raw = row["raw"]
    # asyncpg returns JSONB as dict directly, but handle string case
    if isinstance(raw, str):
        raw = json.loads(raw)
    return Observation(
        id=row["id"],
        location_id=row["location_id"],
        agent_step=row["agent_step"],
        raw=raw,
        created_at=row["created_at"],
    )


def _row_to_session(row: asyncpg.Record) -> Session:
    return Session(
        id=row["id"],
        run_id=row["run_id"],
        direction=row["direction"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        outcome=row["outcome"],
        steps_taken=row["steps_taken"],
        trajectory_summary=row["trajectory_summary"],
    )
