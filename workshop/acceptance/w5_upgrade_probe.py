"""Run in a fresh interpreter using either the archived service or its replacement."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import psycopg

from agent_workshop.admin import enroll
from agent_workshop.auth import authenticate, connect

ORG, INSTALL = "film-archive", "local-studio"


def seed(dsn, revision, binding):
    from agent_workshop.organization import bootstrap

    with connect(dsn) as conn:
        conn.execute("INSERT INTO aw_installations VALUES (%s,'archived-nf',true)", (INSTALL,))
        scope = {"installation_id": INSTALL} if revision in ("w004c", "w005b") else {}
        bootstrap(conn, ORG, **scope)
        token = enroll(conn, INSTALL, ORG, "manager", 1, 1)
        with os.fdopen(os.open(binding, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
            stream.write(token)
        if revision != "w004c":
            from agent_workshop.manager import request_resume

            manager = authenticate(conn, token)
            wake = request_resume(conn, manager, "queued-before-maintenance")
            assert wake["state"] == "PENDING"
            assert request_resume(conn, manager, "queued-before-maintenance") == wake


def consume(dsn):
    from agent_workshop.manager_worker import run_once

    calls = []

    async def project(name, args):
        assert (name, args) == ("nf_resume_project", {})
        calls.append(name)
        return dict(workflow_state="IDLE", ready_count=0, awakened=0, recovered=0, pending_review_count=0)

    async def unexpected(*args):
        raise AssertionError(f"unexpected delegation call: {args[0]}")

    state = asyncio.run(run_once(dsn, ORG, INSTALL, project, unexpected))
    return {"state": state, "peer_calls": calls}


def queue_current(dsn, revision, binding):
    from agent_workshop.manager import request_resume
    from agent_workshop.organization import claim_organization

    with connect(dsn) as conn:
        manager = authenticate(conn, binding.read_text())
        if revision in ("w005", "w005a"):
            assert not manager.org_member
            claim_organization(conn, ORG, INSTALL)
            manager = authenticate(conn, enroll(conn, INSTALL, ORG, "manager", 1, 1))
        assert manager.org_member
        wake = request_resume(conn, manager, "after-maintenance")
        assert request_resume(conn, manager, "after-maintenance") == wake
        return {key: wake[key] for key in ("request_id", "state")}


def schema(dsn):
    # Reuse the W4 qualifier's relation, column, index, view and constraint comparison.
    from test_w4_routing_upgrade import catalog

    signatures = []
    for url in (dsn, os.environ["AW_FRESH_DATABASE_URL"]):
        with psycopg.connect(url) as conn:
            objects = catalog(conn)
            triggers = conn.execute("""SELECT c.relname,t.tgname,pg_get_triggerdef(t.oid)
                FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
                WHERE c.relnamespace=current_schema()::regnamespace AND NOT t.tgisinternal
                ORDER BY 1,2""").fetchall()
            functions = conn.execute("""SELECT p.proname,pg_get_functiondef(p.oid) FROM pg_proc p
                WHERE p.pronamespace=current_schema()::regnamespace ORDER BY 1,2""").fetchall()
            signatures.append((objects, triggers, functions))
    assert signatures[0] == signatures[1]
    assert len(triggers) >= 7 and len(functions) >= 8
    return {"objects": len(objects), "triggers": len(triggers), "functions": len(functions),
            "sha256": hashlib.sha256(json.dumps(signatures[0]).encode()).hexdigest()}


if __name__ == "__main__":
    action, revision, name = sys.argv[1:]
    dsn = os.environ["AW_DATABASE_URL"]
    binding = Path(name)
    if action == "seed":
        seed(dsn, revision, binding)
    elif action == "schema":
        print(json.dumps(schema(dsn)))
    elif action == "consume":
        print(json.dumps(consume(dsn)))
    elif action == "queue":
        print(json.dumps(queue_current(dsn, revision, binding)))
    else:
        raise SystemExit("Unknown upgrade probe")
