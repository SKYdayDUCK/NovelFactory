"""Explicitly invoked, isolated synthetic acceptance worlds.

Nothing here runs at application startup. Source cases remain unchanged in
validation/. R1 and legacy sword dates/identities are deliberately separate.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL
from copy import deepcopy

from novel_factory.domain.canonical import fingerprint

def point(day,phase=0):return {"tick":day,"phase":phase}
def entity_value(eid):return {"kind":"ENTITY","entity_id":eid}

def fixture_definition(name):
    if name not in {"r1_sword","legacy_sword","white_bridge"}:raise ValueError("Unknown isolated fixture")
    namespace=uuid5(NAMESPACE_URL,"novel-factory-p1-fixture:"+name)
    ids={key:str(uuid5(namespace,key)) for key in ["sword","sword2","smith","smith2","protagonist","friend","rival","master","sect","north","south",
          "warehouse","relief","reserve","recipient","grain","guard1","guard2","traveler","blocker","box","cart"]}
    entities={}
    for key,eid in ids.items():
        kind="CHARACTER"
        if key in {"sword","sword2"}:kind="ITEM"
        elif key in {"north","south"}:kind="REGION"
        elif key in {"warehouse","relief","reserve","recipient"}:kind="ACCOUNT"
        elif key=="grain":kind="RESOURCE"
        elif key=="sect":kind="ORGANIZATION"
        elif key in {"box","cart"}:kind="CONTAINER"
        entities[eid]={"entity_id":eid,"entity_kind":kind,"name":key,"created_at":point(1)}
    relations=[]
    def rel(subject,predicate,obj):
        value=entity_value(ids[obj]) if isinstance(obj,str) else {"kind":"BOOLEAN","value":obj}
        relations.append({"subject_id":ids[subject],"predicate":predicate,"object_value":value,"valid_time":{"start":point(1),"end":None,"bounds":"[)"}})
    owner,holder=("sect","master") if name=="legacy_sword" else ("smith","smith")
    for key,own in [("sword",owner),("sword2","smith2")]:
        rel(key,"legal_owner",own);rel(key,"possessed_by",holder if key=="sword" else "smith2");rel(key,"located_in",holder if key=="sword" else "smith2")
    for key in ("smith","smith2","master","protagonist","friend","rival","guard1","guard2","traveler"):rel(key,"located_in","north")
    rel("guard1","on_duty",True);rel("guard2","on_duty",True)
    rel("box","located_in","cart");rel("cart","located_in","north")
    resources=[{"account_id":ids[a],"resource_id":ids["grain"],"quantity":q,"unit":"bag"} for a,q in [("warehouse","100"),("relief","0"),("reserve","100"),("recipient","0")]]
    genesis={"entities":entities,"relations":relations,"resources":resources,"cognitions":[],"lifecycle":{}}
    # The reserve/guards are explicitly synthetic extensions for concurrent
    # constraint tests. The inherited White Bridge consignment remains 100.
    expected={"r1_sword":{"D5":["smith","smith"],"D15":["protagonist","protagonist"],"D25":["protagonist","friend"],"D35":["protagonist","rival"],"destroy_day":40,"frontier":42},
              "legacy_sword":{"D5":["sect","master"],"D13":["master","protagonist"],"D21":["master","rival"],"destroy_day":31,"frontier":33},
              "white_bridge":{"initial_consignment":"100","unit":"bag","regions":["north","south"],"crossing_arbitration":"P2_NOT_EXECUTED"}}[name]
    return {"name":name,"namespace":str(namespace),"ids":ids,"genesis":genesis,"frontiers":{eid:point(1,1) for eid in entities},"expected":expected}

def load_fixture(kernel,name="r1_sword",*,work_id=None):
    definition=fixture_definition(name)
    created=kernel.bootstrap_work("TEST_ONLY:"+name,definition["genesis"],frontiers=definition["frontiers"],work_id=work_id)
    return {**definition,**created}

def create_bound_task(kernel,principal,wr,story_time,subject_ids=None,predicates=None,*,plane=None,actor_id=None,token_budget=50000):
    """Use the real task + ContextCompiler protocol, including input artifacts."""
    from novel_factory.application.queries import WorldQueryService
    with kernel.db.uow(write=False) as r:
        grant=r.one("grants",{"grant_id":principal.grant_id})
        if plane is None:plane="SIMULATION" if r.one("branches",{"branch_id":wr["branch_id"]})["kind"]=="SIMULATION" else "WORLD"
        if subject_ids is None:subject_ids=list(kernel.revision(r,wr)["snapshot"]["entities"])[:10]
    manifest=kernel.put_artifact(principal,wr["work_id"],{"subject_ids":subject_ids,"predicates":predicates or ["owner","active_holder","lifecycle"]},"INPUT_MANIFEST")
    tid=str(uuid4())
    spec={"schema_version":"2.0","task_id":tid,"generation":0,"role_id":grant["role_id"],"task_kind":"kernel_acceptance",
          "state":"READY","world_ref":wr,"story_time":story_time,"context_bundle_id":None,"policy_version":"nf-policy-1","dependency_fingerprint":None,
          "runtime_contract_version":fingerprint({"fixture_role":grant["role_id"],"version":1}),"capability_grant_id":principal.grant_id,"input_artifacts":[manifest],
          "budget":{"currency":"USD","max_amount":"1","token_budget":token_budget},"wall_deadline":(datetime.now(timezone.utc)+timedelta(hours=2)).isoformat()}
    kernel.create_task(principal,spec,"fixture-task:"+tid)
    worker="fixture-worker:"+tid
    claim=kernel.claim_task(principal,worker,lease_seconds=3600,task_id=tid)
    cut={"plane":plane}
    if plane=="BELIEF":cut.update(actor_id=actor_id or grant["actor_id"],known_at=story_time)
    request={"schema_version":"2.1","task_id":tid,"generation":claim["generation"],"recipe_id":"ACTOR" if plane=="BELIEF" else "ADJUDICATOR",
             "input_manifest_ref":manifest,"read_contract":{"revision":{"mode":"FIXED","world_ref":wr},"process_policy":"ALLOW_PARTIAL","index_policy":"AUTHORITATIVE","max_wait_ms":0},
             "knowledge_cut":cut,"subject_time":{"kind":"POINT","at":story_time},"token_budget":token_budget,"idempotency_key":"fixture-context:"+tid}
    compiled=WorldQueryService(kernel).context_compile(principal,request)
    if compiled.get("status")!="READY":raise RuntimeError(f"Fixture compilation blocked: {compiled}")
    return {**claim,"worker_id":worker,"compiled":compiled,"manifest_ref":manifest}

def prepare_command(kernel,principal,wr,template_id,args,*,mode="STRICT_HEAD",key=None,reviewer=None,task=None):
    key=key or str(uuid4());at=args.get("at",args.get("target",point(100)))
    task=task or create_bound_task(kernel,principal,wr,at)
    proposal=kernel.propose(principal,task["task_id"],task["generation"],template_id,args,"proposal:"+key)
    approval_id=proposal["approval_id"]
    if approval_id is None:
        if reviewer is None:raise ValueError("Repair fixture needs a distinct reviewer")
        approval_id=kernel.approve(reviewer,proposal["proposal_ref"],wr["work_id"],idempotency_key="approve:"+key)
    prep={"schema_version":"2.1","task_id":task["task_id"],"generation":task["generation"],"context_bundle_id":proposal["context_bundle_id"],
          "concurrency_mode":mode,"base_world_ref":wr,"proposal_ref":proposal["proposal_ref"],"approval_refs":[approval_id],"dependency_handle":proposal["dependency_handle"],"idempotency_key":"prepare:"+key}
    cert=kernel.prepare(principal,prep)
    req={k:v for k,v in prep.items() if k not in {"dependency_handle"}}
    req.update(acceptance_certificate_ref=cert,dependency_fingerprint=proposal["dependency_fingerprint"],idempotency_key="accept:"+key)
    return {"request":req,"prepare_request":prep,"proposal":proposal,"task":task}

def run_command(kernel,principal,wr,template_id,args,**kwargs):
    prepared=prepare_command(kernel,principal,wr,template_id,args,**kwargs)
    receipt=kernel.accept(principal,prepared["request"])
    return {**prepared,"receipt":receipt,"world_ref":receipt["world_ref"]}

def seed_sword_history(kernel,fixture):
    """Accept the actual fixture history, rather than writing projections."""
    from novel_factory.domain.propositions import encode_proposition
    ids=fixture["ids"];principal=fixture["principal"];wr=fixture["world_ref"];steps=[]
    legacy=fixture["name"]=="legacy_sword"
    if legacy:
        first=run_command(kernel,principal,wr,"SET_RELATION",{"subject_id":ids["sword"],"predicate":"legal_owner","object_value":entity_value(ids["master"]),"at":point(10)})
    else:
        first=run_command(kernel,principal,wr,"TRANSFER_ITEM",{"item_id":ids["sword"],"from_id":ids["smith"],"to_id":ids["protagonist"],"transfer_kind":"GIFT","at":point(10)})
    steps.append(first);wr=first["world_ref"]
    loan_day=12 if legacy else 20;src="master" if legacy else "protagonist";dst="protagonist" if legacy else "friend"
    loan=run_command(kernel,principal,wr,"TRANSFER_ITEM",{"item_id":ids["sword"],"from_id":ids[src],"to_id":ids[dst],"transfer_kind":"LOAN","at":point(loan_day)})
    steps.append(loan);wr=loan["world_ref"]
    evidence={"kind":"EVENT","world_ref":wr,"event_ref":loan["receipt"]["accepted_event_refs"][0]}
    cognition=run_command(kernel,principal,wr,"RECORD_COGNITION",{"actor_id":ids["protagonist"],"statement":encode_proposition(ids["sword"],"possessed_by",entity_value(ids[dst])),
       "record_kind":"BELIEF","epistemic_mode":"OBSERVED","evidence_refs":[evidence],"at":point(loan_day,1)})
    steps.append(cognition);wr=cognition["world_ref"]
    theft=run_command(kernel,principal,wr,"TRANSFER_ITEM",{"item_id":ids["sword"],"from_id":ids[dst],"to_id":ids["rival"],"transfer_kind":"STEAL","at":point(20 if legacy else 30)})
    steps.append(theft);wr=theft["world_ref"]
    destroy=run_command(kernel,principal,wr,"DESTROY_ITEM",{"item_id":ids["sword"],"at":point(31 if legacy else 40)})
    steps.append(destroy);wr=destroy["world_ref"]
    advance=run_command(kernel,principal,wr,"ADVANCE_SCOPE",{"scope_id":ids["sword"],"target":point(33 if legacy else 42)})
    steps.append(advance)
    return {**fixture,"world_ref":advance["world_ref"],"steps":steps,"loan":loan,"theft":theft,"destroy":destroy}
