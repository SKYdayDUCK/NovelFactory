"""Run the Linux audit suite against one worktree, two disposable PostgreSQL 17 clusters and Redis."""
import argparse
import os
from pathlib import Path
import socket
import subprocess
import sys
from time import monotonic, sleep

from redis import Redis
from redis.exceptions import ConnectionError
from sqlalchemy.engine import make_url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pg-bin", type=Path, required=True)
    redis_source = parser.add_mutually_exclusive_group(required=True)
    redis_source.add_argument("--redis-server", type=Path, help="Absolute or relative path to installed redis-server")
    redis_source.add_argument("--redis-root", type=Path, help="Extracted Debian package root with usr/bin/redis-server")
    parser.add_argument("--stage", choices=["start", "nf", "aw"], default="start", help=argparse.SUPPRESS)
    parser.add_argument("tests", nargs="*", help="Optional pytest paths or node IDs, defaults to tests")
    args = parser.parse_args()
    root = Path.cwd()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if Path(sys.prefix).resolve() != (root / ".venv").resolve():
        parser.error("Run from the selected worktree using its own .venv/bin/python")

    env = dict(os.environ)
    if args.redis_root:
        package = args.redis_root.resolve()
        redis_server = package / "usr/bin/redis-server"
        libs = str(package / "usr/lib/x86_64-linux-gnu")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, (libs, env.get("LD_LIBRARY_PATH"))))
    else:
        # Debian Redis uses argv[0] to choose the server entry point of its multicall binary.
        redis_server = args.redis_server.absolute()
    pg_bin = args.pg_bin.resolve()
    if args.stage in {"start", "nf"}:
        stage = "nf" if args.stage == "start" else "aw"
        if args.stage == "nf":
            env["NF_FIXED_TEST_URL"] = env["NF_TEST_DATABASE_URL"]
        return subprocess.call([
            sys.executable, "scripts/with_postgres.py", "--pg-bin", str(pg_bin),
            "--log-dir", str(output / ("pg-" + stage)), "--", sys.executable, str(Path(__file__).resolve()),
            "--output", str(output), "--pg-bin", str(pg_bin), "--redis-server", str(redis_server),
            "--stage", stage, *args.tests], env=env)

    aw = make_url(env["NF_TEST_DATABASE_URL"]).set(drivername="postgresql", database="postgres")
    nf = make_url(env["NF_FIXED_TEST_URL"]).set(drivername="postgresql", database="postgres")
    if (nf.host, nf.port) == (aw.host, aw.port):
        raise RuntimeError("NF and Workshop need separate PostgreSQL clusters")
    for key in list(env):
        if key.startswith("PG") and key != "PG_BIN":
            del env[key]
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    env.update(
        NF_DATABASE_URL=env["NF_FIXED_TEST_URL"], NF_TEST_DATABASE_URL=env["NF_FIXED_TEST_URL"],
        DATABASE_URL=env["NF_FIXED_TEST_URL"], AW_NF_SOURCE=str(root), AW_W0_PYTHON=sys.executable,
        AW_DUAL_TEST_NF_ADMIN_URL=nf.render_as_string(hide_password=False),
        AW_DUAL_TEST_ADMIN_URL=aw.render_as_string(hide_password=False),
        NF_TEST_REDIS_URL=f"redis://127.0.0.1:{port}/0", AW_REDIS_TEST_URL=f"redis://127.0.0.1:{port}/1",
        NF_S6_COMPARISON_OUTPUT=str(output / "s6.json"),
        PYTHONPATH=os.pathsep.join(map(str, (root / "workshop/src", root / "src", root))),
        PATH=os.pathsep.join((str(root / ".venv/bin"), str(redis_server.parent), env["PATH"])),
    )
    with (output / "redis.log").open("w") as log:
        process = subprocess.Popen([redis_server, "--bind", "127.0.0.1", "--port", str(port),
                                    "--save", "", "--appendonly", "no"], env=env, stdout=log, stderr=log)
        try:
            deadline = monotonic() + 10
            with Redis(host="127.0.0.1", port=port, socket_connect_timeout=1, socket_timeout=1) as client:
                while True:
                    try:
                        if client.ping():
                            break
                    except ConnectionError:
                        if process.poll() is not None or monotonic() >= deadline:
                            raise
                    sleep(.05)
            return subprocess.call([sys.executable, "-m", "pytest", "-q", "-o", "junit_family=xunit1",
                                    *(args.tests or ["tests"]), "--junitxml=" + str(output / "cases.xml")], env=env)
        finally:
            process.terminate()
            process.wait()


if __name__ == "__main__":
    sys.exit(main())
