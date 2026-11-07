"""asyncpg connection pool helpers."""

import asyncpg

# Role for user-facing API queries: no BYPASSRLS, so row-level security applies.
APP_ROLE = "argus_app"


async def create_pool(
    dsn: str, *, min_size: int = 1, max_size: int = 10, role: str | None = None
) -> asyncpg.Pool:
    """Create a pool. With ``role``, every connection runs as that role: it is sent as a
    startup setting, so asyncpg's ``RESET ALL`` on release returns to it rather than to
    the login role."""
    settings = {"role": role} if role else None
    pool = await asyncpg.create_pool(
        dsn, min_size=min_size, max_size=max_size, server_settings=settings
    )
    if pool is None:  # pragma: no cover - asyncpg only returns None when awaited lazily
        raise RuntimeError("failed to create database pool")
    return pool
