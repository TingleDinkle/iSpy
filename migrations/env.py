"""Alembic environment — wired to tracker's settings and models.

First-time setup on an EXISTING database created by `manage.py init-db`:
    alembic stamp head
Then, for every future model change:
    alembic revision --autogenerate -m "describe the change"
    alembic upgrade head
Fresh databases can skip init-db entirely and run `alembic upgrade head`
once an initial migration exists.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from tracker.config import settings
from tracker.models import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.database_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
