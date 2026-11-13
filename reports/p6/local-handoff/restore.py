"""Move the stopped P6 batch into a new, local PostgreSQL 17 cluster (Linux/WSL2)."""
import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HERE = Path(__file__).resolve().parent
ARCHIVE = json.loads((HERE / "checkpoint.json").read_bytes())
MAGIC = b"NF-P6-ARCHIVE-v1\n"
BOOKS = ("A", "B", "C")


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def load(path):
    return json.loads(path.read_bytes())


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def hashed_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unpack(keyfile, root):
    require(keyfile.is_file() and not keyfile.is_symlink() and keyfile.stat().st_uid == os.getuid()
            and keyfile.stat().st_mode & 0o077 == 0, "Recovery file must be owner-only (chmod 600).")
    keyfile_data = load(keyfile)
    encrypted = bytearray()
    for part in ARCHIVE["parts"]:
        data = (HERE / part["name"]).read_bytes()
        require(len(data) == part["bytes"] and hashlib.sha256(data).hexdigest() == part["sha256"],
                "Archive part differs: " + part["name"])
        encrypted.extend(data)
    require(hashlib.sha256(encrypted).hexdigest() == ARCHIVE["archive_sha256"] and encrypted.startswith(MAGIC),
            "Incomplete or incorrect checkpoint.")
    nonce = bytes(encrypted[len(MAGIC):len(MAGIC) + 12])
    key = hashlib.sha256(MAGIC + keyfile_data["passphrase"].encode("ascii")).digest()
    archive = AESGCM(key).decrypt(nonce, bytes(encrypted[len(MAGIC) + 12:]), MAGIC)
    with tempfile.TemporaryFile(dir=root) as source, tempfile.TemporaryFile(dir=root) as plain:
        source.write(archive)
        source.seek(0)
        subprocess.run(["zstd", "-qdc"], stdin=source, stdout=plain, check=True)
        plain.seek(0)
        with tarfile.open(fileobj=plain) as tar:
            for member in tar:
                require(member.isfile() or member.isdir(), "Unexpected archive entry.")
                tar.extract(member, root, filter="data")
    for name, digest in load(root / "files.json").items():
        require(hashed_file(root / name) == digest, "File checksum differs: " + name)
    return keyfile_data["capabilities"]


def db_env(root):
    meta = load(root / "local.json")
    env = dict(os.environ, PGHOST="127.0.0.1", PGPORT=str(meta["port"]), PGUSER="nf_local",
               PGDATABASE="p6_three_novels", PGPASSFILE=str(root / "pgpass"),
               NF_DATABASE_URL=meta["url"], NF_OBJECT_ROOT=str(root / "objects"))
    env.pop("PGPASSWORD", None)
    return env


def check_source(source):
    def git(*args):
        return subprocess.check_output(["git", "-C", source, *args], text=True).strip()
    require(git("rev-parse", "HEAD") == ARCHIVE["source_sha"] and not git("status", "--porcelain"),
            "Select a clean worktree at the frozen production SHA.")
    paths = [source / "src", source / "workshop/src", source / "scripts", source]
    sys.path[:0] = list(map(str, paths))
    return os.pathsep.join(map(str, paths))


def control(root, action):
    command = ["pg_ctl", "-D", str(root / "pgdata"), "-w"]
    if action == "start":
        command += ["-l", str(root / "postgres-local.log")]
    if action == "stop":
        command += ["-m", "fast"]
    subprocess.run(command + [action], check=True)


def provision(root, port):
    require(subprocess.check_output(["pg_config", "--version"], text=True).split()[1].split(".")[0] == "17"
            and " 17." in subprocess.check_output(["initdb", "--version"], text=True), "PostgreSQL 17 required.")
    password = secrets.token_urlsafe(42)
    pw = root / "init-password"
    pw.write_text(password + "\n")
    try:
        subprocess.run(["initdb", "-D", str(root / "pgdata"), "-U", "nf_local", "--locale=C",
                        "--encoding=UTF8", "--auth=scram-sha-256", "--pwfile", str(pw)],
                       stdout=subprocess.DEVNULL, check=True)
    finally:
        pw.unlink(missing_ok=True)
    with (root / "pgdata/postgresql.conf").open("a") as conf:
        conf.write(f"\nlisten_addresses = '127.0.0.1'\nunix_socket_directories = ''\ntimezone = 'Asia/Tokyo'\nport = {port}\n")
    (root / "pgpass").write_text(f"127.0.0.1:{port}:*:nf_local:{password}\n")
    url = f"postgresql+psycopg://nf_local@127.0.0.1:{port}/p6_three_novels"
    write(root / "local.json", {"port": port, "url": url})


def bind(root, tokens):
    paths = {f"batch/{b}/{name}.json" for b in BOOKS
             for name in ("control", "preparation-control", "preparation", "supervisor")}
    require(set(tokens) == paths, "Incomplete runtime bindings.")
    manifest = root / "batch/batch.json"
    config = load(manifest)
    require(config["source_sha"] == ARCHIVE["source_sha"], "Wrong source manifest.")
    url = load(root / "local.json")["url"]
    objects = str(root / "objects")
    for name in sorted(paths):
        path = root / name
        data = load(path)
        require("token" not in data, "Unexpected credential in the archive.")
        if "binding" in data:
            require(data["database_url"] == config["database"] and data["object_root"] == config["objects"],
                    "Control refers to an unrelated batch.")
            data.update(database_url=url, object_root=objects)
        write(path, {**data, "token": tokens[name]})
    write(manifest, {**config, "database": url, "objects": objects})


def verify(root):
    from sqlalchemy import text
    from novel_factory.infrastructure.database import Database
    from novel_factory.infrastructure.recovery import database_snapshot
    from novel_factory.infrastructure.objects import ObjectStore
    from novel_factory.application.kernel import Kernel
    from p6_observe_batch import snapshot
    env = db_env(root)
    os.environ["PGPASSFILE"] = env["PGPASSFILE"]
    os.environ.pop("PGPASSWORD", None)
    db = Database(env["NF_DATABASE_URL"])
    try:
        store = ObjectStore(root / "objects")
        with db.engine.connect() as conn:
            conn.execute(text("SET LOCAL idle_in_transaction_session_timeout = 0"))
            conn.execute(text("SET LOCAL statement_timeout = 0"))
            conn.execute(text("SET LOCAL TIME ZONE 'UTC'"))
            original = load(root / "database-tables.json")
            require(database_snapshot(conn) == original, "Database rows differ from the stopped original.")
            objects = conn.execute(text("SELECT hash, size FROM nf_objects")).all()
        for digest, size in objects:
            store.verify(digest, expected_size=size)
        with tempfile.TemporaryDirectory(dir=root) as scratch:
            sample, _, _ = snapshot(Kernel(db, store), root / "batch", load(root / "batch/batch.json"),
                                    load(root / "batch/enrolled.json"), Path(scratch), "1")
        original_books = load(root / "last-observation/sample-1.json")["books"]
        fields = ("scopes", "completed_transport_count", "known_input_tokens", "known_output_tokens",
                  "extent", "attempt_ledger_states", "task_states")
        for book, state in sample["books"].items():
            require(all(state[k] == original_books[book][k] for k in fields),
                    "Work, transport, or workflow ledger differs: " + book)
        return {"source_sha": ARCHIVE["source_sha"], "tables": len(original), "verified_objects": len(objects),
                "http_200": {b: original_books[b]["completed_transport_count"] for b in BOOKS}}
    finally:
        db.close()


def worker(root, source, pythonpath, book, keyfile, *, check_only):
    from sqlalchemy import text
    from novel_factory.infrastructure.database import Database
    from novel_factory.infrastructure.objects import ObjectStore
    from novel_factory.infrastructure.wire_receipts import read_receipt
    state = load(root / "batch" / book / "worker-state.json")
    env = db_env(root)
    os.environ["PGPASSFILE"] = env["PGPASSFILE"]
    os.environ.pop("PGPASSWORD", None)
    db = Database(env["NF_DATABASE_URL"])
    try:
        work = load(root / "batch" / book / "supervisor.json")["work_id"]
        intake = load(root / "batch/enrolled.json")[book]["work_id"]
        store = ObjectStore(root / "objects")
        with db.engine.connect() as conn:
            if state.get("reason") == "STALE_GENERATION":
                paused = conn.execute(text("SELECT task_id,generation FROM nf_tasks WHERE work_id=:work "
                                           "AND state='PAUSED' ORDER BY task_id"), {"work": work}).all()
                require(not paused, "STALE_GENERATION: inspect the current paused task(s): " + str(paused))
            rows = conn.execute(text("SELECT a.attempt_id,a.request_hash FROM nf_call_attempts a JOIN nf_tasks t "
                                     "USING(task_id) WHERE t.work_id IN (:work,:intake) AND a.state IN ('SENT','UNKNOWN')"),
                                {"work": work, "intake": intake})
            for attempt, digest in rows:
                wire = read_receipt(store, attempt, digest)
                require(wire and wire[0]["complete"] and wire[0]["http_status"] == 200,
                        "Reconcile original provider attempt before continuing: " + str(attempt))
    finally:
        db.close()
    if check_only:
        print(f"{book}: receipt and generation checks passed; no provider call.")
        return
    require(not keyfile.is_symlink() and keyfile.is_file() and keyfile.stat().st_uid == os.getuid()
            and keyfile.stat().st_mode & 0o077 == 0, "Provider credential file must be owner-only (chmod 600).")
    name, sep, value = keyfile.read_text().strip().partition("=")
    require(name == "NF_DEEPSEEK_API_KEY" and sep and value and "\n" not in value, "Invalid provider file.")
    env.update(PYTHONPATH=pythonpath, NF_RUNTIME_LIVE_APPROVED="1", NF_DEEPSEEK_API_KEY=value)
    os.chdir(source)
    os.execve(sys.executable, [sys.executable, "scripts/p6_batch.py", "worker", "--private-dir",
                               str(root / "batch"), "--book", book], env)


def main():
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["restore", "db-start", "db-stop", "check-worker", "worker"])
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--source", type=Path)
    p.add_argument("--recovery-file", type=Path)
    p.add_argument("--provider-file", type=Path)
    p.add_argument("--port", type=int, default=55432)
    p.add_argument("--book", choices=BOOKS)
    args = p.parse_args()
    root = args.root.resolve()
    if args.action in {"db-start", "db-stop"}:
        require((root / "verified.json").is_file(), "Select a verified restore.")
        return control(root, args.action.split("-")[1])
    require(args.source, "--source required")
    source = args.source.resolve()
    pythonpath = check_source(source)
    require(not root.is_relative_to(source), "Choose a private directory outside the source tree.")
    if args.action in {"check-worker", "worker"}:
        require((root / "verified.json").is_file() and args.book, "Verified restore and --book required.")
        require(args.action == "check-worker" or args.provider_file, "--provider-file required for paid production.")
        return worker(root, source, pythonpath, args.book, args.provider_file, check_only=args.action == "check-worker")
    require(args.recovery_file and not root.exists() and 1024 <= args.port <= 65535,
            "Restore requires --recovery-file, a new private directory and a valid port.")
    root.mkdir(mode=0o700, parents=True)
    tokens = unpack(args.recovery_file, root)
    provision(root, args.port)
    bind(root, tokens)
    control(root, "start")
    try:
        env = db_env(root)
        subprocess.run(["createdb", "p6_three_novels"], env=env, check=True)
        subprocess.run(["pg_restore", "--single-transaction", "--exit-on-error", "--no-owner", "--no-acl",
                        "-d", "p6_three_novels", str(root / "database.dump")], env=env, check=True)
        result = verify(root)
        write(root / "verified.json", result)
        print(json.dumps(result), flush=True)
    finally:
        control(root, "stop")


if __name__ == "__main__":
    main()
