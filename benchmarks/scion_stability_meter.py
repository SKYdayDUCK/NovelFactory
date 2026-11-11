"""Measure header starts for the unchanged original 600-second stable read workload.

HTTPX reports the start of writing HTTP/1.1 request headers. A single client
event loop owns the counters. The independent ASGI boundary is recorded by
the usual worker instrumentation.
"""
import gzip
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch

import httpx
import scion_paired as paired


def main():
    if sys.argv[1:] != ["--child"]:
        with patch.object(paired,"__file__",__file__):
            paired.main()
        return
    # Preserve the private fixture solely in memory. Do not expose its contents.
    import io
    raw = sys.stdin.read()
    msg = json.loads(raw)
    assert msg["case"] == "steady-500"
    clock = {"n":0,"first_ns":None,"last_ns":None}
    original_request = httpx.AsyncClient.request

    async def traced(client, method, url, **kwargs):
        extensions = dict(kwargs.get("extensions") or {})
        prev = extensions.get("trace")
        async def observe(event, info):
            if event == "http11.send_request_headers.started":
                now=time.monotonic_ns()
                if clock["first_ns"] is None:
                    clock["first_ns"] = now
                clock["last_ns"] = now
                clock["n"] += 1
            if prev is not None:
                await prev(event,info)
        extensions["trace"] = observe
        kwargs["extensions"] = extensions
        return await original_request(client, method, url, **kwargs)

    start = time.process_time()
    with patch.object(sys,"stdin",io.StringIO(raw)), patch.object(httpx.AsyncClient,"request",traced):
        paired.child()
    cpu = time.process_time() - start
    dest=Path(msg["output"]).with_suffix(".json.gz")
    content=json.loads(gzip.decompress(dest.read_bytes()))
    elapsed=(clock["last_ns"]-clock["first_ns"])/1e9 if clock["n"]>1 else None
    content["client_header_meter"]={**clock,"first_last_seconds":elapsed,
                         "headers_per_second":(clock["n"]-1)/elapsed if elapsed else None,
                         "process_cpu_seconds_including_profile_finalization":cpu}
    temp=dest.with_suffix(".tmp")
    temp.write_bytes(gzip.compress(json.dumps(content,separators=(",",":"),ensure_ascii=False).encode(),mtime=0))
    temp.replace(dest)


if __name__ == "__mp_main__" and os.environ.get("NF_SCION_METER_DIR"):
    paired.install_worker_meter()

if __name__ == "__main__":
    main()
