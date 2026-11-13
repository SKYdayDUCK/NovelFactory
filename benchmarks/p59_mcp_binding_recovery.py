"""Inject a stdio server death between durable candidate storage and SDK reply."""
import argparse
import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client
from mcp.server.session import ServerSession
from sqlalchemy import text

from bound_stdio_throughput import provision
from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces import mcp_stdio


def fault_server(marker):
    send = ServerSession._send_response

    async def drop_candidate_response(session, request_id, response):
        value = getattr(getattr(response, "root", None), "structuredContent", None)
        if isinstance(value, dict) and value.get("status") == "CANDIDATE_STORED":
            # _send_response is invoked after the handler and its transactions finish.
            marker.write_text(str(os.getpid()))
            os._exit(75)
        await send(session, request_id, response)

    ServerSession._send_response = drop_candidate_response
    mcp_stdio.main()


def gated_discovery_server(marker):
    # Deterministically put four real incoming requests into the same native
    # group. This injects a barrier before the production dispatch function.
    dispatch = mcp_stdio.DiscoveryGroup._dispatch

    async def barrier(self, group):
        if marker.exists():
            async with asyncio.timeout(2):
                while len(group) < 4:
                    await asyncio.sleep(.001)
            marker.with_suffix(".captured").write_text(str(len(group)))
        await dispatch(self, group)

    mcp_stdio.DiscoveryGroup._dispatch = barrier
    mcp_stdio.main()


async def connect(stack, role, extra, *, marker=None, barrier=None):
    args = ([str(Path(__file__).resolve()), "die-before-reply", str(marker)] if marker
            else ["-m", "novel_factory.interfaces.mcp_stdio"])
    if barrier is not None:
        args = [str(Path(__file__).resolve()), "gated-group", str(barrier)]
    env = {k: os.environ[k] for k in ("PYTHONPATH", "LD_LIBRARY_PATH") if k in os.environ}
    params = StdioServerParameters(command=sys.executable, args=[
        *args, "--binding", str(role.path), *extra], env=env)
    reader, writer = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(reader, writer))
    await session.initialize()
    await session.list_tools()
    return session


async def cancel_group_member(kernel, role, session, *, n=2, barrier=None):
    # A test-only ACCESS EXCLUSIVE lock delays all SELECTs on grants. The
    # observer uses a second connection and waits until PG confirms a waiter.
    async def confirm_waiting():
        for _ in range(150):
            with kernel.db.uow(write=False) as repo:
                waiting = repo.conn.scalar(text("""
                    SELECT count(*) FROM pg_stat_activity WHERE application_name=:name
                    AND wait_event_type='Lock'
                """), {"name": role.application_name})
            if waiting:
                return
            await asyncio.sleep(.01)
        raise AssertionError("test never observed the blocked discovery")

    if barrier is not None:
        barrier.touch()
    with kernel.db.engine.begin() as conn:
        conn.exec_driver_sql("LOCK TABLE nf_grants IN ACCESS EXCLUSIVE MODE")
        first_id = session._request_id  # pinned SDK wire id is assigned before first await
        pending = [asyncio.create_task(session.list_tools()) for _ in range(n)]
        await confirm_waiting()
        notice = types.CancelledNotification(params=types.CancelledNotificationParams(
            requestId=first_id, reason="individual queued request withdrawn"))
        await session.send_notification(types.ClientNotification(notice))
        # The MCP SDK responds to cancellation. Wait while the SELECT is still
        # blocked so the test cannot accidentally cancel an already delivered result.
        try:
            async with asyncio.timeout(1):
                await pending[0]
        except TimeoutError:
            raise AssertionError("cancel response not delivered while peer reads were blocked") from None
        except Exception as exc:
            error = type(exc).__name__
        else:
            raise AssertionError("withdrawn request completed normally")
    responses = await asyncio.gather(*pending[1:])
    if barrier is not None:
        barrier.unlink()
        assert int(barrier.with_suffix(".captured").read_text()) == n
    expected = {"nf_task_get", "nf_context_get", "nf_query_batch", "nf_artifact_put"}
    assert all({t.name for t in response.tools} == expected for response in responses)
    replay = await session.call_tool("nf_task_get", {})
    assert replay.structuredContent == role.expected["nf_task_get"]
    return {"confirmed_pg_lock_wait": True, "cancel_exception": error,
            "forced_single_group_size": n if barrier is not None else None,
            "uncancelled_discoveries_correct": len(responses), "next_role_read_correct": True}


async def experiment(kernel, directory):
    role = provision(kernel, directory, 0)
    ref = role.expected["nf_task_get"]["world_ref"]
    extra = ["--compact-role-reads", "--batch-discovery"]
    candidate = {"content": "Return to the bridge.", "idempotency_key": "durable-reconnect"}
    marker = directory / "fault-confirmed"
    async with AsyncExitStack() as stack:
        session = await connect(stack, role, extra, marker=marker)
        try:
            async with asyncio.timeout(8):
                await session.call_tool("nf_artifact_put", candidate)
        except Exception as exc:
            disconnect = type(exc).__name__
        else:
            raise AssertionError("client received a reply before injected process death")
    assert marker.is_file()
    with kernel.db.uow(write=False) as repo:
        receipts = repo.many("receipts", {"work_id": ref["work_id"], "command_type": "MCP_ARTIFACT"})
        assert len(receipts) == 1
        expected = receipts[0]["payload"]
        grants = repo.many("grants", {"work_id": ref["work_id"]})
        owner = next(g for g in grants if "ADMIN" in g["capabilities"])
        actor = next(g for g in grants if g["principal_id"] == role.principal_id)
    async with AsyncExitStack() as stack:
        session = await connect(stack, role, extra)
        reply = await session.call_tool("nf_artifact_put", candidate)
        assert not reply.isError and reply.structuredContent == expected
        changed = await session.call_tool("nf_artifact_put", {**candidate, "content": "Different action."})
        assert changed.structuredContent == {"error": {"code": "IDEMPOTENCY_MISMATCH"}}
        isolation = await cancel_group_member(kernel, role, session)
    barrier = directory / "discovery-batch-barrier"
    async with AsyncExitStack() as stack:
        session = await connect(stack, role, extra, barrier=barrier)
        isolated_group = await cancel_group_member(kernel, role, session, n=4, barrier=barrier)
        kernel.revoke_grant(Principal(owner["grant_id"], owner["principal_id"]), actor["grant_id"])
        discoveries = await asyncio.gather(*(session.list_tools() for _ in range(4)))
        assert all(not response.tools for response in discoveries)
        denied = await session.call_tool("nf_context_get", {})
        assert denied.isError and denied.structuredContent == {"error": {"code": "VIEW_DENIED"}}
    with kernel.db.uow(write=False) as repo:
        artefacts = repo.many("artifacts", {"work_id": ref["work_id"], "kind": "MCP_TASK_CANDIDATE"})
        count = len(repo.many("receipts", {"work_id": ref["work_id"], "command_type": "MCP_ARTIFACT"}))
        assert count == len(artefacts) == 1 and kernel.head(repo, ref["work_id"]) == ref
        remaining = repo.conn.scalar(text(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name=:name AND state='idle in transaction'"),
            {"name": role.application_name})
        assert remaining == 0
    return {"registered_fault_exit": 75, "killed_before_sdk_send": True, "client_exception": disconnect,
            "persisted_before_reconnect": True, "identical_receipt_on_replay": True,
            "changed_content_error": "IDEMPOTENCY_MISMATCH", "candidate_artifacts": len(artefacts),
            "receipts": count, "empty_discoveries_after_revocation": len(discoveries),
            "compact_read_after_revocation": "VIEW_DENIED", "canonical_head_unchanged": True,
            "remaining_idle_in_transaction": remaining, "one_member_cancelled": isolation,
            "one_of_four_forced_group_cancelled": isolated_group}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    db = Database(pool_size=2)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            result = asyncio.run(experiment(Kernel(db, ObjectStore(directory/"objects")), directory))
    finally:
        db.close()
    result["source"] = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    result["driver_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "die-before-reply":
        marker = Path(sys.argv[2])
        del sys.argv[1:3]
        fault_server(marker)
    elif len(sys.argv) > 2 and sys.argv[1] == "gated-group":
        marker = Path(sys.argv[2])
        del sys.argv[1:3]
        gated_discovery_server(marker)
    else:
        main()
