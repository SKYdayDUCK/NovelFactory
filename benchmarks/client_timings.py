"""HTTPX/HTTPcore observations for the pinned benchmark client, without payloads."""
import time
from contextvars import ContextVar

from httpcore._async.connection_pool import AsyncPoolRequest


class ClientTimings:
    """Record real pool waits and header-send events per HTTP attempt.

    HTTPcore's trace hook does not expose pool acquisition. The pinned 1.0.9
    wait method is observed and always restored, including cancellation/errors.
    A header-send start is a transport event, not proof of server acceptance.
    """
    def __init__(self):
        self.current = ContextVar('nf_benchmark_http_attempt', default=None)

    async def __aenter__(self):
        self.original = AsyncPoolRequest.wait_for_connection
        owner = self
        async def measured(request, timeout=None):
            started = time.perf_counter()
            failed = True
            try:
                result = await owner.original(request, timeout=timeout)
                failed = False
                return result
            finally:
                active = owner.current.get()
                if active is not None:
                    record, _ = active
                    record['client_pool_wait_ms'] += (time.perf_counter()-started)*1000
                    record['client_pool_wait_calls'] += 1
                    record['client_pool_wait_failures'] += int(failed)
        AsyncPoolRequest.wait_for_connection = measured
        return self

    async def __aexit__(self, *args):
        AsyncPoolRequest.wait_for_connection = self.original

    def begin(self, record, origin):
        record.update(client_pool_wait_ms=0.0, client_pool_wait_calls=0, client_pool_wait_failures=0)
        return self.current.set((record, origin))

    def end(self, token):
        self.current.reset(token)

    async def trace(self, name, _info):
        active = self.current.get()
        if active is None:
            return
        record, origin = active
        fields = {'http11.send_request_headers.started': 'wire_started_seconds',
                  'http11.send_request_headers.complete': 'headers_sent_seconds',
                  'http11.receive_response_headers.complete': 'response_headers_seconds'}
        if name in fields:
            record.setdefault(fields[name], round(time.monotonic()-origin, 6))
