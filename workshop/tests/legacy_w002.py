"""Create a genuine pre-ownership W2 installation and its queued business data."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

W2_SOURCES = {
    "w002": "0da5b7c8fab5321fa50d2a04a516f3fd95432539",
    "w002a": "ec713a437b28a18c965ec58da0fb4cb8503470bb",
}


def seed_historical_w002(dsn, org, tmp_path, *, installation_id=None, revision="w002"):
    """Install legacy Minimal and queue one real director-to-actor message using its original code."""
    legacy_source = W2_SOURCES[revision]
    installation = installation_id or org
    restore_root = tmp_path / "historical-w002"
    restore_root.mkdir()
    archive = restore_root / "package.tar"
    repo = Path(__file__).resolve().parents[2]
    subprocess.run(
        ["git", "archive", "--format=tar", "--output", archive, legacy_source, "workshop"],
        cwd=repo, check=True,
    )
    with tarfile.open(archive) as files:
        files.extractall(restore_root, filter="data")
    old = restore_root / "workshop"
    env = {**os.environ, "AW_DATABASE_URL": dsn, "PYTHONPATH": str(old / "src")}
    for module, args in (
        ("alembic", ["upgrade", "head"]),
        ("agent_workshop.admin", ["install", installation, "legacy-nf"]),
        ("agent_workshop.admin", ["bootstrap", org, "--preset", "Minimal"]),
    ):
        subprocess.run([sys.executable, "-m", module, *args], cwd=old, env=env, check=True)
    result = subprocess.run(
        [sys.executable, "-c", LEGACY_SEND, installation, org],
        cwd=old, env=env, text=True, capture_output=True, check=True,
    )
    receipt = json.loads(result.stdout)
    assert receipt["alembic_revision"] == revision and receipt["member_count"] == 8
    return receipt


LEGACY_SEND = """
import json, os, sys
from agent_workshop.admin import enroll
from agent_workshop.auth import authenticate, connect
from agent_workshop.messaging import register_body, send

installation, org = sys.argv[1:]
with connect(os.environ["AW_DATABASE_URL"]) as conn:
    source = authenticate(conn, enroll(conn, installation, org, "volume", 1, 1))
    body = register_body(conn, source, "legacy-body", "actor-1", "organization", "historical fixture")
    message = send(conn, source, "legacy-send", "actor-1", "ASSIGN", "organization", 1, 3600,
                   body_ref=body["body_ref"])
    count = conn.execute("SELECT count(*) AS n FROM aw_employees WHERE org_id=%s", (org,)).fetchone()["n"]
    version = conn.execute("SELECT version_num FROM aw_alembic_version").fetchone()["version_num"]
print(json.dumps(dict(installation_id=installation, org_id=org, member_count=count,
                      message_id=str(message["message_id"]), alembic_revision=version)))
"""
