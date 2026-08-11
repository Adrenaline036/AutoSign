from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


class Database:
    def __init__(self, database_url: str, *, sqlite_busy_timeout_ms: int = 2000) -> None:
        if not 0 <= sqlite_busy_timeout_ms <= 60_000:
            raise ValueError("SQLite busy timeout must be between 0 and 60000 milliseconds.")
        self.url = database_url
        self.sqlite_busy_timeout_ms = sqlite_busy_timeout_ms
        self.engine = create_engine(
            database_url,
            connect_args={
                "check_same_thread": False,
                "timeout": sqlite_busy_timeout_ms / 1000,
            },
        )
        self._session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            autoflush=False,
        )
        self._configure_sqlite_connections(self.engine, sqlite_busy_timeout_ms)

    @staticmethod
    def _configure_sqlite_connections(engine: Engine, busy_timeout_ms: int) -> None:
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    def _enable_and_verify_wal(self) -> None:
        with self.engine.connect() as connection:
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one()
            if str(journal_mode).lower() != "wal":
                raise RuntimeError(
                    f"SQLite refused WAL journal mode and returned {journal_mode!r}."
                )
            if connection.exec_driver_sql("PRAGMA quick_check").scalar_one() != "ok":
                raise RuntimeError("SQLite quick_check failed before migration.")

    def migrate(self) -> None:
        import autosign.migrations

        self._enable_and_verify_wal()
        migrations_dir = Path(autosign.migrations.__file__).resolve().parent
        config = Config()
        config.set_main_option("script_location", migrations_dir.as_posix())
        config.set_main_option("sqlalchemy.url", self.url)
        command.upgrade(config, "head")
        with self.engine.connect() as connection:
            if connection.exec_driver_sql("PRAGMA quick_check").scalar_one() != "ok":
                raise RuntimeError("SQLite quick_check failed after migration.")

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
