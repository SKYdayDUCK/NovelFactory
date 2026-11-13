# Loopback HTTP listener calibration

A prebound stream listener with protocol 0 added about 43 ms to short persistent HTTP requests on this Linux host. CPython 3.12's asyncio sets TCP_NODELAY on accepted Internet stream sockets only when the socket protocol is IPPROTO_TCP. The previous benchmark provided Uvicorn with a socket created with the default protocol 0. Under the pinned H11 implementation, Uvicorn writes the response headers and body separately. Explicitly selecting IPPROTO_TCP or enabling TCP_NODELAY on the listener removed this measured delay.

The calibration used two separate processes on CPU 3, one fast HTTPX client, a persistent connection, no database and a verified JSON health response. The final spawned-process A/B/C/C/B/A measured 33 requests per arm. The first establishes the connection. Across the remaining 32, the two warm medians were 43.94 and 44.03 ms with protocol 0, 1.16 and 1.04 ms with IPPROTO_TCP, and 0.99 and 1.22 ms with TCP_NODELAY. All 198 replies were correct. The earlier fork-based run also verified all replies. These measurements concern the transport-only control. They supply no count of authenticated reads, committed tasks or production model calls. [Spawned-process raw intervals](raw/listener/health-spawn-abccba.json) · [Earlier experiment](raw/listener/health-abccba.json)

The affected launchers are the shared HTTP loopback benchmark, isolated API service process harness and transport-only client calibration. The 200-agent behavioral test can use either of the first two. The independent 1,000-agent open-arrival driver uses the isolated process harness. A single-process Uvicorn launch with its ordinary host and port uses asyncio's server bind path. Other socket-activation and multi-worker deployments have their own listener and require measurement.

For a business throughput comparison, run both product revisions with the same launcher and socket setting. Historical capacity probes and their original gates retain their recorded outcomes. A live performance gain between legacy and corrected sockets on the same product supplies no application-level HERA-versus-main result.

```sh
python -m benchmarks.hera_listener_calibration --cpu 3 --output listener.json
```

Omit the optional affinity setting on hosts without Linux CPU affinity. The raw file includes the first request and every subsequent response time in milliseconds.
