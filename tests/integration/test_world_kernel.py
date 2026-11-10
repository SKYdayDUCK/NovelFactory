from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from novel_factory.fixtures import load_fixture,seed_sword_history,create_bound_task,prepare_command,run_command,point,entity_value
from novel_factory.application.queries import WorldQueryService
from novel_factory.domain.reducer import state_at,resource_at
from novel_factory.domain.errors import DomainError
from novel_factory.contracts.registry import validate_contract

pytestmark=[pytest.mark.postgres,pytest.mark.integration]

def current(kernel,wr):
    with kernel.db.uow(write=False) as r:return kernel.revision(r,wr)

def test_bootstrap_repeatable_and_world_scoped(kernel_env):
    k=kernel_env["kernel"];a=load_fixture(k);b=load_fixture(k,"white_bridge")
    assert a["world_ref"]["work_id"]!=b["world_ref"]["work_id"]
    assert a["ids"]["sword"]!=b["ids"]["sword"]
    with pytest.raises(DomainError):k.bootstrap_work("duplicate",a["genesis"],work_id=a["world_ref"]["work_id"])

def test_r1_sword_history_owner_holder_destroyed_and_belief(kernel_env):
    k=kernel_env["kernel"];f=seed_sword_history(k,load_fixture(k));s=current(k,f["world_ref"])["snapshot"];ids=f["ids"]
    for day,owner,holder in [(5,"smith","smith"),(15,"protagonist","protagonist"),(25,"protagonist","friend"),(35,"protagonist","rival")]:
        assert state_at(s,ids["sword"],"owner",point(day))["data"]==entity_value(ids[owner])
        assert state_at(s,ids["sword"],"active_holder",point(day))["data"]==entity_value(ids[holder])
    assert state_at(s,ids["sword"],"active_holder",point(41))["value_status"]=="NONE"
    assert state_at(s,ids["sword"],"active_use",point(41))["value_status"]=="NOT_APPLICABLE"
    assert state_at(s,ids["sword"],"last_confirmed_owner",point(41))["data"]==entity_value(ids["protagonist"])
    assert state_at(s,ids["sword"],"active_holder",point(35),actor_id=ids["protagonist"],known_at=point(35))["data"]==entity_value(ids["friend"])
    assert current(k,f["world_ref"])["frontiers"][ids["sword"]]==point(42)

def test_legacy_dates_preserved_as_separate_fixture(kernel_env):
    k=kernel_env["kernel"];f=seed_sword_history(k,load_fixture(k,"legacy_sword"));s=current(k,f["world_ref"])["snapshot"];ids=f["ids"]
    for day,owner,holder in [(5,"sect","master"),(13,"master","protagonist"),(21,"master","rival")]:
        assert state_at(s,ids["sword"],"owner",point(day))["data"]==entity_value(ids[owner])
        assert state_at(s,ids["sword"],"active_holder",point(day))["data"]==entity_value(ids[holder])
    assert state_at(s,ids["sword"],"lifecycle",point(32))["data"]["value"]=="DESTROYED"

def test_idempotent_accept_content_mismatch_and_proposal_uniqueness(kernel_env):
    k=kernel_env["kernel"];f=load_fixture(k);ids=f["ids"]
    p=prepare_command(k,f["principal"],f["world_ref"],"TRANSFER_ITEM",{"item_id":ids["sword"],"from_id":ids["smith"],"to_id":ids["protagonist"],"transfer_kind":"GIFT","at":point(10)})
    a=k.accept(f["principal"],p["request"]);assert k.accept(f["principal"],p["request"])==a
    changed=deepcopy(p["request"]);changed["generation"]+=1
    with pytest.raises(DomainError) as e:k.accept(f["principal"],changed)
    assert e.value.code=="IDEMPOTENCY_MISMATCH"
    changed=deepcopy(p["request"]);changed["idempotency_key"]+="new"
    with pytest.raises(DomainError) as e:k.accept(f["principal"],changed)
    assert e.value.code=="ALREADY_ACCEPTED"
    assert current(k,a["world_ref"])["branch_seq"]==1

def test_lost_response_then_cancel_retrieves_original_receipt(kernel_env):
    k=kernel_env["kernel"];f=load_fixture(k);ids=f["ids"]
    p=prepare_command(k,f["principal"],f["world_ref"],"DESTROY_ITEM",{"item_id":ids["sword"],"at":point(10)})
    def loss(stage):
        if stage=="after_commit":raise ConnectionError("injected response loss")
    with pytest.raises(ConnectionError):k.accept(f["principal"],p["request"],fault=loss)
    with pytest.raises(DomainError) as cancelled:k.cancel_task(f["principal"],p["task"]["task_id"],p["task"]["generation"],"cancel-after-commit")
    assert cancelled.value.code=="TASK_ALREADY_ACCEPTED"
    assert k.get_task(f["principal"],p["task"]["task_id"])["state"]=="ACCEPTED"
    result=k.accept(f["principal"],p["request"])
    assert result["branch_seq"]==1 and current(k,result["world_ref"])["snapshot"]["lifecycle"][ids["sword"]]=="DESTROYED"

@pytest.mark.fault
def test_failed_transaction_leaves_no_partial_events_or_head(kernel_env):
    k=kernel_env["kernel"];f=load_fixture(k);p=prepare_command(k,f["principal"],f["world_ref"],"DESTROY_ITEM",{"item_id":f["ids"]["sword"],"at":point(10)})
    def failure(stage):
        if stage=="after_events":raise RuntimeError("injected transaction failure")
    with pytest.raises(RuntimeError):k.accept(f["principal"],p["request"],fault=failure)
    with k.db.uow(write=False) as r:
        assert k.head(r,f["world_ref"]["work_id"])==f["world_ref"]
        assert not r.many("events",{"work_id":f["world_ref"]["work_id"]})
        assert not r.many("invalidations",{"work_id":f["world_ref"]["work_id"]})
    assert k.accept(f["principal"],p["request"])["branch_seq"]==1

def test_database_rejects_direct_immutable_event_edit(kernel_env):
    k=kernel_env["kernel"];f=load_fixture(k);a=run_command(k,f["principal"],f["world_ref"],"DESTROY_ITEM",{"item_id":f["ids"]["sword"],"at":point(10)})
    with pytest.raises(IntegrityError):
        with k.db.engine.begin() as c:c.execute(text("UPDATE nf_events SET payload='{}' WHERE event_version_id=:eid"),{"eid":a["receipt"]["accepted_event_refs"][0]["event_version_id"]})

def test_resource_transfer_conserves_white_bridge_original_consignment(kernel_env):
    k=kernel_env["kernel"];f=load_fixture(k,"white_bridge");ids=f["ids"]
    a=run_command(k,f["principal"],f["world_ref"],"RESOURCE_TRANSFER",{"from_account":ids["warehouse"],"to_account":ids["relief"],"resource_id":ids["grain"],"quantity":"30","unit":"bag","at":point(10)})
    s=current(k,a["world_ref"])["snapshot"]
    assert resource_at(s,ids["warehouse"],ids["grain"],"bag",point(11))["quantity"]=="70"
    assert resource_at(s,ids["relief"],ids["grain"],"bag",point(11))["quantity"]=="30"
    assert resource_at(s,ids["warehouse"],ids["grain"],"bag",point(5))["quantity"]=="100"

def test_fork_inherits_selection_once_and_isolates_future(kernel_env):
    k=kernel_env["kernel"];f=load_fixture(k,"white_bridge");ids=f["ids"]
    a=run_command(k,f["principal"],f["world_ref"],"RESOURCE_TRANSFER",{"from_account":ids["warehouse"],"to_account":ids["relief"],"resource_id":ids["grain"],"quantity":"30","unit":"bag","at":point(10)})
    branch=k.fork(f["principal"],a["world_ref"],"SIMULATION","sim")
    assert current(k,branch)["snapshot"]==current(k,a["world_ref"])["snapshot"]
    b=run_command(k,f["principal"],branch,"RESOURCE_TRANSFER",{"from_account":ids["warehouse"],"to_account":ids["relief"],"resource_id":ids["grain"],"quantity":"20","unit":"bag","at":point(100)})
    assert resource_at(current(k,a["world_ref"])["snapshot"],ids["warehouse"],ids["grain"],"bag",point(10))["quantity"]=="70"
    assert resource_at(current(k,b["world_ref"])["snapshot"],ids["warehouse"],ids["grain"],"bag",point(100))["quantity"]=="50"

def test_legacy_strict_entry_uses_same_writer(kernel_env):
    k=kernel_env["kernel"];f=load_fixture(k);p=prepare_command(k,f["principal"],f["world_ref"],"DESTROY_ITEM",{"item_id":f["ids"]["sword"],"at":point(10)})
    req={k:v for k,v in p["request"].items() if k not in {"acceptance_certificate_ref","concurrency_mode","base_world_ref"}}
    req.update(schema_version="2.0",expected_world_ref=f["world_ref"])
    a=k.accept(f["principal"],req);validate_contract("CommitReceipt",a)
    assert a==k.accept(f["principal"],req)

def test_old_generation_and_cancelled_task_cannot_commit(kernel_env):
    k=kernel_env["kernel"];f=load_fixture(k);p=prepare_command(k,f["principal"],f["world_ref"],"DESTROY_ITEM",{"item_id":f["ids"]["sword"],"at":point(10)})
    k.cancel_task(f["principal"],p["task"]["task_id"],p["task"]["generation"],"stop")
    with pytest.raises(DomainError):k.accept(f["principal"],p["request"])
    assert current(k,f["world_ref"])["snapshot"]["lifecycle"][f["ids"]["sword"]]=="ACTIVE"
