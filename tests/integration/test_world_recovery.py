"""Recover actual accepted history, permissions, leases and object evidence."""
import json
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text,select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url

from novel_factory.application.kernel import Kernel
from novel_factory.domain.errors import DomainError
from novel_factory.domain.reducer import state_at
from novel_factory.fixtures import load_fixture,seed_sword_history,create_bound_task,point,entity_value
from novel_factory.infrastructure.database import Database,tables
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.recovery import BackupService
from novel_factory.orchestration.tasks import OutboxService

pytestmark=[pytest.mark.postgres,pytest.mark.integration,pytest.mark.recovery]

def test_real_world_history_and_authorization_survive_isolated_restore(kernel_env,tmp_path):
    source=kernel_env["kernel"]
    original=load_fixture(source)
    world=seed_sword_history(source,original)
    principal=world["principal"];wr=world["world_ref"];ids=world["ids"]
    branch=source.fork(principal,wr,"CANDIDATE","recovery-candidate")
    task=create_bound_task(source,principal,wr,point(41))
    budget=source.tasks.create_budget(wr["work_id"],"5")
    call=source.tasks.reserve_call(task["task_id"],task["generation"],task["worker_id"],budget_id=budget,amount="0.5",
                                  call_key="pending-model-call",request_hash="1"*64,token_amount=100)
    source.tasks.mark_call_sent(call["attempt_id"],worker_id=task["worker_id"])
    dispatcher=OutboxService(source.db.engine,tables)
    delivery=dispatcher.claim("old-dispatcher",topic="world.accepted")
    assert delivery
    watermark=tables["index_watermarks"]
    def materialize(conn,payload):
        cut=payload["world_ref"]
        seq=conn.scalar(select(tables["revisions"].c.branch_seq).where(tables["revisions"].c.world_revision_id==cut["world_revision_id"]))
        conn.execute(insert(watermark).values(branch_id=cut["branch_id"],index_name="restore-audit",world_revision_id=cut["world_revision_id"],branch_seq=seq,generation=1)
                     .on_conflict_do_update(index_elements=[watermark.c.branch_id,watermark.c.index_name],set_={"generation":watermark.c.generation+1}))
    assert dispatcher.consume("restoration-audit",delivery["outbox_id"],materialize)
    with source.db.uow(write=False) as r:
        revisions={x["world_revision_id"]:{k:x[k] for k in ("state_hash","manifest_hash","selection","frontiers","interpretation_ref")}
                   for x in r.many("revisions",{"work_id":wr["work_id"]})}
        expected_scope=r.scope_map(wr["work_id"],wr["branch_id"])
    backup=BackupService(source.db.engine,source.objects)
    manifest=backup.create(tmp_path/"backup")
    target_name="nf_world_restore_"+uuid4().hex[:16]
    with kernel_env["admin"].connect() as c:c.execute(text(f'CREATE DATABASE "{target_name}"'))
    target_url=make_url(kernel_env["url"]).set(database=target_name).render_as_string(hide_password=False)
    restored_db=None
    try:
        verification=backup.restore(tmp_path/"backup",target_url,tmp_path/"restored-objects",tables=tables)
        restored_db=Database(target_url,pool_size=4)
        restored=Kernel(restored_db,ObjectStore(tmp_path/"restored-objects"))
        assert restored.authenticate(world["token"])==principal
        with restored.db.uow(write=False) as r:
            assert restored.head(r,wr["work_id"])==wr
            assert restored.head(r,wr["work_id"],branch["branch_id"])==branch
            assert r.scope_map(wr["work_id"],wr["branch_id"])==expected_scope
            for rid,expected in revisions.items():
                row=r.one("revisions",{"world_revision_id":rid})
                assert {k:row[k] for k in expected}==expected
            history=restored.revision(r,wr)["snapshot"]
            assert state_at(history,ids["sword"],"owner",point(25))["data"]==entity_value(ids["protagonist"])
            assert state_at(history,ids["sword"],"active_holder",point(35))["data"]==entity_value(ids["rival"])
            assert state_at(history,ids["sword"],"active_holder",point(35),actor_id=ids["protagonist"],known_at=point(35))["data"]==entity_value(ids["friend"])
            assert state_at(history,ids["sword"],"active_holder",point(41))["value_status"]=="NONE"
            assert state_at(restored.revision(r,original["world_ref"])["snapshot"],ids["sword"],"owner",point(5))["data"]==entity_value(ids["smith"])
            assert r.one("call_attempts",{"attempt_id":call["attempt_id"]})["state"]=="UNKNOWN"
            assert r.one("budgets",{"budget_id":budget})["reserved_amount"]==Decimal("0.5")
        recovered_task=restored.get_task(principal,task["task_id"])
        assert recovered_task["state"]=="PAUSED" and recovered_task["generation"]==task["generation"]+1
        with restored.db.uow() as r,pytest.raises(DomainError) as exc:
            restored.tasks.guard_task(r.conn,task["task_id"],task["generation"])
        assert exc.value.code=="STALE_GENERATION"
        restored_dispatcher=OutboxService(restored.db.engine,tables)
        assert not restored_dispatcher.consume("restoration-audit",delivery["outbox_id"],materialize)
        with restored.db.uow(write=False) as r:assert r.one("index_watermarks",{"branch_id":wr["branch_id"],"index_name":"restore-audit"})["generation"]==1
        with pytest.raises(DomainError):restored_dispatcher.ack(delivery["outbox_id"],delivery["generation"],"old-dispatcher")
        for digest,size in manifest["objects"].items():assert restored.objects.verify(digest).size==size
        evidence=Path(os.environ.get("NF_EVIDENCE_DIR",Path(__file__).resolve().parents[3]/"evidence"/"recovery_tests"))
        evidence.mkdir(parents=True,exist_ok=True)
        (evidence/"world_history_restore.json").write_text(json.dumps({"source_postgres":manifest["postgres_version"],"world_ref":wr,
            "history_revision_count":len(revisions),"state_and_manifest_hashes":revisions,"backup":manifest,"restoration":verification,
            "assertions":["old and latest history equal","owner versus actual holder versus mistaken belief","destroyed remains absent",
                          "all object bytes verified","authorization restored","pending tasks fenced","unknown call reservation retained","inbox prevents duplicate delivery"]},ensure_ascii=False,indent=2)+"\n")
    finally:
        if restored_db:restored_db.close()
        with kernel_env["admin"].connect() as c:c.execute(text(f'DROP DATABASE "{target_name}" WITH (FORCE)'))
