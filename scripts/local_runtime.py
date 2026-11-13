"""Operate a private local NF/Workshop deployment without putting credentials in argv."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

from sqlalchemy.engine import URL

from agent_runtime.credentials import resolve
from novel_factory.infrastructure.private_files import read_private_bytes

ROOT = Path(__file__).resolve().parents[1]


def environment(path):
    config = json.loads(read_private_bytes(path, 100_000))
    env = os.environ.copy()
    for name, database in config["databases"].items():
        password = resolve(database["password_ref"])
        if not password:
            raise ValueError("DATABASE_CREDENTIAL_NOT_CONFIGURED")
        env[name] = URL.create("postgresql+psycopg", username=database["user"], password=password,
                               host="127.0.0.1", port=config["postgres_port"],
                               database=database["database"]).render_as_string(hide_password=False)
    password = resolve(config["redis_password_ref"])
    if not password:
        raise ValueError("REDIS_CREDENTIAL_NOT_CONFIGURED")
    env["NF_REDIS_URL"] = f"redis://:{quote(password, safe='')}@127.0.0.1:{config['redis_port']}/0"
    env["NF_TEST_REDIS_URL"] = env["NF_REDIS_URL"]
    env["AW_REDIS_URL"] = env["NF_REDIS_URL"]
    env.update(config.get("environment", {}))
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "workshop/src"), env.get("PYTHONPATH", "")])
    return config, env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / ".runtime/local/deployment.json")
    parser.add_argument("action", choices=("start", "status", "run"))
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    config, env = environment(args.config)
    if args.action == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            parser.error("run requires a command after --")
        return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode
    if args.action == "start":
        for service in ("postgresql", "redis-server"):
            subprocess.run(["wsl", "-d", config["wsl_distribution"], "--", "service", service, "start"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import redis
    from sqlalchemy import create_engine, text

    rows = {}
    for name in config["databases"]:
        engine = create_engine(env[name], connect_args={"connect_timeout": 5})
        try:
            with engine.connect() as connection:
                rows[name] = {"status": "READY", "server_version": connection.scalar(text("SHOW server_version"))}
        finally:
            engine.dispose()
    client = redis.Redis.from_url(env["NF_REDIS_URL"], socket_connect_timeout=5)
    try:
        rows["redis"] = {"status": "READY" if client.ping() else "UNAVAILABLE"}
    finally:
        client.close()
    print(json.dumps(rows))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError):
        sys.exit("Local deployment unavailable; check private configuration and service state.")
