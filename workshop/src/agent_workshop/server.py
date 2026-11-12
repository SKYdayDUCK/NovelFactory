"""Workshop stdio service. The role can use pipes but must not read the private binding."""
import argparse
import asyncio
import json
import sys

from jsonschema import Draft202012Validator, FormatChecker
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from . import delegation, dynamic_skills, manager, messaging, organization, presets, review_independence, reviews_skills
from .roster_projection import catalog_binding
from .auth import Denied, authenticate, connect, private_binding

PROTOCOL = "aw-nf.v1"
MAX_ARGUMENT_BYTES = 32768


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
          "properties": {"protocol_version": {"type": "string", "maxLength": 32}}}, negotiate), **organization.TOOLS, **messaging.TOOLS, **delegation.TOOLS, **reviews_skills.TOOLS, **dynamic_skills.TOOLS, **review_independence.TOOLS, **manager.TOOLS, **presets.TOOLS}


def dispatch(config, name=None, arguments=None):
    with connect(config["database_url"]) as conn:
        session = authenticate(conn, config["token"])
        # Validate the member even when listing tools on an already open pipe.
        # Mutating organization commands take the exclusive lock first.
        changes = {"aw_hire", "aw_suspend", "aw_transfer", "aw_end", "aw_route_grant", "aw_route_revoke", "aw_preset_apply"}
        visible = {"aw_capabilities"}
        if session.org_member:
            if name in changes:
                organization.lock_org(conn, session.org_id, write=True)
            viewer = organization.require_employee(conn, session)
            visible = set(TOOLS)
            if viewer["role_id"] != "workshop_manager":
                visible -= changes | {"aw_preset_preview", "aw_skill_share", "aw_skill_revoke"}
            if (name is None or name in delegation.TOOLS) and not delegation.route(
                conn, session.org_id, session.employee_id
            ):
                visible -= delegation.TOOLS.keys()
            if viewer["role_id"] not in {"workshop_manager", "review_lead"}:
                visible -= {"aw_review_request", "aw_review_reconcile", "aw_review_independence"}
            if viewer["role_id"] not in {"workshop_manager", "review_lead", "reviewer"}:
                visible.discard("aw_review_get")
            if viewer["role_id"] != "reviewer":
                visible.discard("aw_review_decide")
            if viewer["role_id"] not in {"workshop_manager", "author"}:
                visible.discard("aw_resume_project")
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
