"""Manage recovered workers through the current user's Windows Task Scheduler.

The private configuration contains only version, source, state, deployment and books.
Task Scheduler owns the process independently of an interactive development tool.
It preserves the current user's credential vault and Codex login; it does not run
while that user is logged out. STOP always waits for the current paid call.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_runtime.credentials import resolve
from novel_factory.infrastructure.native_files import (
    file_lock,
    lock_is_held,
    open_append,
    publish_file,
)
from novel_factory.infrastructure.private_files import (
    create_private_file,
    private_temporary_file,
    read_private_bytes,
)

ROOT = Path(__file__).resolve().parents[1]
TASK_PREFIX = "NovelFactory-P6-"


def save(path, value):
    fd, pending = private_temporary_file(path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        publish_file(pending, path, replace=True)
    finally:
        Path(pending).unlink(missing_ok=True)


def configuration(path):
    value = json.loads(read_private_bytes(path, 100_000))
    if (set(value) != {"version", "source", "state", "deployment", "books"}
            or value["version"] != 1 or not value["books"]
            or len(set(value["books"])) != len(value["books"])
            or any(book not in ("A", "B", "C") for book in value["books"])):
        raise ValueError("INVALID_HOST_CONFIGURATION")
    for name in ("source", "state", "deployment"):
        if not Path(value[name]).is_absolute():
            raise ValueError("HOST_PATH_MUST_BE_ABSOLUTE")
        value[name] = Path(value[name]).resolve()
    manifest = json.loads(read_private_bytes(value["state"] / "batch/batch.json", 100_000))
    sha = subprocess.check_output(["git", "-C", str(value["source"]), "rev-parse", "HEAD"], text=True).strip()
    if sha != manifest["source_sha"]:
        raise ValueError("HOST_SOURCE_BINDING_DIFFERS")
    value["source_sha"] = sha
    return value


def task_name(book):
    if book not in ("A", "B", "C"):
        raise ValueError("INVALID_BOOK")
    return TASK_PREFIX + book


def task_xml(config_path, book, user_sid):
    # InteractiveToken needs no stored password and uses the authorizing user's
    # vault/login. No API keys or database URLs are registered with the OS.
    namespace = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", namespace)
    node = lambda name: "{" + namespace + "}" + name
    root = ET.Element(node("Task"), version="1.2")
    triggers = ET.SubElement(root, node("Triggers"))
    trigger = ET.SubElement(triggers, node("LogonTrigger"))
    ET.SubElement(trigger, node("Enabled")).text = "true"
    ET.SubElement(trigger, node("UserId")).text = user_sid
    principals = ET.SubElement(root, node("Principals"))
    principal = ET.SubElement(principals, node("Principal"), id="Author")
    ET.SubElement(principal, node("UserId")).text = user_sid
    ET.SubElement(principal, node("LogonType")).text = "InteractiveToken"
    ET.SubElement(principal, node("RunLevel")).text = "LeastPrivilege"
    settings = ET.SubElement(root, node("Settings"))
    for key, value in {"MultipleInstancesPolicy": "IgnoreNew", "DisallowStartIfOnBatteries": "false",
                       "StopIfGoingOnBatteries": "false", "AllowHardTerminate": "false",
                       "StartWhenAvailable": "true", "AllowStartOnDemand": "true", "Enabled": "true",
                       "Hidden": "true", "ExecutionTimeLimit": "PT0S"}.items():
        ET.SubElement(settings, node(key)).text = value
    restart = ET.SubElement(settings, node("RestartOnFailure"))
    ET.SubElement(restart, node("Interval")).text = "PT1M"
    ET.SubElement(restart, node("Count")).text = "3"
    actions = ET.SubElement(root, node("Actions"), Context="Author")
    action = ET.SubElement(actions, node("Exec"))
    ET.SubElement(action, node("Command")).text = sys.executable
    ET.SubElement(action, node("Arguments")).text = subprocess.list2cmdline(
        [str(Path(__file__).resolve()), "--config", str(config_path.resolve()), "run", "--book", book])
    ET.SubElement(action, node("WorkingDirectory")).text = str(ROOT)
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


def install(config_path, books):
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        sid = win32security.ConvertSidToStringSid(win32security.GetTokenInformation(token, win32security.TokenUser)[0])
    finally:
        token.Close()
    for book in books:
        fd, path = private_temporary_file(config_path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(task_xml(config_path, book, sid))
            subprocess.run(["schtasks.exe", "/Create", "/TN", task_name(book), "/XML", str(path), "/F"],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            Path(path).unlink(missing_ok=True)


def database_health(config):
    """Readiness only; never starts a worker, advances a task or contacts a model."""
    import redis
    from local_runtime import environment
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import SQLAlchemyError

    _, env = environment(config["deployment"])
    manifest = json.loads(read_private_bytes(config["state"] / "batch/batch.json", 100_000))
    password = resolve("NF_P6_DATABASE_PASSWORD")
    if not password:
        return {"database": "CREDENTIAL_MISSING", "redis": "UNCHECKED"}
    engine = create_engine(make_url(manifest["database"]).set(password=password), connect_args={"connect_timeout": 5})
    result = {}
    try:
        with engine.connect() as connection:
            result["database"] = "READY" if connection.scalar(text("SELECT 1")) == 1 else "UNAVAILABLE"
    except (SQLAlchemyError, OSError):
        # Driver diagnostics can contain credential-bearing connection strings.
        result["database"] = "UNAVAILABLE"
    finally:
        engine.dispose()
    client = redis.Redis.from_url(env["NF_REDIS_URL"], socket_connect_timeout=5, socket_timeout=5)
    try:
        result["redis"] = "READY" if client.ping() else "UNAVAILABLE"
    except (redis.RedisError, OSError):
        result["redis"] = "UNAVAILABLE"
    finally:
        client.close()
    return result


def status(config):
    books = {}
    for book in config["books"]:
        directory = config["state"] / "batch" / book
        books[book] = {name: lock_is_held(directory / ("." + name + ".lock"))
                       for name in ("host", "manager", "worker")}
        active = config["state"] / (book + "-active-worker.json")
        if active.exists():
            record = json.loads(read_private_bytes(active, 100_000))
            books[book]["source_matches"] = record.get("source_sha") == config["source_sha"]
            books[book]["stop_requested"] = Path(record["stop_file"]).exists()
    return {"books": books, "health": database_health(config)}


def request_stop(config, books):
    for book in books:
        path = config["state"] / (book + "-active-worker.json")
        record = json.loads(read_private_bytes(path, 100_000))
        if record["source_sha"] != config["source_sha"]:
            raise ValueError("STOP_SOURCE_BINDING_DIFFERS")
        stop = Path(record["stop_file"]).resolve()
        if stop.parent != config["state"]:
            raise ValueError("INVALID_STOP_PATH")
        save(stop, {"action": "STOP_AFTER_CURRENT_CALL", "book": book, "source_sha": config["source_sha"]})


@contextmanager
def awake():
    """Keep an active production session awake; leave display power policy alone."""
    if os.name == "nt":
        import ctypes
        if not ctypes.windll.kernel32.SetThreadExecutionState(0x80000001):
            raise OSError("HOST_SLEEP_INHIBITION_FAILED")
    try:
        yield
    finally:
        if os.name == "nt":
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)


def run(config, book):
    directory = config["state"] / "batch" / book
    # Probe before blocking file_lock, so a second manual invocation exits.
    if any(lock_is_held(directory / ("." + name + ".lock")) for name in ("host", "manager", "worker")):
        raise ValueError("BOOK_HOST_ALREADY_RUNNING")
    with file_lock(directory / ".host.lock"), awake():
        from local_runtime import environment

        _, env = environment(config["deployment"])
        # Both books may be started by the same logon event. Serialize service
        # readiness, not model dispatch, so a stopped private cluster starts once.
        with file_lock(config["deployment"].parent / ".service-start.lock"):
            subprocess.run([sys.executable, str(ROOT / "scripts/local_runtime.py"), "--config",
                            str(config["deployment"]), "start"], env=env, cwd=ROOT, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if any(value != "READY" for value in database_health(config).values()):
            raise ValueError("HOST_INFRASTRUCTURE_NOT_READY")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        paths = {name: config["state"] / f"{book}-{stamp}{suffix}"
                 for name, suffix in (("stdout", ".log"), ("stderr", ".stderr.log"), ("stop_file", ".stop.json"))}
        for name in ("stdout", "stderr"):
            os.close(create_private_file(paths[name]))
        with open_append(paths["stdout"]) as out, open_append(paths["stderr"]) as err:
            command = [sys.executable, str(ROOT / "scripts/p6_local_worker.py"), "--source", str(config["source"]),
                       "--state", str(config["state"]), "--book", book, "--run", "--watch", "--stop-file", str(paths["stop_file"])]
            child = subprocess.Popen(command, cwd=ROOT, env=env, stdout=out, stderr=err,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            save(config["state"] / (book + "-active-worker.json"), {
                "pid": child.pid, "host_pid": os.getpid(), "source_sha": config["source_sha"],
                "started_at_utc": datetime.now(UTC).isoformat(), **{k: str(v) for k, v in paths.items()}})
            # Do not retry a paid request here. The worker's durable task/Attempt
            # state remains authoritative after any process restart.
            return child.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("action", choices=("plan", "install", "start", "status", "stop", "run"))
    parser.add_argument("--book", choices=("A", "B", "C"))
    args = parser.parse_args()
    config = configuration(args.config)
    books = [args.book] if args.book else config["books"]
    if any(book not in config["books"] for book in books):
        raise ValueError("BOOK_NOT_CONFIGURED")
    if args.action == "plan":
        print(json.dumps({"tasks": [task_name(book) for book in books], "source_sha": config["source_sha"],
                          "owner": "CURRENT_LOGGED_IN_USER", "model_calls": 0,
                          "restart": "AT_LOGON_AND_3_FAILURE_RETRIES", "stop": "AFTER_CURRENT_CALL"}))
    elif args.action == "install":
        install(args.config, books)
    elif args.action == "start":
        for book in books:
            subprocess.run(["schtasks.exe", "/Run", "/TN", task_name(book)], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif args.action == "stop":
        request_stop(config, books)
    elif args.action == "status":
        print(json.dumps(status(config)))
    else:
        if len(books) != 1:
            parser.error("run requires --book")
        return run(config, books[0])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        sys.exit("Persistent worker host unavailable; check private configuration, locks, services and task ownership.")
