import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import DatabaseSettings
from app.db.models import Base

config = context.config
if config.config_file_name is not None:
    # Don't disable loggers that already exist (matters when tests run migrations in-process).
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations() -> None:
    # Only DATABASE_URL: migrations must not need the GitHub secrets.
    database_url = str(DatabaseSettings().database_url)
    engine = create_async_engine(database_url, poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


asyncio.run(run_migrations())
