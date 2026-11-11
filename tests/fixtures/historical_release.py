"""Run genuine, pinned release code with its own runtime files and module cache."""
import json
import os
import subprocess
import sys
import tarfile
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from novel_factory.infrastructure.database import Database


@pytest.fixture
def release_database(kernel_env):
    """An empty disposable database, ready for the archived release's own migrations."""
    name = "nf_release_" + uuid4().hex[:12]
    admin = kernel_env["admin"]
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(kernel_env["url"]).set(database=name).render_as_string(hide_password=False)
    db = Database(url)
    try:
        yield db, {**os.environ, "NF_DATABASE_URL": url}
    finally:
        db.close()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def run_release(repo, directory, revision, program, env, *, fixture_files=()):
    archive = directory / (revision + ".tar")
    subprocess.run(["git", "archive", "--format=tar", "--output", archive, revision,
                    "src", "runtime", "contracts", "migrations", "alembic.ini", *fixture_files], cwd=repo, check=True)
    source = directory / revision
    with tarfile.open(archive) as files:
        files.extractall(source, filter="data")
    bootstrap = """
from pathlib import Path
from novel_factory.application.kernel import Kernel
assert Path(Kernel.__init__.__code__.co_filename).resolve().is_relative_to(Path.cwd() / "src")
"""
    archived_env = {**env, "PYTHONPATH": str(source / "src"), "NF_OBJECT_ROOT": str(directory / "objects")}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=source, env=archived_env,
                   check=True, capture_output=True)
    return json.loads(subprocess.check_output(
        [sys.executable, "-c", bootstrap + program], cwd=source, text=True,
        env=archived_env,
    ))
