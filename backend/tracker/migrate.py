"""Schema migrations with Alembic.

On startup the API and the worker bring the database to the latest revision (TRACKER_AUTO_MIGRATE=0 turns that
into a check that refuses to start on an outdated schema). Databases created before Alembic by
`Base.metadata.create_all` (the imported code or intermediate versions) are stamped at the baseline and then
upgraded, keeping their data. A cross-process lock (PostgreSQL advisory lock, lock file next to a SQLite
database) stops the API and the worker from migrating at the same time.

CLI: `python -m tracker.migrate upgrade|current|check` (works inside the container, no alembic.ini needed).
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text

MIGRATIONS = Path(__file__).parent / "migrations"
BASELINE = "0001_baseline"
PRE_ALEMBIC_TABLE = "wallets"  # present in every database the imported code created
LOCK_KEY = 4_204_202_611  # arbitrary, constant advisory-lock key


class SchemaOutdated(RuntimeError):
    pass


def alembic_config(connection=None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    if connection is not None:
        cfg.attributes["connection"] = connection
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current_revision(engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


@contextmanager
def _lock(engine):
    backend = engine.url.get_backend_name()
    if backend == "postgresql":
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": LOCK_KEY})
                conn.commit()
        return
    database = engine.url.database
    if backend == "sqlite" and database and database != ":memory:":
        try:
            import fcntl
        except ImportError:  # non-POSIX: no file lock available
            yield
            return
        with open(f"{database}.migrate.lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        return
    yield


def upgrade(engine) -> dict:
    with _lock(engine):
        before = current_revision(engine)
        stamped = False
        with engine.begin() as conn:
            inspector = inspect(conn)
            if not inspector.has_table("alembic_version") and inspector.has_table(PRE_ALEMBIC_TABLE):
                command.stamp(alembic_config(conn), BASELINE)  # pre-Alembic database: keep its data
                stamped = True
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        return {"before": before, "stamped_baseline": stamped, "after": current_revision(engine)}


def check(engine):
    current, head = current_revision(engine), head_revision()
    if current != head:
        raise SchemaOutdated(f"La base de datos está en la revisión {current or 'sin versión'} y el código espera "
                             f"{head}. Ejecuta `python -m tracker.migrate upgrade` (o activa TRACKER_AUTO_MIGRATE).")


def ensure_schema(engine):
    if os.getenv("TRACKER_AUTO_MIGRATE", "1") == "0":
        check(engine)
    else:
        upgrade(engine)


def main(argv=None):
    from .db import make_engine

    action = (argv or sys.argv[1:] or ["upgrade"])[0]
    engine = make_engine()
    if action == "upgrade":
        print(upgrade(engine))
    elif action == "current":
        print({"current": current_revision(engine), "head": head_revision()})
    elif action == "check":
        check(engine)
        print("ok")
    else:
        raise SystemExit("Uso: python -m tracker.migrate [upgrade|current|check]")


if __name__ == "__main__":
    main()
