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
    def __init__(self, kernel, bindings, authenticate, execute, settings, coalescer):
        self.bindings = bindings
        self.kernel = kernel
        self.authenticate = authenticate
        self.execute = execute
        self.settings = settings
        self.coalescer = coalescer
        server = make_server(None, resolve=lambda request: request.state.nf_runtime, run=self.run)
        security = TransportSecuritySettings(allowed_hosts=list(settings.mcp_allowed_hosts),
                                             allowed_origins=list(settings.allowed_origins))
        self.manager = StreamableHTTPSessionManager(app=server, json_response=True,
                                                     stateless=True, security_settings=security)

    async def run(self, runtime, method, args, kwargs, *, kind):
        return await self.execute(runtime.principal, kind, lambda _: method(*args, **kwargs))

    async def __call__(self, scope, receive, send):
        request = Request(scope)
        principal = await self.authenticate(request)
        binding = self.bindings.get(principal)
        if binding is None:
            raise DomainError("VIEW_DENIED", "Remote binding unavailable")
        # The SDK forwards this HTTP Request to this JSON-RPC handler, even
        # when several grants run concurrently in the same server task group.
        request.state.nf_runtime = McpRuntime(self.kernel, principal, binding,
                                              compact_role_reads=self.settings.compact_remote_role_reads,
                                              fenced_task_status=self.settings.fenced_task_status,
                                              fuse_status_auth=self.settings.fuse_status_auth,
                                              status_coalescer=self.coalescer)
        await self.manager.handle_request(scope, receive, send)

    def route(self):
        # Disallow long-lived GET event streams. Stateless clients poll the durable ledger.
        return Route("/mcp", self, methods=["POST"])
