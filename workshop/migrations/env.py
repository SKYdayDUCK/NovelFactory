import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

url = make_url(os.environ["AW_DATABASE_URL"]).set(drivername="postgresql+psycopg")
engine = create_engine(url, poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, version_table="aw_alembic_version")
    with context.begin_transaction():
        context.run_migrations()
