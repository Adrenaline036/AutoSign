from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from autosign.core.db import Database


def database_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


def test_database_enables_wal_and_connection_pragmas(tmp_path: Path) -> None:
    database = Database(database_url(tmp_path / "autosign.db"), sqlite_busy_timeout_ms=2345)
    try:
        database.migrate()
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 2345
            assert connection.exec_driver_sql("PRAGMA synchronous").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA quick_check").scalar_one() == "ok"
    finally:
        database.dispose()


def test_busy_timeout_allows_short_write_contention(tmp_path: Path) -> None:
    database = Database(database_url(tmp_path / "autosign.db"), sqlite_busy_timeout_ms=1000)
    database.migrate()
    first = database.engine.raw_connection()
    second = database.engine.raw_connection()
    try:
        first.execute("BEGIN IMMEDIATE")
        first.execute(
            "INSERT INTO app_metadata (key, value) VALUES (?, ?)",
            ("first-writer", "held"),
        )

        def delayed_writer() -> None:
            second.execute(
                "INSERT INTO app_metadata (key, value) VALUES (?, ?)",
                ("second-writer", "waited"),
            )
            second.commit()

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(delayed_writer)
            time.sleep(0.15)
            first.commit()
            pending.result(timeout=2)
        elapsed = time.monotonic() - started

        assert 0.1 <= elapsed < 1.0
        assert second.execute(
            "SELECT value FROM app_metadata WHERE key = ?", ("second-writer",)
        ).fetchone() == ("waited",)
    finally:
        first.close()
        second.close()
        database.dispose()


def test_wal_recovers_after_abrupt_process_exit(tmp_path: Path) -> None:
    database_path = tmp_path / "autosign.db"
    script = textwrap.dedent(
        f"""
        import os
        from autosign.core.db import Database

        database = Database({database_url(database_path)!r})
        database.migrate()
        with database.session() as session:
            session.execute(
                __import__('sqlalchemy').text(
                    "INSERT INTO app_metadata (key, value) VALUES (:key, :value)"
                ),
                {{"key": "abrupt-exit", "value": "committed-in-wal"}},
            )
            session.commit()
        os._exit(0)
        """
    )
    completed = subprocess.run([sys.executable, "-c", script], check=False)
    assert completed.returncode == 0
    assert database_path.with_name(f"{database_path.name}-wal").is_file()

    database = Database(database_url(database_path))
    try:
        database.migrate()
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT value FROM app_metadata WHERE key = ?",
                ("abrupt-exit",),
            ).fetchone() == ("committed-in-wal",)
            assert connection.exec_driver_sql("PRAGMA quick_check").scalar_one() == "ok"
    finally:
        database.dispose()


def test_invalid_busy_timeout_is_rejected() -> None:
    for timeout in (-1, 60_001):
        try:
            Database("sqlite:///:memory:", sqlite_busy_timeout_ms=timeout)
        except ValueError as exc:
            assert "between 0 and 60000" in str(exc)
        else:
            raise AssertionError(f"Busy timeout {timeout} should have been rejected")
