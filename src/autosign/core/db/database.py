from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker


class Database:
    def __init__(self, database_url: str) -> None:
        self.url = database_url
        self.engine = create_engine(
            database_url,
            connect_args={"check_same_thread": False},
        )
        self._session_factory = sessionmaker(
            bind=self.engine,
            expire_on_commit=False,
            autoflush=False,
        )
        self._enable_sqlite_foreign_keys(self.engine)

    @staticmethod
    def _enable_sqlite_foreign_keys(engine: Engine) -> None:
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    def migrate(self) -> None:
        import autosign.migrations

        migrations_dir = Path(autosign.migrations.__file__).resolve().parent
        config = Config()
        config.set_main_option("script_location", migrations_dir.as_posix())
        config.set_main_option("sqlalchemy.url", self.url)
        command.upgrade(config, "head")

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()

