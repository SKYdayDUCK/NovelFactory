"""Run the preregistered A/B/B/A relay comparison in disposable, per-arm SQL schemas."""
import argparse
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

from alembic import command
from psycopg import sql
from redis import Redis
from sqlalchemy.engine import make_url

from agent_workshop.admin import enroll
from agent_workshop.auth import authenticate, connect
from agent_workshop.messaging import send
from agent_workshop.migrate import config
from agent_workshop.notifications import notify_batch, receive_batch
from agent_workshop.organization import bootstrap


def run(base, redis, grouped, size, repeat):
    schema = 'hera_bench_' + uuid4().hex
    dsn = make_url(base).update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
    org = uuid4().hex
    stream = "hera:notifications:" + org
    with connect(base) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        os.environ["AW_DATABASE_URL"] = dsn
        command.upgrade(config(), "head")
        with connect(dsn) as conn:
            conn.execute("INSERT INTO aw_installations VALUES(%s,%s,true)", (org, "nf-test"))
            bootstrap(conn, org, installation_id=org)
            director = authenticate(conn, enroll(conn, org, org, "volume", 1, 1))
            for i in range(size):
                send(conn, director, str(i), "actor-1", "ASSIGN", "organization", 1, 3600)
        start = time.perf_counter_ns()
        published = notify_batch(dsn, redis, stream, limit=size, grouped=grouped)
        published_at = time.perf_counter_ns()
        accepted = receive_batch(dsn, redis, stream, org, "single", limit=size, grouped=grouped)
        completed = time.perf_counter_ns()
        hints = redis.xrange(stream)
        with connect(dsn) as conn:
            expected = {str(row['event_id']) for row in conn.execute("SELECT event_id FROM aw_outbox")}
            inbox = {str(row['event_id']) for row in conn.execute("SELECT event_id FROM aw_inbox WHERE subscriber=%s", (org,))}
            pg = conn.execute("SHOW server_version").fetchone()["server_version"]
        ids = [fields["event_id"] for _, fields in hints]
        assert published == accepted == len(ids) == size and set(ids) == expected == inbox
        pending = redis.xpending(stream, org)["pending"]
        assert pending == 0
        return dict(repeat=repeat, grouped=grouped, size=size, publish_ms=(published_at-start)/1e6,
                    accept_ms=(completed-published_at)/1e6, elapsed_ms=(completed-start)/1e6,
                    events=[dict(event_id=i, stream_id=s, received=i in inbox) for i,(s,_) in zip(ids,hints)],
                    pending=pending, pg_version=pg, redis_version=redis.info("server")["redis_version"])
    finally:
        os.environ["AW_DATABASE_URL"] = base
        redis.delete(stream)
        with connect(base) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    base = os.environ["AW_DATABASE_URL"]
    redis = Redis.from_url(os.environ["AW_REDIS_TEST_URL"], decode_responses=True)
    meta = dict(python=sys.version.split()[0], os=platform.platform(),
                source=subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip())
    with args.output.open("w", encoding="utf8") as file:
        for size in (1, 32, 128):
            for repeat, grouped in enumerate((False, True, True, False), 1):
                row = run(base, redis, grouped, size, repeat)
                file.write(json.dumps({**meta, **row},separators=(",",":"))+"\n")
                file.flush()
                print(size, grouped, round(row["elapsed_ms"],3), flush=True)


if __name__ == "__main__":
    main()
