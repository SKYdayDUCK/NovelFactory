"""Workshop stdio service. The role can use pipes but must not read the private binding."""
import argparse
import asyncio
import json
import sys
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import organization
from .auth import Denied, authenticate, connect, private_binding

PROTOCOL = "aw-nf.v1"
MAX_ARGUMENT_BYTES = 32768


@cache
def catalog_binding():
    # Wheels contain the frozen manifest beside this module; source checkouts
    # retain it at the existing contracts path used by operators and CI.
    path = Path(__file__).with_name("nf-catalog.json")
    if not path.is_file():
        path = Path(__file__).resolve().parents[2] / "contracts" / "nf-catalog.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return {
        "nf_commit": manifest["nf_commit"],
        "role_catalog_hash": manifest["catalogs"]["runtime/roles/catalog.json"],
        "skill_catalog_hash": manifest["catalogs"]["runtime/skills/catalog.json"],
    }


def negotiate(conn, session, protocol_version):
    if protocol_version != PROTOCOL:
        raise Denied("PROTOCOL_UNSUPPORTED")
    instance = conn.execute("SELECT nf_instance_id FROM aw_installations WHERE installation_id=%s",
                            (session.installation_id,)).fetchone()
    return {"protocol_version": PROTOCOL, "installation_id": session.installation_id,
            "nf_instance_id": instance["nf_instance_id"], "grant_epoch": session.grant_epoch,
            "max_argument_bytes": MAX_ARGUMENT_BYTES, "max_ttl_seconds": 86400,
            **catalog_binding()}


TOOLS = {"aw_capabilities": ("Negotiate this connection's organization protocol.",
         {"type": "object", "required": ["protocol_version"], "additionalProperties": False,
          "properties": {"protocol_version": {"type": "string", "maxLength": 32}}}, negotiate), **organization.TOOLS}


def dispatch(config, name=None, arguments=None):
    with connect(config["database_url"]) as conn:
        session = authenticate(conn, config["token"])
        # Validate the member even when listing tools on an already open pipe.
        # Mutating organization commands take the exclusive lock first.
        changes = {"aw_hire", "aw_suspend", "aw_transfer", "aw_end"}
        visible = {"aw_capabilities"}
        if session.org_member:
            if name in changes:
                organization.lock_org(conn, session.org_id, write=True)
            viewer = organization.require_employee(conn, session)
            visible = set(TOOLS)
            if viewer["role_id"] != "workshop_manager":
                visible -= changes
        if name is None:
            return sorted(visible)
        if name not in visible:
            raise Denied("VIEW_DENIED")
        _, schema, handler = TOOLS[name]
        if (len(json.dumps(arguments, ensure_ascii=False).encode()) > MAX_ARGUMENT_BYTES
                or not Draft202012Validator(schema, format_checker=FormatChecker()).is_valid(arguments)):
            raise Denied("INVALID_ARGUMENT")
        return handler(conn, session, **arguments)


def error(code):
    return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=code)],
                                structuredContent={"error": {"code": code}})


async def bounded_input():
    while line := await asyncio.to_thread(sys.stdin.buffer.readline, 262145):
        if len(line) > 262144 or not line.endswith(b"\n"):
            return
        try:
            yield line.decode("utf-8")
        except UnicodeDecodeError:
            return


async def serve(config):
    server = Server("AgentWorkshop", version="0.1.0")

    @server.list_tools()
    async def list_tools():
        try:
            names = await asyncio.to_thread(dispatch, config)
        except Exception:
            return []
        return [types.Tool(name=n, description=TOOLS[n][0], inputSchema=TOOLS[n][1]) for n in names]

    @server.call_tool(validate_input=False)
    async def call(name, arguments):
        try:
            return await asyncio.to_thread(dispatch, config, name, arguments)
        except Denied as exc:
            return error(str(exc))
        except Exception:
            return error("SERVICE_UNAVAILABLE")

    async with stdio_server(stdin=bounded_input()) as streams:
        await server.run(*streams, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", required=True)
    args = parser.parse_args()
    try:
        asyncio.run(serve(private_binding(args.binding)))
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit("Workshop startup failed. Inspect the private binding and database.") from None


if __name__ == "__main__":
    main()
