"""Run both installed MCP services against disposable, separately owned PG17 databases."""
import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess
from uuid import uuid4

import psycopg
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy.engine import make_url

from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from tests.integration.test_p5_mcp import config

WORKSHOP = Path(__file__).resolve().parents[1]


@asynccontextmanager
async def session(params, expected_name, record_property):
    async with stdio_client(params) as streams, ClientSession(*streams) as client:
        hello = await client.initialize()
        assert hello.serverInfo.name == expected_name
        assert hello.protocolVersion == "2025-11-25"
        record_property(f"{expected_name}.initialize", json.dumps(
            {"server": hello.serverInfo.name, "version": hello.serverInfo.version,
             "protocolVersion": hello.protocolVersion}))
        yield client


def test_separate_postgres_authorities(record_property):
    nf = make_url(os.environ["NF_DATABASE_URL"]).set(drivername="postgresql")
    aw = make_url(os.environ["AW_DATABASE_URL"]).set(drivername="postgresql")
    assert nf.username != aw.username
    for own, other, own_table, other_table in (
        (nf, aw, "nf_works", "aw_installations"),
        (aw, nf, "aw_installations", "nf_works"),
    ):
        with psycopg.connect(own.render_as_string(hide_password=False)) as connection:
            assert connection.info.server_version // 10000 == 17
            flags = connection.execute(
                "SELECT rolsuper, rolcreatedb, rolcreaterole FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            assert flags == (False, False, False)
            assert connection.execute("SELECT to_regclass(%s), to_regclass(%s)",
                                      (own_table, other_table)).fetchone() == (own_table, None)
            record_property(f"{own_table}.postgres_version", connection.info.server_version)
        try:
            with psycopg.connect(other.render_as_string(hide_password=False), user=own.username,
                                 password=own.password or "", connect_timeout=5):
                raise AssertionError("A service login was allowed into the other service database")
        except psycopg.OperationalError as exc:
            # libpq may omit the SQLSTATE during startup; require a server authentication
            # or CONNECT denial, so an offline DB cannot satisfy this test.
            assert (exc.sqlstate in {"28000", "28P01", "42501"}
                    or any(reason in str(exc) for reason in (
                        "authentication failed", "permission denied for database", "does not exist",
                        "no pg_hba.conf entry",
                    )))
            record_property(f"{own_table}.cross_database_login", "denied")


def test_both_stdio_services_and_revoked_session(tmp_path, record_property):
    aw_python = Path(os.environ.get("AW_W0_PYTHON", WORKSHOP / ".venv/bin/python"))
    aw_bin = aw_python.parent
    ident = uuid4().hex
    instance = "nf-" + ident
    binding = tmp_path / "aw.json"
    replacement = tmp_path / "aw-new.json"

    def admin(*args):
        subprocess.run([aw_bin / "aw-admin", *args], check=True,
                       env={"AW_DATABASE_URL": os.environ["AW_DATABASE_URL"]})

    def aw_params(path):
        return StdioServerParameters(command=str(aw_bin / "aw-mcp"), args=["--binding", str(path)])

    async def capabilities(client):
        return await client.call_tool("aw_capabilities", {"protocol_version": "aw-nf.v1"})

    async def denied(client):
        assert not (await client.list_tools()).tools
        reply = await capabilities(client)
        assert reply.isError and reply.structuredContent == {"error": {"code": "VIEW_DENIED"}}

    admin("install", ident, instance)
    admin("enroll", ident, ident, "manager", binding, "--revision", "1")
    nf_url = os.environ["NF_DATABASE_URL"]
    db = Database(nf_url)
    try:
        store = ObjectStore(tmp_path / "objects")
        kernel = Kernel(db, store)
        world = load_fixture(kernel)
        branch = {k: world["world_ref"][k] for k in ("work_id", "branch_id")}
        grant = kernel.issue_grant(world["principal"], branch["work_id"], role_id="resume_operator",
                                   capabilities=["PROJECT_RESUME"], branch_ids=[branch["branch_id"]],
                                   expires_at=datetime.now(UTC) + timedelta(hours=1))
        nf = config(tmp_path / "nf.json", {"store": store, "url": nf_url}, grant["token"], branch)

        async def check():
            async with (session(nf, "NovelFactory", record_property) as nf_client,
                        session(aw_params(binding), "AgentWorkshop", record_property) as aw):
                nf_tools = {t.name for t in (await nf_client.list_tools()).tools}
                aw_tools = {t.name for t in (await aw.list_tools()).tools}
                assert nf_tools == {"nf_project_status", "nf_resume_project"}
                # This connection was enrolled without a Workshop organization or employee.
                assert aw_tools == {"aw_capabilities"}
                record_property("nf.project_tools", json.dumps(sorted(nf_tools)))
                record_property("aw.tools", json.dumps(sorted(aw_tools)))
                negotiated = (await capabilities(aw)).structuredContent
                assert negotiated["nf_instance_id"] == instance
                record_property("aw.negotiation", json.dumps(negotiated, sort_keys=True))
                assert not (await nf_client.call_tool("nf_project_status", {})).isError
                for client, tool in ((nf_client, "aw_capabilities"), (aw, "nf_project_status")):
                    reply = await client.call_tool(tool, {})
                    assert reply.isError and reply.structuredContent == {"error": {"code": "VIEW_DENIED"}}
                    record_property(f"cross_service.{tool}", reply.structuredContent["error"]["code"])

                admin("revoke", ident, ident, "manager")
                await denied(aw)
                admin("enroll", ident, ident, "manager", replacement, "--revision", "2")
                await denied(aw)
                async with session(aw_params(binding), "AgentWorkshop", record_property) as old:
                    await denied(old)
                async with session(aw_params(replacement), "AgentWorkshop", record_property) as current:
                    assert not (await capabilities(current)).isError
                    assert not (await nf_client.call_tool("nf_project_status", {})).isError
                    kernel.revoke_grant(world["principal"], grant["principal"].grant_id)
                    assert not (await nf_client.list_tools()).tools
                    assert not (await capabilities(current)).isError

        asyncio.run(check())
    finally:
        db.close()
