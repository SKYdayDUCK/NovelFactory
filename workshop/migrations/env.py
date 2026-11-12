import os

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

url = make_url(os.environ["AW_DATABASE_URL"]).set(drivername="postgresql+psycopg")
engine = create_engine(url, poolclass=pool.NullPool)
with engine.connect() as connection:
    version, nf = connection.exec_driver_sql(
        "SELECT current_setting('server_version_num')::int, to_regclass('nf_works')"
    ).one()
    if not 170000 <= version < 180000 or nf is not None:
        raise RuntimeError("Select a separate Workshop database running PostgreSQL 17")
    connection.commit()
    context.configure(connection=connection, version_table="aw_alembic_version")
    with context.begin_transaction():
        context.run_migrations()
