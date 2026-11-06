"""Minimal forward-only SQL migration runner.

Migrations are numbered ``NNNN_name.sql`` files applied in order, each in its own
transaction. A Postgres advisory lock serialises concurrent runners (e.g. several
containers starting at once), so a migration is never applied twice.
"""

import argparse
import asyncio
import hashlib
import sys
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from argus.config import get_settings

# Arbitrary constant key for pg_advisory_lock; unique to Argus migrations.
_LOCK_KEY = 0x41524755  # "ARGU"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()


def default_migrations_dir() -> Path:
    packaged = Path(__file__).resolve().parent.parent / "_migrations"
    if packaged.is_dir():
        return packaged
    return Path(__file__).resolve().parents[3] / "migrations"


def load_migrations(directory: Path) -> list[Migration]:
    migrations: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        prefix, _, rest = path.stem.partition("_")
        if not prefix.isdigit() or not rest:
            raise ValueError(f"bad migration filename: {path.name}")
        migrations.append(Migration(int(prefix), rest, path.read_text()))
    versions = [m.version for m in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError("duplicate migration versions")
    return migrations


async def apply_migrations(conn: asyncpg.Connection, migrations: list[Migration]) -> list[int]:
    """Apply pending migrations; return the versions applied by this call."""
    await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version     integer PRIMARY KEY,
                name        text NOT NULL,
                checksum    text NOT NULL,
                applied_at  timestamptz NOT NULL DEFAULT now()
            )
            """
        )
        rows = await conn.fetch("SELECT version, checksum FROM schema_migrations")
        applied = {r["version"]: r["checksum"] for r in rows}
        done: list[int] = []
        for m in migrations:
            if m.version in applied:
                if applied[m.version] != m.checksum:
                    raise RuntimeError(
                        f"migration {m.version}_{m.name} changed after being applied"
                    )
                continue
            async with conn.transaction():
                await conn.execute(m.sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)",
                    m.version,
                    m.name,
                    m.checksum,
                )
            done.append(m.version)
        return done
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)


async def run(dsn: str, directory: Path) -> list[int]:
    conn = await asyncpg.connect(dsn)
    try:
        return await apply_migrations(conn, load_migrations(directory))
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply Argus database migrations")
    parser.add_argument("--dsn", default=None)
    parser.add_argument("--dir", type=Path, default=None)
    args = parser.parse_args(argv)
    dsn = args.dsn or get_settings().database_url
    applied = asyncio.run(run(dsn, args.dir or default_migrations_dir()))
    print(f"applied migrations: {applied or 'none'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
