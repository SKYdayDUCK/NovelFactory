"""Upgrade a real W0 installation, then enroll W1 membership through the trusted CLI."""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import subprocess
import sys
import tarfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import psycopg

WORKSHOP = Path(__file__).resolve().parents[1]
W0 = "c4939e24b6b5501d82127f528f2054e9c1992856"


def run(package, dsn, module, *args):
    subprocess.run(
        [sys.executable, "-m", module, *args], cwd=package, check=True,
        env={**os.environ, "AW_DATABASE_URL": dsn, "PYTHONPATH": str(package / "src")},
    )


@asynccontextmanager
async def client(package, binding):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "agent_workshop.server", "--binding", str(binding)],
        cwd=package, env={"PYTHONPATH": str(package / "src")},
    )
    async with stdio_client(params) as streams, ClientSession(*streams) as session:
        hello = await session.initialize()
        assert hello.serverInfo.name == "AgentWorkshop" and hello.protocolVersion == "2025-11-25"
        yield session


async def only_capabilities(session):
    assert {t.name for t in (await session.list_tools()).tools} == {"aw_capabilities"}
    result = await session.call_tool("aw_capabilities", {"protocol_version": "aw-nf.v1"})
    assert not result.isError and result.structuredContent["nf_instance_id"] == "historical-nf"
    denied = await session.call_tool("aw_roster_get", {})
    assert denied.isError and denied.structuredContent == {"error": {"code": "VIEW_DENIED"}}


def test_w0_binding_survives_upgrade_without_acquiring_membership(tmp_path, record_property):
    # Operator-provisioned, empty disposable DB. Never reset the Workshop service DB.
    dsn = os.environ["AW_UPGRADE_DATABASE_URL"]
    with psycopg.connect(dsn) as conn:
        assert conn.info.server_version // 10000 == 17
        assert conn.execute("SELECT to_regclass('aw_alembic_version')").fetchone() == (None,)

    archive = tmp_path / "w0.tar"
    subprocess.run(["git", "archive", "--format=tar", "--output", archive, W0, "workshop"],
                   cwd=WORKSHOP.parent, check=True)
    with tarfile.open(archive) as items:
        items.extractall(tmp_path, filter="data")
    old = tmp_path / "workshop"
    legacy = tmp_path / "legacy.json"
    member = tmp_path / "member.json"
    run(old, dsn, "alembic", "upgrade", "head")
    run(old, dsn, "agent_workshop.admin", "install", "installation", "historical-nf")
    run(old, dsn, "agent_workshop.admin", "enroll", "installation", "org", "manager", legacy, "--revision", "1")
    columns = ("SELECT token_digest, installation_id, org_id, employee_id, employment_revision, "
               "grant_epoch, expires_at, revoked FROM aw_bindings")
    with psycopg.connect(dsn) as conn:
        assert conn.execute("SELECT version_num FROM aw_alembic_version").fetchone() == ("w000",)
        assert conn.execute("SELECT to_regclass('aw_employees')").fetchone() == (None,)
        before = conn.execute(columns).fetchall()
        assert len(before) == 1
    record_property("archived_w0_sha", W0)

    async def verify():
        async with client(old, legacy) as existing:
            await only_capabilities(existing)
            run(WORKSHOP, dsn, "alembic", "upgrade", "head")
            with psycopg.connect(dsn) as conn:
                assert conn.execute(columns).fetchall() == before
                assert conn.execute("SELECT count(*) FROM aw_employees").fetchone() == (0,)
                assert conn.execute("SELECT org_member FROM aw_bindings").fetchone() == (False,)
                revision = conn.execute("SELECT version_num FROM aw_alembic_version").fetchone()[0]
                record_property("upgraded_revision", revision)
            await only_capabilities(existing)
            async with client(WORKSHOP, legacy) as reconnected:
                await only_capabilities(reconnected)
                run(WORKSHOP, dsn, "agent_workshop.admin", "bootstrap", "org", "--installation", "installation")
                run(WORKSHOP, dsn, "agent_workshop.admin", "enroll", "installation", "org",
                    "manager", member, "--revision", "1")
                # Identical installation, employee, organization and revision across both
                # credentials. Only the new enrollment authenticates as a roster member.
                await only_capabilities(reconnected)
                async with client(WORKSHOP, member) as active:
                    roster = await active.call_tool("aw_roster_get", {})
                    assert not roster.isError and roster.structuredContent["org_id"] == "org"
                    assert any(e["employee_id"] == "manager" and e["status"] == "ACTIVE"
                               for e in roster.structuredContent["employees"])
                    run(WORKSHOP, dsn, "agent_workshop.admin", "revoke", "installation", "org", "manager")
                    for session in (existing, reconnected, active):
                        assert not (await session.list_tools()).tools
                        result = await session.call_tool("aw_capabilities", {"protocol_version": "aw-nf.v1"})
                        assert result.isError and result.structuredContent == {"error": {"code": "VIEW_DENIED"}}
    asyncio.run(verify())
