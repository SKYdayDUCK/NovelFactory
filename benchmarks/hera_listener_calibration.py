"""Compare three loopback Uvicorn listener configurations with a persistent HTTP client."""
import argparse
import json
import multiprocessing
import os
from pathlib import Path
import socket
import statistics
import threading
import time

from fastapi import FastAPI
import httpx
import uvicorn


def serve(channel, mode):
    protocol = socket.IPPROTO_TCP if mode == "tcp" else 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM, protocol) as listener:
        if mode == "nodelay":
            listener.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        app = FastAPI()

        @app.get("/health")
        async def health():
            return {"status": "alive"}

        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        thread.start()
        try:
            deadline = time.monotonic() + 15
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("Uvicorn did not start")
                time.sleep(.01)
            channel.send(listener.getsockname())
            channel.recv()
        finally:
            server.should_exit = True
            thread.join(10)
            channel.close()


def measure(mode):
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    server = context.Process(target=serve, args=(child, mode))
    server.start()
    child.close()
    elapsed_ms = []
    try:
        if not parent.poll(30):
            raise RuntimeError("Uvicorn address unavailable")
        host, port = parent.recv()
        with httpx.Client(base_url=f"http://{host}:{port}", trust_env=False) as client:
            for _ in range(33):
                started = time.perf_counter_ns()
                response = client.get("/health")
                elapsed_ms.append((time.perf_counter_ns() - started) / 1e6)
                response.raise_for_status()
                if response.json() != {"status": "alive"}:
                    raise ValueError("Unexpected health payload")
        parent.send("stop")
    finally:
        parent.close()
        server.join(10)
        if server.is_alive():
            server.terminate()
            server.join()
    if server.exitcode:
        raise RuntimeError(f"Uvicorn process exited with {server.exitcode}")
    return {"mode": mode, "ms": elapsed_ms}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cpu", type=int, help="Optional Linux CPU affinity for client and server")
    args = parser.parse_args()
    if args.cpu is not None:
        os.sched_setaffinity(0, {args.cpu})
    rows = [measure(mode) for mode in ("legacy", "tcp", "nodelay", "nodelay", "tcp", "legacy")]
    args.output.write_text(json.dumps(rows) + "\n", encoding="utf-8")
    for row in rows:
        print(row["mode"], round(statistics.median(row["ms"][1:]), 2))
