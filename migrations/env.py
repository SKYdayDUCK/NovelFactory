"""Alembic entry point. All upgrades must run against PostgreSQL 17."""
import os

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config
url = os.environ.get("NF_DATABASE_URL") or os.environ.get("DATABASE_URL")
if not url:
    raise RuntimeError("NF_DATABASE_URL is required; no implicit production target")

if context.is_offline_mode():
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        if connection.dialect.name != "postgresql":
            raise RuntimeError("Real PostgreSQL is required")
        version = int(connection.exec_driver_sql("SHOW server_version_num").scalar_one())
        if not 170000 <= version < 180000:
            raise RuntimeError("This release is validated for PostgreSQL 17 only")
        connection.commit()
        context.configure(connection=connection, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
