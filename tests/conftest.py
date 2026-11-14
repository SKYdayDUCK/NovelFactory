"""Real, isolated PostgreSQL test databases. Missing PG is a failure, not a skip."""
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


class KernelEnvironment(dict):
    def __repr__(self):
        return "KernelEnvironment(<isolated PostgreSQL credentials redacted>)"


@pytest.fixture(autouse=True)
def isolate_model_authorization(monkeypatch):
    # Local deployment runners may carry production approval. A test that
    # exercises the live boundary must set its own flag and synthetic backend;
    # a missed mock must not inherit permission to spend from the operator.
    monkeypatch.delenv("NF_RUNTIME_LIVE_APPROVED", raising=False)


@pytest.fixture(scope="module")
def kernel_env(tmp_path_factory):
    url=os.environ.get("NF_TEST_DATABASE_URL")
    if not url:pytest.fail("PostgreSQL 17 is required; use scripts/with_postgres.py")
    name="nf_kernel_"+uuid4().hex[:16]
    admin=create_engine(make_url(url).set(database="postgres"),isolation_level="AUTOCOMMIT")
    with admin.connect() as c:c.execute(text(f'CREATE DATABASE "{name}"'))
    target=make_url(url).set(database=name).render_as_string(hide_password=False)
    root=Path(__file__).resolve().parents[1]
    subprocess.run([sys.executable,"-m","alembic","upgrade","head"],cwd=root,env={**os.environ,"NF_DATABASE_URL":target},check=True)
    db=Database(target,pool_size=8);store=ObjectStore(tmp_path_factory.mktemp("kernel_objects"));kernel=Kernel(db,store)
    yield KernelEnvironment(db=db,store=store,kernel=kernel,url=target,admin=admin)
    db.close()
    with admin.connect() as c:c.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()
