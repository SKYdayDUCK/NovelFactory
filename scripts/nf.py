"""Operate a NovelFactory checkout using its active Python interpreter."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = {
    "init-sample": "bootstrap_p5_m0.py",
    "init-book": "p58_bootstrap_book.py",
    "grant-resume": "provision_resume.py",
    "supervisor": "run_supervisor.py",
    "processor": "run_processor.py",
}
MODULES = {"migrate": "alembic", "call": "novel_factory.interfaces.bound_call",
           "mcp": "novel_factory.interfaces.mcp_stdio"}


def main():
    parser = argparse.ArgumentParser(description=__doc__, epilog="Use COMMAND --help for its own options.")
    parser.add_argument("command", choices=[*SCRIPTS, *MODULES, "serve", "doctor"])
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "doctor":
        from agent_runtime.host_process import resolve_host_binary

        modules = ["sqlalchemy", "alembic", "psycopg", "mcp", "redis", "agent_runtime"]
        if os.name == "nt":
            modules.extend(["win32file", "win32security", "win32job"])
        ready = {name: importlib.util.find_spec(name) is not None for name in modules}
        print(json.dumps({"os": platform.system(), "python": platform.python_version(), "modules": ready,
                          "database_configured": bool(os.environ.get("NF_DATABASE_URL")),
                          "redis_configured": bool(os.environ.get("NF_REDIS_URL")),
                          "native_codex_found": resolve_host_binary() is not None}))
        return int(sys.version_info[:2] != (3, 12) or not all(ready.values()))
    # Keep all processes in the caller's foreground session. Dispatch existing
    # entry points so signals, authorization, arguments and exit codes are preserved.
    if args.command in SCRIPTS:
        script = ROOT / "scripts" / SCRIPTS[args.command]
        sys.argv = [str(script), *args.arguments]
        runpy.run_path(str(script), run_name="__main__")
    else:
        if args.command == "serve":
            module = "uvicorn"
            prefix = ["novel_factory.interfaces.http:from_environment", "--factory"]
        elif args.command == "migrate":
            module, prefix = "alembic", ["-c", str(ROOT / "alembic.ini")]
        else:
            module, prefix = MODULES[args.command], []
        sys.argv = [module, *prefix, *args.arguments]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
