import asyncio
import json
import os
import sys
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent_workshop.organization import bootstrap
from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, connect, digest, private_binding
from agent_workshop.server import PROTOCOL


def test_binding_rejects_public_files_or_symlinks(tmp_path):
    path = tmp_path / "binding"
    path.write_text('{"database_url":"test","token":"synthetic"}')
    path.chmod(0o644)
    with pytest.raises(Denied, match="INVALID_BINDING"):
        private_binding(path)
    path.chmod(0o600)
    (tmp_path / "link").symlink_to(path)
    with pytest.raises(OSError):
        private_binding(tmp_path / "link")


def test_live_negotiation_scope_and_revocation(tmp_path):
    dsn = os.environ["AW_DATABASE_URL"]
    ident = uuid4().hex
    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (ident, "synthetic-nf"))
        bootstrap(conn, ident, installation_id=ident)
        token = enroll(conn, ident, ident, "manager", 1, 1)
    path = tmp_path / "server.json"
    path.write_text(json.dumps({"database_url": dsn, "token": token}))
    path.chmod(0o600)
    params = StdioServerParameters(command=sys.executable,
        args=["-m", "agent_workshop.server", "--binding", str(path)],
        env={"PYTHONPATH": os.path.abspath("src")})

    async def check():
        async with stdio_client(params) as streams, ClientSession(*streams) as client:
            hello = await client.initialize()
            assert hello.serverInfo.name == "AgentWorkshop"
            names = {t.name for t in (await client.list_tools()).tools}
            assert "aw_capabilities" in names
            assert all(n.startswith("aw_") for n in names)
            result = await client.call_tool("aw_capabilities", {"protocol_version": PROTOCOL})
            assert result.structuredContent["nf_instance_id"] == "synthetic-nf"
            for args, code in (({"protocol_version": "unknown"}, "PROTOCOL_UNSUPPORTED"),
                               ({"protocol_version": PROTOCOL, "employee_id": "other"}, "INVALID_ARGUMENT")):
                result = await client.call_tool("aw_capabilities", args)
                assert result.isError and result.structuredContent == {"error": {"code": code}}
            with connect(dsn) as conn:
                conn.execute("UPDATE aw_bindings SET revoked=true WHERE token_digest=%s", (digest(token),))
            assert not (await client.list_tools()).tools
            result = await client.call_tool("aw_capabilities", {"protocol_version": PROTOCOL})
            assert result.isError and result.structuredContent == {"error": {"code": "VIEW_DENIED"}}
    asyncio.run(check())
