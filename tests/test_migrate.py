import asyncio
import uuid
from pathlib import Path

import asyncpg
import pytest

from argus.db.migrate import apply_migrations, load_migrations, run

pytestmark = pytest.mark.integration


async def _fresh_db(pg_dsn: str) -> str:
    """Create an empty database so each test controls migration state."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(pg_dsn)
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()
    return pg_dsn.rsplit("/", 1)[0] + f"/{name}"


def _write(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, sql in files.items():
        (tmp_path / name).write_text(sql)
    return tmp_path


async def test_applies_in_order_and_is_idempotent(pg_dsn: str, tmp_path: Path) -> None:
    dsn = await _fresh_db(pg_dsn)
    d = _write(
        tmp_path,
        {
            "0002_b.sql": "ALTER TABLE t ADD COLUMN b int;",
            "0001_a.sql": "CREATE TABLE t (a int);",
        },
    )
    assert await run(dsn, d) == [1, 2]
    assert await run(dsn, d) == []


async def test_concurrent_runners_apply_each_migration_once(pg_dsn: str, tmp_path: Path) -> None:
    dsn = await _fresh_db(pg_dsn)
    # A non-idempotent migration: applying it twice would fail on the duplicate table.
    d = _write(tmp_path, {"0001_a.sql": "CREATE TABLE once (id int); SELECT pg_sleep(0.2);"})
    results = await asyncio.gather(*(run(dsn, d) for _ in range(5)))
    assert sorted(len(r) for r in results) == [0, 0, 0, 0, 1]
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT count(*) FROM schema_migrations") == 1
    finally:
        await conn.close()


async def test_failed_migration_rolls_back(pg_dsn: str, tmp_path: Path) -> None:
    dsn = await _fresh_db(pg_dsn)
    d = _write(tmp_path, {"0001_bad.sql": "CREATE TABLE half (id int); SELECT 1/0;"})
    with pytest.raises(asyncpg.DivisionByZeroError):
        await run(dsn, d)
    conn = await asyncpg.connect(dsn)
    try:
        assert await conn.fetchval("SELECT to_regclass('half')") is None
        assert await conn.fetchval("SELECT count(*) FROM schema_migrations") == 0
    finally:
        await conn.close()


async def test_edited_migration_is_rejected(pg_dsn: str, tmp_path: Path) -> None:
    dsn = await _fresh_db(pg_dsn)
    d = _write(tmp_path, {"0001_a.sql": "CREATE TABLE e (id int);"})
    await run(dsn, d)
    (d / "0001_a.sql").write_text("CREATE TABLE e (id bigint);")
    conn = await asyncpg.connect(dsn)
    try:
        with pytest.raises(RuntimeError, match="changed after being applied"):
            await apply_migrations(conn, load_migrations(d))
    finally:
        await conn.close()


def test_bad_filenames_rejected(tmp_path: Path) -> None:
    _write(tmp_path, {"abc.sql": "SELECT 1;"})
    with pytest.raises(ValueError, match="bad migration filename"):
        load_migrations(tmp_path)


def test_duplicate_versions_rejected(tmp_path: Path) -> None:
    _write(tmp_path, {"0001_a.sql": "SELECT 1;", "0001_b.sql": "SELECT 1;"})
    with pytest.raises(ValueError, match="duplicate"):
        load_migrations(tmp_path)


async def test_baseline_enables_pgvector(db: asyncpg.Connection) -> None:
    assert await db.fetchval("SELECT extname FROM pg_extension WHERE extname='vector'") == "vector"
