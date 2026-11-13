"""Run the original P2 client and schema, swapping only the spawned API."""
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path


def select_source():
    baseline = Path(os.environ["P3_CLIENT_ROOT"]).resolve()
    worker = __name__ == "__mp_main__"
    product = Path(os.environ["P3_API_ROOT"]).resolve() if worker else baseline
    sys.path[:0] = [str(product / "src"), str(product / "workshop/src"), str(baseline)]

    import benchmarks.process_server as server
    import novel_factory.interfaces.http as http

    # Both the worker constructor and client benchmark are frozen. The HTTP
    # implementation imported by each worker must come from the chosen arm.
    assert Path(server.__file__).resolve() == baseline / "benchmarks/process_server.py"
    assert Path(http.__file__).resolve() == product / "src/novel_factory/interfaces/http.py"
    receipt = {
        "role": "api" if worker else "client",
        "sha": subprocess.check_output(["git", "-C", product, "rev-parse", "HEAD"], text=True).strip(),
        "pid": os.getpid(),
        "http_module": http.__file__,
        "process_server": server.__file__,
    }
    out = Path(os.environ["P3_ARM_DIR"]) / f"process-{os.getpid()}.json"
    out.write_text(json.dumps(receipt) + "\n")


if __name__ in {"__mp_main__", "__main__"}:
    select_source()
if __name__ == "__main__":
    runpy.run_module("benchmarks.p2_behavioral_load", run_name="__main__")
