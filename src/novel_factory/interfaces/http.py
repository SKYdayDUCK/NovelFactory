"""Stateless HTTP adapter for the one application kernel.

No route writes database tables. Authentication creates a server-side Principal;
all operation, plane, actor and branch checks remain in application services.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import logging
import os
import time
from typing import Callable
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError, TimeoutError as PoolTimeout

from novel_factory.contracts.registry import plain, validate_contract
from novel_factory.domain.errors import DomainError
from .admission import AdmissionController, ConnectionBudget, TokenBucketLimiter

log = logging.getLogger("novel_factory.http")


@dataclass(frozen=True)
class HttpSettings:
    max_body_bytes: int = 262144
    body_read_timeout_seconds: float = 5.0
    max_body_chunks: int = 1024
    max_inflight_requests: int = 128
    max_queued_operations: int = 64
    per_principal_concurrency: int = 2
    queue_timeout_seconds: float = 2.0
    rate_per_second: float = 20.0
    rate_burst: int = 100
    allowed_origins: tuple[str, ...] = ()


def _status(code: str) -> int:
    if code in {"UNAUTHENTICATED", "AUTHENTICATION_REQUIRED", "INVALID_TOKEN"}:
        return 401
    if code in {"VIEW_DENIED", "FORBIDDEN", "PERMISSION_DENIED", "AUTHORIZATION_FAILED", "GRANT_REVOKED", "ORIGIN_DENIED"}:
        return 403
    if code in {"RATE_LIMITED", "BACKPRESSURE"}:
        return 429
    if code in {"SERVICE_UNAVAILABLE", "POOL_TIMEOUT"}:
        return 503
    if code == "PAYLOAD_TOO_LARGE":
        return 413
    if code == "REQUEST_TIMEOUT":
        return 408
    if code in {"NOT_FOUND", "TASK_NOT_FOUND"}:
        return 404
    if code == "METHOD_NOT_ALLOWED":
        return 405
    if code == "UNSUPPORTED_MEDIA_TYPE":
        return 415
    if code in {"INVALID_ARGUMENT", "UNSUPPORTED_SCHEMA"}:
        return 422
    if code == "INTERNAL_ERROR":
        return 500
    return 409


def _error_response(error: DomainError, request_id: str) -> JSONResponse:
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store"}
    if error.code in {"BACKPRESSURE", "RATE_LIMITED", "SERVICE_UNAVAILABLE", "POOL_TIMEOUT"}:
        headers["Retry-After"] = "1"
    if _status(error.code) == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(error.as_dict(request_id), status_code=_status(error.code), headers=headers)


class RequestBoundaryMiddleware:
    """Bound request bodies and total requests before parsing or database access."""
    def __init__(self, app, settings: HttpSettings, ingress: AdmissionController):
        self.app, self.settings, self.ingress = app, settings, ingress

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = str(uuid4())
        scope.setdefault("state", {})["request_id"] = request_id
        headers = {key.lower(): value for key, value in scope["headers"]}
        started = time.monotonic()
        status_code = 500
        token_key = hashlib.sha256(headers.get(b"authorization", b"anonymous")).hexdigest()

        async def tracked_send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message["headers"] = [(key, value) for key, value in message.get("headers", [])
                                      if key.lower() not in {b"x-request-id", b"cache-control"}] + [
                    (b"x-request-id", request_id.encode()), (b"cache-control", b"no-store")]
            await send(message)

        try:
            async with self.ingress.slot(token_key):
                origin = headers.get(b"origin")
                if origin is not None and origin.decode("latin-1") not in self.settings.allowed_origins:
                    raise DomainError("ORIGIN_DENIED", "Origin is not permitted")
                declared = headers.get(b"content-length")
                if declared:
                    try:
                        length = int(declared)
                    except ValueError:
                        raise DomainError("INVALID_ARGUMENT", "Invalid request length") from None
                    if length < 0:
                        raise DomainError("INVALID_ARGUMENT", "Invalid request length")
                    if length > self.settings.max_body_bytes:
                        raise DomainError("PAYLOAD_TOO_LARGE", "Request body exceeds configured limit")
                chunks, size, count = [], 0, 0
                # An absolute deadline also defeats trickle uploads: every
                # fragment shares one total budget, not a fresh timeout.
                deadline = asyncio.get_running_loop().time() + self.settings.body_read_timeout_seconds
                while True:
                    try:
                        async with asyncio.timeout_at(deadline):
                            message = await receive()
                    except TimeoutError:
                        raise DomainError("REQUEST_TIMEOUT", "Request body deadline exceeded", True, "WAIT") from None
                    if message["type"] == "http.disconnect":
                        return
                    if message["type"] != "http.request":
                        raise DomainError("INVALID_ARGUMENT", "Invalid request body frame")
                    count += 1
                    if count > self.settings.max_body_chunks:
                        raise DomainError("INVALID_ARGUMENT", "Too many request body fragments")
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    if size > self.settings.max_body_bytes:
                        raise DomainError("PAYLOAD_TOO_LARGE", "Request body exceeds configured limit")
                    chunks.append(chunk)
                    if not message.get("more_body", False):
                        break
                body, sent = b"".join(chunks), False

                async def replay():
                    nonlocal sent
                    if not sent:
                        sent = True
                        return {"type": "http.request", "body": body, "more_body": False}
                    return await receive()

                await self.app(scope, replay, tracked_send)
        except DomainError as exc:
            await _error_response(exc, request_id)(scope, receive, tracked_send)
        finally:
            route = getattr(scope.get("route"), "path", "unmatched")
            log.info(json.dumps({"event": "http_request", "request_id": request_id,
                                 "route": route, "status": status_code,
                                 "duration_seconds": round(time.monotonic() - started, 6)}))


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


async def _body(request: Request, contract: str, version: str) -> dict:
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise DomainError("UNSUPPORTED_MEDIA_TYPE", "Content-Type must be application/json")
    try:
        value = json.loads(await request.body(), object_pairs_hook=_strict_object,
                           parse_constant=lambda value: (_ for _ in ()).throw(ValueError("non-finite number")))
    except (ValueError, UnicodeError, RecursionError):
        raise DomainError("INVALID_ARGUMENT", "Request body must be valid JSON") from None
    return validate_contract(contract, value, version)


async def _finish_short_call(function, *args):
    # A disconnected HTTP request must not release an admission slot while its
    # short database transaction still runs in the thread pool.
    task = asyncio.create_task(run_in_threadpool(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await task
        finally:
            raise


def create_app(kernel, *, query_service=None, settings: HttpSettings | None = None,
               connection_budget: ConnectionBudget | None = None) -> FastAPI:
    settings = settings or HttpSettings()
    if (settings.max_body_bytes < 1024 or settings.max_inflight_requests < 1
            or settings.body_read_timeout_seconds <= 0 or settings.max_body_chunks < 1):
        raise ValueError("invalid HTTP request budget")
    pool_size = kernel.db.engine.pool.size()
    connection_budget = connection_budget or ConnectionBudget(api_pool_size=pool_size)
    connection_budget.validate()
    if connection_budget.api_pool_size != pool_size:
        raise ValueError("declared API pool budget differs from actual SQLAlchemy pool")
    if pool_size < 2:
        raise ValueError("HTTP needs at least two pool slots for auth and operations")
    if query_service is None:
        from novel_factory.application.queries import WorldQueryService
        query_service = WorldQueryService(kernel)
    auth_capacity = min(2, pool_size - 1)
    operation_capacity = pool_size - auth_capacity
    admission = AdmissionController(
        operation_capacity, per_principal=settings.per_principal_concurrency,
        max_waiters=settings.max_queued_operations, timeout=settings.queue_timeout_seconds,
        class_limits={"query": operation_capacity, "context": min(2, operation_capacity),
                      "write": min(2, operation_capacity), "task": min(3, operation_capacity)},
    )
    authentication = AdmissionController(auth_capacity, per_principal=auth_capacity,
                                          max_waiters=settings.max_queued_operations,
                                          timeout=settings.queue_timeout_seconds)
    ingress = AdmissionController(settings.max_inflight_requests,
                                  per_principal=settings.max_inflight_requests,
                                  max_waiters=0, timeout=settings.queue_timeout_seconds)
    rate_limiter = TokenBucketLimiter(settings.rate_per_second, settings.rate_burst)
    app = FastAPI(title="Novel Factory P0/P1 kernel", version="2.0.1",
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.kernel = kernel
    app.state.query_service = query_service
    app.state.admission = admission
    app.state.authentication_admission = authentication
    app.state.ingress = ingress
    app.state.connection_budget = connection_budget
    app.add_middleware(RequestBoundaryMiddleware, settings=settings, ingress=ingress)

    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError):
        return _error_response(exc, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def request_error(request: Request, exc):
        return _error_response(DomainError("INVALID_ARGUMENT", "Request parameters are invalid"), request.state.request_id)

    @app.exception_handler(HTTPException)
    async def transport_error(request: Request, exc):
        code = "METHOD_NOT_ALLOWED" if exc.status_code == 405 else "NOT_FOUND"
        return _error_response(DomainError(code, "Requested endpoint is unavailable"), request.state.request_id)

    async def execute(request: Request, kind: str, operation: Callable):
        raw = request.headers.get("authorization", "")
        parts = raw.split()
        if len(parts) != 2 or parts[0].lower() != "bearer" or len(parts[1]) > 512:
            raise DomainError("UNAUTHENTICATED", "Valid Bearer authentication is required")
        token = parts[1]
        auth_key = hashlib.sha256(token.encode()).hexdigest()
        try:
            async with authentication.slot(auth_key):
                try:
                    principal = await _finish_short_call(kernel.authenticate, token)
                except DomainError as exc:
                    if exc.code == "VIEW_DENIED":
                        raise DomainError("UNAUTHENTICATED", "Valid Bearer authentication is required") from None
                    raise
            await rate_limiter.check(str(principal.principal_id))
            async with admission.slot(str(principal.principal_id), kind):
                result = await _finish_short_call(operation, principal)
            return JSONResponse(jsonable_encoder(plain(result), custom_encoder={Decimal: str}))
        except DomainError:
            raise
        except PoolTimeout:
            raise DomainError("POOL_TIMEOUT", "Database capacity is temporarily unavailable", True, "WAIT") from None
        except SQLAlchemyError:
            log.warning(json.dumps({"event": "database_operation_failed", "request_id": request.state.request_id}))
            raise DomainError("SERVICE_UNAVAILABLE", "Database operation is temporarily unavailable", True, "RETRY_SAME_COMMAND") from None
        except Exception as exc:
            log.error(json.dumps({"event": "application_operation_failed", "request_id": request.state.request_id,
                                  "exception_type": type(exc).__name__}))
            raise DomainError("INTERNAL_ERROR", "Application operation failed", False, "NONE") from None

    @app.get("/health")
    async def health():
        return {"status": "alive", "release": "P0/P1"}

    @app.post("/api/v1/queries/timeline")
    async def query(request: Request):
        payload = await _body(request, "UnifiedQueryRequest", "2.1")
        return await execute(request, "query", lambda principal: query_service.query(principal, payload))

    @app.post("/api/v1/queries/batch")
    async def batch(request: Request):
        payload = await _body(request, "QueryBatchRequest", "2.1")
        return await execute(request, "query", lambda principal: query_service.batch(principal, payload))

    @app.post("/api/v1/contexts/compile")
    async def compile_context(request: Request):
        payload = await _body(request, "ContextCompileRequest", "2.1")
        return await execute(request, "context", lambda principal: query_service.context_compile(principal, payload))

    @app.post("/api/v1/world/acceptance-certificates")
    async def prepare(request: Request):
        payload = await _body(request, "AcceptancePrepareRequest", "2.1")
        return await execute(request, "write", lambda principal: kernel.prepare(principal, payload))

    @app.post("/api/v1/world/accept-guarded")
    async def accept_guarded(request: Request):
        payload = await _body(request, "GuardedAcceptRequest", "2.1")
        return await execute(request, "write", lambda principal: kernel.accept(principal, payload))

    @app.post("/api/v1/world/accept")
    async def accept(request: Request):
        payload = await _body(request, "AcceptWorldRequest", "2.0")
        return await execute(request, "write", lambda principal: kernel.accept(principal, payload))

    @app.post("/api/v1/proposals")
    async def propose(request: Request):
        payload = await _body(request, "ProposalRequest", "http")
        command = payload["command"]
        return await execute(request, "write", lambda principal: kernel.propose(
            principal, payload["task_id"], payload["generation"], command["template_id"],
            command["args"], payload["idempotency_key"]))

    @app.post("/api/v1/refinements/prepare")
    async def prepare_refinement(request: Request):
        from novel_factory.application.refinement import WorldRepresentationService
        payload = await _body(request, "RefinementRequest", "refinement")
        return await execute(request, "write", lambda principal:
                             WorldRepresentationService(kernel).prepare_refinement(principal, payload))

    @app.post("/api/v1/refinements/proposals")
    async def propose_refinement(request: Request):
        from novel_factory.application.refinement import WorldRepresentationService
        payload = await _body(request, "RefinementProposeRequest", "refinement")
        return await execute(request, "write", lambda principal:
            WorldRepresentationService(kernel).propose_refinement(
                principal, payload["task_id"], payload["generation"], payload["proof_ref"], payload["idempotency_key"]))

    @app.post("/api/v1/continuity/prepare")
    async def prepare_continuity(request: Request):
        from novel_factory.application.continuity import ContinuityService
        payload = await _body(request, "ContinuityRequest", "continuity")
        return await execute(request, "write", lambda principal: ContinuityService(kernel).prepare(principal, payload))

    @app.post("/api/v1/works/{work_id}/continuity/readings")
    async def read_continuity_candidate(request: Request, work_id: str):
        from novel_factory.application.continuity import ContinuityService
        validate_contract("UUID", work_id)
        payload = await _body(request, "ContinuityReadRequest", "continuity")
        return await execute(request, "query", lambda principal: ContinuityService(kernel).read_candidate(
            principal, work_id, payload["candidate_ref"], payload["idempotency_key"]))

    @app.post("/api/v1/works/{work_id}/continuity/reviews")
    async def review_continuity(request: Request, work_id: str):
        from novel_factory.application.continuity import ContinuityService
        validate_contract("UUID", work_id)
        payload = await _body(request, "ContinuityReviewRequest", "continuity")
        return await execute(request, "write", lambda principal: ContinuityService(kernel).review(principal, work_id, payload))

    @app.post("/api/v1/works/{work_id}/continuity/publish")
    async def publish_continuity(request: Request, work_id: str):
        from novel_factory.application.continuity import ContinuityService
        validate_contract("UUID", work_id)
        payload = await _body(request, "ContinuityPublishRequest", "continuity")
        return await execute(request, "write", lambda principal: ContinuityService(kernel).publish(principal, work_id, payload))

    @app.post("/api/v1/works/{work_id}/continuity/inspect")
    async def inspect_continuity(request: Request, work_id: str):
        from novel_factory.application.continuity import ContinuityService
        validate_contract("UUID", work_id)
        payload = await _body(request, "ContinuityInspectRequest", "continuity")
        return await execute(request, "query", lambda principal: ContinuityService(kernel).inspect(
            principal, work_id, payload["snapshot_ref"], target_world_ref=payload.get("target_world_ref")))

    @app.post("/api/v1/tasks/{task_id}/continuity")
    async def read_task_continuity(request: Request, task_id: str):
        from novel_factory.application.continuity import ContinuityService
        validate_contract("UUID", task_id)
        payload = await _body(request, "ContinuityTaskReadRequest", "continuity")
        return await execute(request, "context", lambda principal: ContinuityService(kernel).read_for_task(
            principal, task_id, payload["generation"], payload["snapshot_ref"],
            budget_bytes=payload.get("budget_bytes", 100_000)))

    @app.post("/api/v1/tasks")
    async def create_task(request: Request):
        payload = await _body(request, "TaskCreateRequest", "http")
        return await execute(request, "task", lambda principal: kernel.create_task(
            principal, payload["spec"], payload["idempotency_key"]))

    @app.post("/api/v1/tasks/claim")
    async def claim_task(request: Request):
        payload = await _body(request, "TaskClaimRequest", "http")
        return await execute(request, "task", lambda principal: kernel.claim_task(
            principal, payload["worker_id"], task_id=payload.get("task_id"),
            lease_seconds=payload.get("lease_seconds", 30)))

    @app.get("/api/v1/tasks/{task_id}")
    async def get_task(request: Request, task_id: str):
        validate_contract("UUID", task_id)
        return await execute(request, "query", lambda principal: kernel.get_task(principal, task_id))

    @app.post("/api/v1/tasks/cancel")
    async def cancel_task(request: Request):
        payload = await _body(request, "TaskCancelHttpRequest", "http")
        return await execute(request, "task", lambda principal: kernel.cancel_task(
            principal, payload["task_id"], payload["generation"], payload["idempotency_key"]))

    @app.post("/api/v1/tasks/results")
    async def submit_result(request: Request):
        payload = await _body(request, "TaskResultHttpRequest", "http")
        return await execute(request, "task", lambda principal: kernel.submit_result(
            principal, payload["result"], payload["worker_id"]))

    @app.post("/api/v1/branches")
    async def fork(request: Request):
        payload = await _body(request, "ForkRequest", "http")
        return await execute(request, "write", lambda principal: kernel.fork(
            principal, payload["base_world_ref"], payload["kind"], payload["idempotency_key"]))

    @app.post("/api/v1/world/promotions")
    async def promote(request: Request):
        payload = await _body(request, "PromotionHttpRequest", "http")
        return await execute(request, "write", lambda principal: kernel.promote(
            principal, payload["old_world_ref"], payload["candidate_world_ref"], payload["idempotency_key"]))

    return app


def from_environment() -> FastAPI:
    """uvicorn novel_factory.interfaces.http:from_environment --factory."""
    from novel_factory.application.kernel import Kernel
    from novel_factory.infrastructure.database import Database
    from novel_factory.infrastructure.objects import ObjectStore

    pool = int(os.environ.get("NF_POOL_SIZE", "8"))
    overflow = int(os.environ.get("NF_POOL_OVERFLOW", "0"))
    budget = ConnectionBudget(
        api_processes=int(os.environ.get("NF_API_PROCESSES", "1")), api_pool_size=pool,
        api_pool_overflow=overflow, worker_processes=int(os.environ.get("NF_WORKER_PROCESSES", "1")),
        worker_pool_size=int(os.environ.get("NF_WORKER_POOL_SIZE", "4")),
        maintenance_reserve=int(os.environ.get("NF_MAINTENANCE_CONNECTIONS", "16")),
        postgres_max_connections=int(os.environ.get("NF_PG_MAX_CONNECTIONS", "60")),
    )
    budget.validate()
    database = Database(pool_size=pool, max_overflow=overflow,
                        pool_timeout=float(os.environ.get("NF_POOL_TIMEOUT", "2")))
    settings = HttpSettings(
        allowed_origins=tuple(value for value in os.environ.get("NF_ALLOWED_ORIGINS", "").split(",") if value),
        body_read_timeout_seconds=float(os.environ.get("NF_BODY_READ_TIMEOUT_SECONDS", "5.0")),
        max_body_chunks=int(os.environ.get("NF_MAX_BODY_CHUNKS", "1024")),
    )
    return create_app(Kernel(database, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects"))),
                      settings=settings, connection_budget=budget)
