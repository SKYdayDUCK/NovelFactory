"""REST and Streamable HTTP for the pre-authorized Workshop delegation contract."""
from hashlib import sha256
from uuid import UUID

from fastapi import Request
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy.exc import SQLAlchemyError
from starlette.responses import JSONResponse
from starlette.routing import Route

from novel_factory.application.delegation import SCHEMAS, Delegations
from novel_factory.domain.errors import DomainError
from .delegation_stdio import _DESCRIPTIONS
from .mcp_stdio import tool_error


def mount_delegations(app, kernel, *, authenticate_slots, admission, rate, short_call, read_body, settings):
    async def authenticate(request, installation):
        parts = request.headers.get("authorization", "").split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or not 16 <= len(parts[1]) <= 256:
            raise DomainError("UNAUTHENTICATED", "Installation credential required")
        token = parts[1]
        adapter = Delegations(kernel, str(installation), token)

        def check():
            with kernel.db.uow(write=False) as repo:
                adapter.identity(repo)

        async with authenticate_slots.slot(sha256(token.encode()).hexdigest()):
            try:
                await short_call(check)
            except SQLAlchemyError:
                raise DomainError("SERVICE_UNAVAILABLE", "Installation lookup unavailable", True, "WAIT") from None
        await rate.check("installation:" + str(installation))
        return adapter

    async def call(adapter, method, payload):
        creating = method == "nf_delegation_submit"
        async with admission.slot("installation:" + adapter.installation_id, "task" if creating else "query"):
            # Recheck current installation, scope, role and budget in the same
            # transaction that binds the task, supervisor job and operation receipt.
            try:
                return await short_call(adapter.call, method, payload)
            except SQLAlchemyError:
                raise DomainError("SERVICE_UNAVAILABLE", "Retry the original operation", True, "WAIT") from None

    async def rest(request, installation, method):
        adapter = await authenticate(request, installation)
        result = await call(adapter, method, await read_body(request))
        return JSONResponse(result, status_code=202 if method == "nf_delegation_submit" else 200)

    @app.post("/api/v1/hera/installations/{installation}/delegations/submit")
    async def submit(request: Request, installation: UUID):
        return await rest(request, installation, "nf_delegation_submit")

    @app.post("/api/v1/hera/installations/{installation}/delegations/status")
    async def status(request: Request, installation: UUID):
        return await rest(request, installation, "nf_delegation_get")

    sdk = Server("NovelFactory.Delegation", version="1.0")

    @sdk.list_tools()
    async def tools():
        return [types.Tool(name=name, description=_DESCRIPTIONS[name], inputSchema=schema)
                for name, schema in SCHEMAS.items()]

    @sdk.call_tool(validate_input=False)
    async def invoke(name, arguments):
        try:
            return await call(sdk.request_context.request.state.installation, name, arguments)
        except DomainError as exc:
            result = tool_error(exc.code)
            if exc.retryable and exc.code in {"BACKPRESSURE", "RATE_LIMITED", "SERVICE_UNAVAILABLE", "POOL_TIMEOUT"}:
                result.structuredContent["error"].update(
                    retryable=True, retry_after_ms=1000, recovery=exc.recovery,
                    retry_same_operation=True,
                )
            return result

    security = TransportSecuritySettings(allowed_hosts=list(settings.mcp_allowed_hosts),
                                         allowed_origins=list(settings.allowed_origins))
    manager = StreamableHTTPSessionManager(app=sdk, json_response=True, stateless=True, security_settings=security)

    class Endpoint:
        async def __call__(self, scope, receive, send):
            request = Request(scope)
            request.state.installation = await authenticate(request, request.path_params["installation"])
            await manager.handle_request(scope, receive, send)

    app.router.routes.append(Route("/mcp/installations/{installation:uuid}", Endpoint(), methods=["POST"]))
    return manager
