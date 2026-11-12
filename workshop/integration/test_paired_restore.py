"""Physical PG17 recovery across NF and Workshop. All databases belong to this test."""

import asyncio
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import time
from uuid import uuid4

import psycopg
import pytest
from sqlalchemy.engine import make_url

from agent_workshop.admin import enroll
from agent_workshop.auth import Denied, connect
from agent_workshop.manager_worker import run_once
from agent_workshop.organization import bootstrap
from agent_workshop.provision import allow_slot
from agent_workshop.reconcile import GET_FIELDS, reconcile, remote
from agent_workshop.recovery import freeze, frozen, permit
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.application.delegation import (
    install,
    installation_ledger,
    provision_slot,
    reconcile_restored_installation,
)
from novel_factory.domain.errors import DomainError
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.recovery import fence_recovered_tasks
from conftest import database_pair
from test_dual_stdio import bound_process


def port_at(dsn, port):
    return make_url(dsn).set(port=port).render_as_string(hide_password=False)


def aw_state(dsn, op):
    with connect(dsn) as c:
        return c.execute(
            """SELECT d.state, d.last_error, h.status AS hold,
            r.receipt_ref::text,r.task_id::text,r.accounting_state,h.settled_amount,
            (d.payload->>'token_limit')::int AS task_token_limit,
            (SELECT jsonb_agg(jsonb_build_array(o.attempt_id,o.state,o.actual_tokens)
                              ORDER BY o.observed_at, o.state)
             FROM aw_cost_observations o WHERE o.org_id=d.org_id AND o.operation_id=d.operation_id)
               AS token_observations,
            (SELECT count(*) FROM aw_cost_observations o WHERE o.operation_id=d.operation_id) AS observations
            FROM aw_delegations d JOIN aw_budget_holds h USING(org_id,operation_id)
            LEFT JOIN aw_remote_receipts r USING(org_id,operation_id) WHERE d.operation_id=%s""",
            (op,),
        ).fetchone()


def tick(dsn, inst, org, op, transport):
    with connect(dsn) as c:
        c.execute(
            "UPDATE aw_delegations SET next_check=clock_timestamp() WHERE operation_id=%s", (op,)
        )
    return asyncio.run(reconcile(dsn, inst, org, op, transport))


def test_physical_pitr_both_directions(tmp_path, monkeypatch):
    nf_source = Path(os.environ["AW_NF_SOURCE"]).resolve()
    monkeypatch.syspath_prepend(str(nf_source / "scripts"))
    monkeypatch.syspath_prepend(str(nf_source / "tests/integration"))
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "qualification"))
    import pitr_drill as pg
    from offline_witness import nf_snapshot, workshop_disconnected, workshop_snapshot
    from test_p5_supervisor import backend

    pg_bin = Path(os.environ.get("PG_BIN", "/usr/lib/postgresql/17/bin"))
    for name in list(os.environ):
        if name.startswith("PG") and name != "PG_BIN":
            monkeypatch.delenv(name)
    started = time.monotonic()
    version = pg._run([pg_bin / "postgres", "--version"])["stdout"]
    assert "PostgreSQL) 17." in version
    archive, original, image, recovered = [
        tmp_path / p for p in ("archive", "source", "base", "recovered")
    ]
    archive.mkdir()
    pg._run(
        [
            pg_bin / "initdb",
            "-D",
            original,
            "-U",
            "nf_dev",
            "--encoding=UTF8",
            "--locale=C",
            "--auth-local=reject",
            "--auth-host=trust",
            "--data-checksums",
        ]
    )
    (original / "pg_hba.conf").write_text(
        "host all all 127.0.0.1/32 scram-sha-256\nhost replication nf_dev 127.0.0.1/32 trust\n"
    )
    # Only this disposable cluster trusts its isolated local bootstrap user.
    hba = original / "pg_hba.conf"
    hba.write_text("host all nf_dev 127.0.0.1/32 trust\n" + hba.read_text())
    port = pg._port()
    admin_dsn = f"postgresql://nf_dev@127.0.0.1:{port}/postgres"
    archived_segment = f"{shlex.quote(str(archive))}/%f"
    archive_command = f"test -f {archived_segment} || (cp %p {archived_segment}.part && mv {archived_segment}.part {archived_segment})"
    settings = dict(
        listen_addresses="127.0.0.1",
        unix_socket_directories="",
        port=port,
        max_connections=35,
        shared_buffers="48MB",
        fsync="on",
        synchronous_commit="on",
        full_page_writes="on",
        wal_level="replica",
        archive_mode="on",
        max_wal_senders=4,
        archive_command=archive_command,
    )
    source_conf = tmp_path / "source.conf"
    pg._config(source_conf, settings)
    with ExitStack() as stack:
        source = pg._start(pg_bin, original, source_conf, port, tmp_path / "source.log")
        stack.callback(pg._stop, source)
        urls = stack.enter_context(database_pair(admin_dsn))
        db = Database(urls["nf"])
        stack.callback(db.close)
        kernel = Kernel(db, ObjectStore(tmp_path / "objects"))
        world = load_fixture(kernel)
        wr, owner = world["world_ref"], world["principal"]
        org, instance = str(uuid4()), str(uuid4())
        g = install(
            kernel,
            owner,
            nf_instance_id=instance,
            org_id=org,
            work_id=wr["work_id"],
            branch_id=wr["branch_id"],
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        inst = g["installation_id"]
        adapter, requests, _ = stack.enter_context(backend())
        gateway = AgentGateway(kernel, {"fixture": adapter})
        supervisor = RuntimeSupervisor(kernel, gateway)
        token_limit = 100000
        spec = RuntimeTaskService(kernel, gateway).define(
            owner,
            wr,
            role_id="evidence_maintainer",
            task_kind="summary",
            story_time=point(1),
            subject_ids=[world["ids"]["sword"]],
            predicates=["owner"],
            assignment="Summarize observed state.",
            adapter_id="fixture",
            max_amount="0.10",
            token_budget=token_limit,
        )
        budget_id = kernel.tasks.create_budget(wr["work_id"], "1")
        budget_scope = (owner, wr["work_id"], wr["branch_id"], budget_id)
        assert SharedBudgetService(kernel).set_limit(*budget_scope, token_limit)["available_tokens"] == token_limit

        def budget_at(peer):
            status = SharedBudgetService(peer).status(*budget_scope)
            assert str(status["budget_id"]) == budget_id and status["total_tokens"] == token_limit
            return {k: status[k] for k in ("reserved_tokens", "spent_tokens", "available_tokens")}

        provision_slot(
            supervisor,
            owner,
            inst,
            scope_ref="scene.summary",
            task_template_id="summary.v1",
            frozen=spec,
            budget_id=budget_id,
            reserve_amount="0.10",
            token_reservation=token_limit,
            accept_summary=True,
        )
        policy = dict(
            nf_instance_id=instance,
            work_id=wr["work_id"],
            branch_id=wr["branch_id"],
            generation=0,
            grant_epoch=g["grant_epoch"],
            scope_ref="scene.summary",
            task_template_id="summary.v1",
            actor_binding_ref=None,
            role_id="evidence_maintainer",
            max_amount="0.10",
            currency="USD",
            token_limit=token_limit,
        )
        with connect(urls["aw"]) as c:
            c.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (inst, instance))
            bootstrap(c, org, "Long-form", installation_id=inst)
            c.execute("INSERT INTO aw_budget_accounts VALUES(%s,1,'USD')", (org,))
            allow_slot(c, org, inst, "scene.summary", "memory", policy)
            chief = enroll(c, inst, org, "chief", 1, 1)
            author = enroll(c, inst, org, "author", 1, 1)

        def transport(where, module, config, src):
            folder = tmp_path / where
            folder.mkdir()
            return remote(bound_process(folder, module, config, src))

        aw_src = Path(__file__).resolve().parents[1] / "src"
        aw = transport(
            "chief", "agent_workshop.server", dict(token=chief, database_url=urls["aw"]), aw_src
        )
        aw_author = transport(
            "author", "agent_workshop.server", dict(token=author, database_url=urls["aw"]), aw_src
        )

        def nf_process(tag, dsn, objects):
            return transport(
                tag,
                "novel_factory.interfaces.delegation_stdio",
                dict(
                    token=g["token"],
                    installation_id=inst,
                    database_url=dsn,
                    object_root=str(objects),
                ),
                nf_source / "src",
            )

        nf = nf_process("live", urls["nf"], tmp_path / "objects")
        admin = stack.enter_context(psycopg.connect(admin_dsn, autocommit=True))
        pg_env = {
            **os.environ,
            "PGHOST": "127.0.0.1",
            "PGPORT": str(port),
            "PGUSER": "nf_dev",
            "PGDATABASE": "postgres",
        }
        pg._run(
            [
                pg_bin / "pg_basebackup",
                "-D",
                image,
                "-Fp",
                "-Xstream",
                "--checkpoint=fast",
                "--manifest-checksums=SHA256",
            ],
            env=pg_env,
        )
        verified = pg._run([pg_bin / "pg_verifybackup", image])
        manifest_sha = hashlib.sha256((image / "backup_manifest").read_bytes()).hexdigest()
        op = asyncio.run(
            aw(
                "aw_delegate",
                dict(
                    operation_id="persist-before-nf",
                    slot_id="scene.summary",
                    max_amount="0.10",
                    token_limit=token_limit,
                ),
            )
        )["operation_id"]
        wake_key = {"operation_id": "pending-before-pitr"}
        wake = asyncio.run(aw_author("aw_resume_project", wake_key))
        assert wake["state"] == "PENDING"
        target = "aw_intent_before_nf"
        lsn = admin.execute("SELECT pg_create_restore_point(%s)", (target,)).fetchone()[0]
        shutil.copytree(tmp_path / "objects", tmp_path / "objects-at-target")
        assert tick(urls["aw"], inst, org, op, nf) == "BOUND"
        bound_target = "nf_bound_before_inference"
        bound_lsn = admin.execute("SELECT pg_create_restore_point(%s)", (bound_target,)).fetchone()[
            0
        ]
        initial = workshop_snapshot(urls["aw"], op)
        task = initial["task_id"]
        with connect(urls["aw"]) as c:
            payload = c.execute(
                "SELECT payload FROM aw_delegations WHERE operation_id=%s", (op,)
            ).fetchone()["payload"]
        get = {k: payload[k] for k in GET_FIELDS}
        scans = []
        with workshop_disconnected(admin, urls["aw"]):
            # The bound stdio service fails closed while its database is unavailable.
            with pytest.raises(Denied, match="^VIEW_DENIED$"):
                asyncio.run(aw_author("aw_resume_project", wake_key))
            for _ in range(2):
                before = nf_snapshot(supervisor, task)
                result = supervisor.run_once(owner, wr["work_id"], wr["branch_id"])
                scans.append(
                    {
                        "before": before,
                        "after": nf_snapshot(supervisor, task),
                        "status": result["status"],
                    }
                )
            accepted = asyncio.run(nf("nf_delegation_get", get))
            assert scans[0]["before"]["state"] != "ACCEPTED" and not scans[0]["before"]["attempts"]
            assert scans[-1]["after"] == {"state": "ACCEPTED", "attempts": accepted["attempt_ids"]}
            assert accepted["task_state"] == "ACCEPTED"
            assert len(accepted["attempt_ids"]) == len(requests) == 1

        assert workshop_snapshot(urls["aw"], op) == initial
        assert asyncio.run(aw_author("aw_resume_project", wake_key)) == wake
        assert tick(urls["aw"], inst, org, op, nf) == "BOUND"
        reconnected = workshop_snapshot(urls["aw"], op)
        assert initial["task_state"] != "ACCEPTED" and reconnected["task_state"] == "ACCEPTED"
        assert initial["receipt_ref"] == reconnected["receipt_ref"] == accepted["receipt_ref"]
        assert reconnected["task_id"] == task and reconnected["attempts_observed"] == 1
        later = aw_state(urls["aw"], op)
        assert later["hold"] == "HELD" and later["accounting_state"] == "UNKNOWN"
        attempt_id = accepted["attempt_ids"][0]
        unknown_usage = budget_at(kernel)
        assert unknown_usage == {"reserved_tokens": token_limit, "spent_tokens": 0, "available_tokens": 0}
        assert later["task_token_limit"] == token_limit
        assert later["token_observations"] == [[attempt_id, "UNKNOWN", None]]
        unknown_target = "unknown_usage_before_invoice"
        unknown_lsn = admin.execute("SELECT pg_create_restore_point(%s)", (unknown_target,)).fetchone()[0]
        offline = {
            "status": "PASS",
            "operation_id": op,
            "recorded_utc": datetime.now(UTC).isoformat(),
            "scope": "Workshop database refused connections across two real NF supervisor scans.",
            "aw_database_allowed_connections": False,
            "aw_live_client_backends": 0,
            "aw_author_mcp_during_outage": "VIEW_DENIED",
            "pending_wake_preserved": wake["request_id"],
            "aw_unchanged_while_offline": initial,
            "nf_supervisor_scans": scans,
            "nf_stdio_during_outage": {
                "receipt_ref": accepted["receipt_ref"],
                "attempt_ids": accepted["attempt_ids"],
            },
            "aw_after_reconciliation": reconnected,
            "production_provider_calls": 0,
        }
        wal = admin.execute("SELECT pg_walfile_name(pg_current_wal_lsn())").fetchone()[0]
        admin.execute("SELECT pg_switch_wal()")
        until = time.monotonic() + 60
        while not (archive / wal).exists():
            assert time.monotonic() < until, "WAL archive deadline exceeded"
            time.sleep(0.1)

        def restore_at(target):
            recovered_port = pg._port()
            recovered = tmp_path / ("data-" + target)
            shutil.copytree(image, recovered)
            (recovered / "recovery.signal").touch()
            restore_conf = tmp_path / (target + ".conf")
            pg._config(
                restore_conf,
                {
                    **settings,
                    "port": recovered_port,
                    "archive_mode": "off",
                    "restore_command": f"cp {shlex.quote(str(archive))}/%f %p",
                    "recovery_target_name": target,
                    "recovery_target_action": "promote",
                },
            )
            restored = pg._start(
                pg_bin, recovered, restore_conf, recovered_port, tmp_path / (target + ".log")
            )
            stack.callback(pg._stop, restored)
            with psycopg.connect(port_at(admin_dsn, recovered_port), autocommit=True) as ctl:
                until = time.monotonic() + 60
                while ctl.execute("SELECT pg_is_in_recovery()").fetchone()[0]:
                    assert time.monotonic() < until
                    time.sleep(0.1)
                replay = ctl.execute("SELECT pg_last_wal_replay_lsn()").fetchone()[0]
            assert (
                f'recovery stopping at restore point "{target}"'
                in (tmp_path / (target + ".log")).read_text()
            )
            return recovered_port, replay

        recovered_port, replay = restore_at(target)
        restored_nf, restored_aw = (port_at(urls[k], recovered_port) for k in ("nf", "aw"))
        db2 = Database(restored_nf)
        stack.callback(db2.close)
        kernel2 = Kernel(db2, ObjectStore(tmp_path / "objects-at-target"))
        fence_recovered_tasks(db2.engine, kernel2.objects)
        with db2.uow(write=False) as repo:
            assert not repo.many("delegation_receipts", {"installation_id": inst})
            assert not repo.many("tasks", {"work_id": wr["work_id"]})
        unused_usage = budget_at(kernel2)
        assert unused_usage == {"reserved_tokens": 0, "spent_tokens": 0, "available_tokens": token_limit}
        early = aw_state(restored_aw, op)
        assert (
            early["state"] == "REQUESTED"
            and early["hold"] == "HELD"
            and early["receipt_ref"] is None
        )
        restored_peer = nf_process("restored", restored_nf, tmp_path / "objects-at-target")
        calls = []

        def traced(peer):
            async def invoke(tool, args):
                calls.append((tool, args.get("operation_id")))
                return await peer(tool, args)

            return invoke

        assert tick(urls["aw"], inst, org, op, traced(restored_peer)) == "WAITING_OPERATOR"
        ahead = aw_state(urls["aw"], op)
        assert (
            ahead["last_error"] == "REMOTE_HISTORY_MISMATCH"
            and ahead["hold"] == "HELD"
            and ahead["receipt_ref"] == accepted["receipt_ref"]
        )
        with connect(urls["aw"]) as c:
            assert frozen(c, inst)
            with pytest.raises(Denied, match="REMOTE_HISTORY_MISMATCH"):
                permit(c, inst, {})
        with pytest.raises(Denied, match="DELEGATION_RECOVERY_FENCED"):
            asyncio.run(restored_peer("nf_delegation_submit", payload))
        with connect(restored_aw) as c:
            freeze(c, inst)

        # A normal restarted manager cannot send either an NF resume or a delegation.
        async def forbidden(tool, args):
            raise AssertionError(f"unexpected outbound call {tool}")

        assert asyncio.run(run_once(restored_aw, org, inst, forbidden, forbidden)) == "IDLE"
        for _ in range(2):
            assert tick(restored_aw, inst, org, op, traced(nf)) == "BOUND"
        before_invoice = aw_state(restored_aw, op)
        assert (
            before_invoice["receipt_ref"] == accepted["receipt_ref"]
            and before_invoice["hold"] == "HELD"
        )
        assert (
            before_invoice["observations"] == 1 and before_invoice["accounting_state"] == "UNKNOWN"
        )
        assert before_invoice["token_observations"] == later["token_observations"]
        assert budget_at(kernel) == unknown_usage
        invoice = dict(state="SUCCEEDED", actual_amount="0.02", actual_tokens=18,
                       provider_receipt="synthetic-drill-invoice")
        kernel.tasks.settle_call(attempt_id, **invoice)
        after_invoice_usage = budget_at(kernel)
        assert after_invoice_usage == {"reserved_tokens": 0, "spent_tokens": 18, "available_tokens": token_limit - 18}
        kernel.tasks.settle_call(attempt_id, **invoice)
        assert budget_at(kernel) == after_invoice_usage
        assert tick(restored_aw, inst, org, op, traced(nf)) == "SETTLED"
        final = aw_state(restored_aw, op)
        assert (
            str(final["settled_amount"]) == "0.020000"
            and final["observations"] == 2
            and final["hold"] == "SETTLED"
        )
        assert final["token_observations"] == [[attempt_id, "UNKNOWN", None], [attempt_id, "SUCCEEDED", 18]]
        with connect(restored_aw) as c:
            permit(c, inst, installation_ledger(kernel, owner, inst))
            assert not frozen(c, inst)
        # A new recovery epoch cannot reuse a prior proof. A settled local row
        # must be checked even if the peer presents the exact same receipt ID.
        with connect(restored_aw) as c:
            freeze(c, inst)
            with pytest.raises(Denied, match="REMOTE_HISTORY_MISMATCH"):
                permit(c, inst, installation_ledger(kernel, owner, inst))
        assert tick(restored_aw, inst, org, op, traced(nf)) == "SETTLED"
        with connect(restored_aw) as c:
            permit(c, inst, installation_ledger(kernel, owner, inst))
        middle_port, middle_replay = restore_at(bound_target)
        middle_dsn = port_at(urls["nf"], middle_port)
        middle_db = Database(middle_dsn)
        stack.callback(middle_db.close)
        middle_kernel = Kernel(middle_db, ObjectStore(tmp_path / "objects-at-target"))
        fence_recovered_tasks(middle_db.engine, middle_kernel.objects)
        middle_view = installation_ledger(middle_kernel, owner, inst)
        assert (
            middle_view[op]["receipt_ref"] == final["receipt_ref"]
            and middle_view[op]["attempts"] == []
        )
        with pytest.raises(DomainError) as blocked:
            reconcile_restored_installation(
                middle_kernel, owner, inst, installation_ledger(kernel, owner, inst)
            )
        assert blocked.value.code == "DELEGATION_RECOVERY_FENCED"
        middle_peer = nf_process("middle", middle_dsn, tmp_path / "objects-at-target")
        with connect(restored_aw) as c:
            freeze(c, inst)
        assert tick(restored_aw, inst, org, op, traced(middle_peer)) == "WAITING_OPERATOR"
        after_mismatch = aw_state(restored_aw, op)
        assert (
            after_mismatch["hold"] == "SETTLED"
            and after_mismatch["settled_amount"] == final["settled_amount"]
        )
        with connect(restored_aw) as c:
            assert frozen(c, inst)
            with pytest.raises(Denied, match="REMOTE_HISTORY_MISMATCH"):
                permit(c, inst, middle_view)
        assert len(requests) == 1 and calls == [("nf_delegation_get", op)] * 6
        with db.uow(write=False) as repo:
            assert len(repo.many("tasks", {"work_id": wr["work_id"]})) == 1
            assert kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr
        # Restore both service databases to the recorded post-UNKNOWN point after the live
        # bill has settled. The old WAL image must independently retain its unresolved hold.
        unknown_port, replayed_unknown = restore_at(unknown_target)
        unknown_db = Database(port_at(urls["nf"], unknown_port))
        stack.callback(unknown_db.close)
        recovered_unknown_usage = budget_at(Kernel(unknown_db, kernel.objects))
        recovered_unknown_aw = aw_state(port_at(urls["aw"], unknown_port), op)
        assert replayed_unknown == unknown_lsn
        assert recovered_unknown_usage == unknown_usage
        assert recovered_unknown_aw == later
        assert budget_at(kernel) == after_invoice_usage
        if witness := os.environ.get("AW_OFFLINE_WITNESS"):
            Path(witness).write_text(json.dumps(offline, indent=2) + "\n")
        out = os.environ.get("AW_PAIRED_RESTORE_EVIDENCE")
        if out:
            Path(out).write_text(
                json.dumps(
                    dict(
                        status="PASS",
                        pg=version,
                        basebackup="physical pg_basebackup -Fp -Xstream",
                        verification=verified,
                        manifest_sha256=manifest_sha,
                        target=target,
                        target_lsn=lsn,
                        replayed_lsn=replay,
                        second_wal_target=bound_target,
                        second_target_lsn=bound_lsn,
                        second_replay_lsn=middle_replay,
                        shared_token_budget=dict(
                            budget_id=budget_id, total_tokens=token_limit, original_attempt_id=attempt_id,
                            before_first_call=unused_usage, live_unknown=unknown_usage,
                            unknown_wal_target=unknown_target, unknown_target_lsn=unknown_lsn,
                            unknown_replayed_lsn=replayed_unknown, recovered_unknown=recovered_unknown_usage,
                            recovered_workshop_observations=recovered_unknown_aw["token_observations"],
                            after_repeated_invoice=after_invoice_usage,
                            final_workshop_observations=final["token_observations"],
                        ),
                        same_receipt_missing_attempt_peer=middle_view[op],
                        after_settled_history_mismatch=after_mismatch,
                        rolled_back_nf_task_count=0,
                        rolled_back_aw_intent=early,
                        live_nf=accepted,
                        nf_restored_aw_live=ahead,
                        aw_restored_nf_live_before_invoice=before_invoice,
                        after_explicit_synthetic_invoice=final,
                        resulting_nf_tasks=1,
                        loopback_synthetic_http_requests=len(requests),
                        supplier_model_calls=0,
                        recovery_mcp_calls=calls,
                        frozen_manager="IDLE",
                        workshop_offline=offline,
                        elapsed_seconds=round(time.monotonic() - started, 2),
                    ),
                    default=str,
                    indent=2,
                )
                + "\n"
            )
