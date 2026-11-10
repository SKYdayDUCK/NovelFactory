"""A complete external client command traverses every HTTP/application boundary."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from uuid import uuid4

import httpx
import pytest

from benchmarks.http_load import loopback_server
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.reducer import state_at
from novel_factory.fixtures import point,entity_value
from novel_factory.interfaces.http import create_app

pytestmark=[pytest.mark.postgres,pytest.mark.integration]

def test_http_task_to_guarded_accept_and_retry_uses_one_authoritative_commit(service_env,service_world):
    kernel=service_env["kernel"];world=service_world;wr=world["world_ref"];ids=world["ids"]
    # Initial input selection is provisioned by the trusted control plane.
    # Every task/compile/proposal/certificate/accept step below is a TCP call.
    manifest=kernel.put_artifact(world["principal"],wr["work_id"],{"subject_ids":[ids["sword"]],"predicates":["owner","active_holder"]},"INPUT_MANIFEST")
    spec={"schema_version":"2.0","task_id":str(uuid4()),"generation":0,"role_id":"control_operator","task_kind":"http_acceptance",
          "state":"READY","world_ref":wr,"story_time":point(10),"context_bundle_id":None,"policy_version":"nf-policy-1","dependency_fingerprint":None,
          "runtime_contract_version":fingerprint({"http":"p1"}),"capability_grant_id":world["principal"].grant_id,"input_artifacts":[manifest],
          "budget":{"currency":"USD","max_amount":"1","token_budget":50000},"wall_deadline":(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()}
    with loopback_server(create_app(kernel)) as base_url,httpx.Client(base_url=base_url,trust_env=False,timeout=10) as http:
        headers={"Authorization":"Bearer "+world["token"]}
        def post(route,payload):
            response=http.post("/api/v1/"+route,json=payload,headers=headers)
            assert response.status_code==200,response.json()
            return response.json()
        post("tasks",{"spec":spec,"idempotency_key":"tcp-create"})
        claim=post("tasks/claim",{"worker_id":"tcp-worker","task_id":spec["task_id"],"lease_seconds":60})
        context=post("contexts/compile",{"schema_version":"2.1","task_id":spec["task_id"],"generation":claim["generation"],"recipe_id":"ADJUDICATOR",
            "input_manifest_ref":manifest,"read_contract":{"revision":{"mode":"FIXED","world_ref":wr},"process_policy":"ALLOW_PARTIAL","index_policy":"AUTHORITATIVE","max_wait_ms":0},
            "knowledge_cut":{"plane":"WORLD"},"subject_time":{"kind":"POINT","at":point(10)},"token_budget":50000,"idempotency_key":"tcp-compile"})
        assert context["status"]=="READY"
        proposal=post("proposals",{"task_id":spec["task_id"],"generation":claim["generation"],"command":{"template_id":"TRANSFER_ITEM",
            "args":{"item_id":ids["sword"],"from_id":ids["smith"],"to_id":ids["protagonist"],"transfer_kind":"GIFT","at":point(10)}},"idempotency_key":"tcp-propose"})
        prepare={"schema_version":"2.1","task_id":spec["task_id"],"generation":claim["generation"],"context_bundle_id":proposal["context_bundle_id"],
            "concurrency_mode":"DEPENDENCY_GUARDED","base_world_ref":wr,"proposal_ref":proposal["proposal_ref"],"approval_refs":[proposal["approval_id"]],
            "dependency_handle":proposal["dependency_handle"],"idempotency_key":"tcp-prepare"}
        certificate=post("world/acceptance-certificates",prepare)
        command={k:v for k,v in prepare.items() if k!="dependency_handle"}
        command.update(acceptance_certificate_ref=certificate,dependency_fingerprint=proposal["dependency_fingerprint"],idempotency_key="tcp-accept")
        receipt=post("world/accept-guarded",command)
        validate_contract("GuardedCommitReceipt",receipt,"2.1")
        assert receipt==post("world/accept-guarded",command)
        altered=deepcopy(command);altered["generation"]+=1
        rejected=http.post("/api/v1/world/accept-guarded",json=altered,headers=headers)
        assert rejected.status_code==409 and rejected.json()["code"]=="IDEMPOTENCY_MISMATCH"
    with kernel.db.uow(write=False) as r:
        revision=kernel.revision(r,receipt["world_ref"])
        assert revision["branch_seq"]==1 and len(revision["selection"])==1
        assert state_at(revision["snapshot"],ids["sword"],"owner",point(10))["data"]==entity_value(ids["protagonist"])
        assert state_at(revision["snapshot"],ids["sword"],"active_holder",point(10))["data"]==entity_value(ids["protagonist"])
