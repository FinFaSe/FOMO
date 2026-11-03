"""Alembic environment. The URL comes from DATABASE_URL (same default as the app); a connection passed by
tracker.migrate is used as is. Batch mode keeps ALTER TABLE working on SQLite."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from tracker.db import Base, resolve_url

config = context.config
target_metadata = Base.metadata
connection = config.attributes.get("connection")
if config.config_file_name is not None and connection is None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def run(conn):
    context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url=config.get_main_option("sqlalchemy.url") or resolve_url(), target_metadata=target_metadata,
                      literal_binds=True, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
elif connection is not None:
    run(connection)
else:
    engine = create_engine(config.get_main_option("sqlalchemy.url") or resolve_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        run(conn)
        conn.commit()
