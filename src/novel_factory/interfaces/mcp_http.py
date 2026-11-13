"""Stateless SDK transport for explicitly provisioned bindings on the shared API."""
import json

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.routing import Route

from novel_factory.application.authorization import Principal
from novel_factory.application.mcp_runtime import McpRuntime
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.private_files import read_private_bytes

from .bindings import parse_binding
from .mcp_stdio import make_server


def load_roster(path):
    """An owner-private operator manifest uses identity IDs, never provider secrets."""
    if path is None:
        return None
    roster = {}
    rows = json.loads(read_private_bytes(path, 4 * 1024 * 1024))
    if not isinstance(rows, list) or not 1 <= len(rows) <= 10000:
        raise ValueError("Remote binding roster must have 1 to 10000 entries")
    for row in rows:
        if set(row) != {"principal_id", "grant_id", "binding"}:
            raise ValueError("Invalid remote binding roster entry")
        principal = Principal(row["grant_id"], row["principal_id"])
        if principal in roster:
            raise ValueError("Duplicate remote binding")
        roster[principal] = parse_binding(row["binding"])
    return roster


class RemoteTools:
    path = "/mcp"
    methods = ("POST",)

    def __init__(self, kernel, bindings, authenticate, execute, settings):
        self.bindings = dict(bindings)
        self.kernel = kernel
        self.authenticate = authenticate
        self.execute = execute
        server = make_server(None, resolve=lambda request: request.state.nf_runtime, run=self.run)
        security = TransportSecuritySettings(allowed_hosts=list(settings.mcp_allowed_hosts),
                                             allowed_origins=list(settings.allowed_origins))
        self.manager = StreamableHTTPSessionManager(app=server, json_response=True,
                                                     stateless=True, security_settings=security)

    async def run(self, runtime, method, args, kwargs, *, kind):
        try:
            return await self.execute(runtime.principal, kind, lambda _: method(*args, **kwargs))
        except DomainError:
            raise
        except Exception:
            # SDK method errors are included in JSON-RPC responses. Preserve
            # protocol error codes without exposing database or driver text.
            raise DomainError("SERVICE_UNAVAILABLE", "Tool temporarily unavailable", True, "WAIT") from None

    async def __call__(self, scope, receive, send):
        request = Request(scope)
        try:
            principal = await self.authenticate(request)
        except DomainError:
            raise
        except Exception:
            raise DomainError("SERVICE_UNAVAILABLE", "Authentication temporarily unavailable", True, "WAIT") from None
        binding = self.bindings.get(principal)
        if binding is None:
            raise DomainError("VIEW_DENIED", "Remote binding unavailable")
        # The SDK forwards this HTTP Request to this JSON-RPC handler, even
        # when several grants run concurrently in the same server task group.
        request.state.nf_runtime = McpRuntime(self.kernel, principal, binding)
        await self.manager.handle_request(scope, receive, send)

    def route(self):
        # Disallow long-lived GET event streams. Stateless clients poll the durable ledger.
        return Route(self.path, self, methods=self.methods)
