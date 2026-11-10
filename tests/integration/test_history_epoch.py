"""A historic replacement fences even independent inputs from the old epoch."""
from copy import deepcopy
from uuid import uuid4

import pytest

from novel_factory.application.authorization import ADMIN_CAPABILITIES
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture,prepare_command,run_command,point

pytestmark=[pytest.mark.postgres,pytest.mark.integration]

def test_repair_bumps_epoch_and_prevents_resigning_old_inputs(kernel_env):
    k=kernel_env["kernel"];w=load_fixture(k);p=w["principal"];ids=w["ids"]
    gift=run_command(k,p,w["world_ref"],"TRANSFER_ITEM",{"item_id":ids["sword"],"from_id":ids["smith"],"to_id":ids["protagonist"],"transfer_kind":"GIFT","at":point(10)})
    branch=k.fork(p,gift["world_ref"],"REPAIR","epoch-repair")
    old_input=prepare_command(k,p,branch,"TRANSFER_ITEM",{"item_id":ids["sword2"],"from_id":ids["smith2"],"to_id":ids["friend"],"transfer_kind":"GIFT","at":point(20)},mode="DEPENDENCY_GUARDED")
    with k.db.uow(write=False) as r:
        original=r.one("events",{"event_version_id":gift["receipt"]["accepted_event_refs"][0]["event_version_id"]})["payload"]
        assert k.revision(r,branch)["provenance"]["history_epoch"]==0
    replacement=deepcopy(original);replacement["event_version_id"]=str(uuid4());replacement["representation_id"]=str(uuid4())
    replacement["effects"]=[e for e in replacement["effects"] if e.get("predicate")!="legal_owner"]
    reviewer=k.issue_grant(p,branch["work_id"],role_id="control_operator",branch_ids=["*"],capabilities=ADMIN_CAPABILITIES,
                          planes=["WORLD"],max_story_time=point(1000))["principal"]
    repaired=run_command(k,p,branch,"REPAIR_EVENT",{"occurrence_id":original["occurrence_id"],"replacement_event":replacement,"reason":"Correct a gift to a loan"},reviewer=reviewer)
    with pytest.raises(DomainError) as stale:k.accept(p,old_input["request"])
    assert stale.value.code=="STALE_CERTIFICATE"
    fresh_request=deepcopy(old_input["prepare_request"]);fresh_request["idempotency_key"]+="-new-signature"
    with pytest.raises(DomainError) as rebound:k.prepare(p,fresh_request)
    assert rebound.value.code=="HISTORY_CHANGED"
    with k.db.uow(write=False) as r:
        assert r.one("branches",{"branch_id":branch["branch_id"]})["history_epoch"]==1
        assert k.revision(r,repaired["world_ref"])["provenance"]["history_epoch"]==1
        assert k.revision(r,branch)["provenance"]["history_epoch"]==0
        assert k.head(r,branch["work_id"],branch["branch_id"])==repaired["world_ref"]
    forked=k.fork(p,repaired["world_ref"],"CANDIDATE","new-epoch-fork")
    with k.db.uow(write=False) as r:
        assert r.one("branches",{"branch_id":forked["branch_id"]})["history_epoch"]==1
        assert k.revision(r,forked)["provenance"]["history_epoch"]==1
