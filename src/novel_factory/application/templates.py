"""Finite, closed-world P1 commands and their server-derived footprints.

Every writer, including strict writes and repairs, uses effects_footprint().
Negative ranges are deliberately conservative (branch-wide registered predicate
ranges). This sacrifices some concurrency without weakening phantom protection.
"""
from __future__ import annotations
from decimal import Decimal
from uuid import UUID, uuid5

from novel_factory.domain.errors import DomainError
from novel_factory.domain.canonical import fingerprint
from novel_factory.contracts.registry import validate_contract

GUARDED = {"TRANSFER_ITEM","DESTROY_ITEM","RESOURCE_TRANSFER","GUARD_LEAVE","PASS_CLEAR_ROUTE"}
ALL_TEMPLATES = GUARDED | {"SET_RELATION","ADVANCE_SCOPE","REPAIR_EVENT","RECORD_COGNITION",
                           "REGISTER_PROCESS","SCHEDULE_ADVANCE","JOINT_INTERACTION"}

def _fail(message,code="HARD_RULE_VIOLATION"):
    raise DomainError(code,message)

def _value(snapshot, subject, predicate, at):
    from novel_factory.domain.reducer import state_at
    return state_at(snapshot,subject,predicate,at)

def _entity(snapshot, eid):
    if eid not in snapshot["entities"]:_fail("Command references an unavailable entity")

def _current_relations(snapshot,subject,predicate,at):
    key=lambda p:(p["tick"],p["phase"])
    return [r for r in snapshot["relations"] if r["subject_id"]==subject and r["predicate"]==predicate and r.get("plane","WORLD")=="WORLD"
            and key(r["valid_time"]["start"])<=key(at) and (r["valid_time"]["end"] is None or key(at)<key(r["valid_time"]["end"]))]

def effects_footprint(effects):
    reads,writes=set(),set()
    for effect in effects:
        kind=effect["kind"]
        if kind in {"RELATION_SET","RELATION_TRANSITION"}:
            subject,predicate=effect["subject_id"],effect["predicate"]
            writes|={"ENTITY:"+subject,"PREDICATE_RANGE:"+predicate}
            reads.add("ENTITY:"+subject)
            target=effect.get("object_id") or effect.get("object_value",{}).get("entity_id")
            if target:reads.add("ENTITY:"+target)
        elif kind=="RESOURCE_DELTA":
            key=f"RESOURCE_CONSTRAINT:{effect['account_id']}:{effect['resource_id']}:{effect['unit']}"
            reads|={key,"ENTITY:"+effect["account_id"],"ENTITY:"+effect["resource_id"]}
            writes.add(key)
        elif kind=="LIFECYCLE_SET":
            reads.add("ENTITY:"+effect["entity_id"])
            writes|={"ENTITY:"+effect["entity_id"],"PREDICATE_RANGE:possessed_by","PREDICATE_RANGE:located_in","PREDICATE_RANGE:contains"}
        elif kind=="COGNITIVE_RECORD_ADD":
            reads.add("ENTITY:"+effect["actor_id"])
            writes.add("ENTITY:"+effect["actor_id"])
        else:_fail("Unregistered effect kind")
    return reads,writes

def build(template_id,args,snapshot,proposal_id):
    """Return exact effects, footprint and affected process scopes.

    Fixed outcome commands are authorized control-plane decisions. No public
    autonomous intent queue exists in P1. Full interactive arbitration is P2.
    """
    validate_contract("TemplateCommand",{"template_id":template_id,"args":args},version="commands")
    if template_id not in ALL_TEMPLATES:_fail("Template is not registered","UNPROVEN_FOOTPRINT")
    ns=UUID(proposal_id); effects=[]; read=set(); scopes=set(); extra_bumps=set(); advance_targets=None
    at=args.get("at",args.get("target"))
    def relation(subject,predicate,value):
        _entity(snapshot,subject)
        if value["kind"]=="ENTITY":_entity(snapshot,value["entity_id"])
        for old in _current_relations(snapshot,subject,predicate,at):
            effects.append({"kind":"RELATION_TRANSITION","effect_key":f"close:{len(effects)}","relation_id":old["relation_id"],"operation":"CLOSE",
                "subject_id":subject,"predicate":predicate,"object_value":old["object_value"],"at":at,"expected_relation_version_id":old["relation_version_id"]})
        effects.append({"kind":"RELATION_TRANSITION","effect_key":f"open:{len(effects)}","relation_id":str(uuid5(ns,f"relation:{len(effects)}")),
            "operation":"OPEN","subject_id":subject,"predicate":predicate,"object_value":value,"at":at,"expected_relation_version_id":None})
        scopes.add(subject)
    def ref(eid):return {"kind":"ENTITY","entity_id":eid}
    def set_entity_relation(subject,predicate,target):
        effects.append({"kind":"RELATION_SET","effect_key":f"set:{len(effects)}","subject_id":subject,
                        "predicate":predicate,"object_id":target,"value_status":"VALUE"})
        scopes.add(subject)
    if template_id=="TRANSFER_ITEM":
        item,src,dst=args["item_id"],args["from_id"],args["to_id"]
        for e in (item,src,dst):_entity(snapshot,e)
        if src==dst:_fail("Source and destination must differ")
        holder=_value(snapshot,item,"active_holder",at)
        if holder["data"] != ref(src):_fail("Transfer source does not currently hold the item","RESOURCE_CONFLICT")
        if args["transfer_kind"] in {"GIFT","LOAN"} and _value(snapshot,item,"owner",at)["data"]!=ref(src):
            _fail("Only the current owner can give or lend the item","RESOURCE_CONFLICT")
        set_entity_relation(item,"possessed_by",dst)
        if args["transfer_kind"]=="GIFT":set_entity_relation(item,"legal_owner",dst)
        # Physical location follows the new holder through graph containment;
        # a unique item may not retain its old region edge after transfer.
        set_entity_relation(item,"located_in",dst)
    elif template_id=="DESTROY_ITEM":
        item=args["item_id"];_entity(snapshot,item)
        if _value(snapshot,item,"lifecycle",at)["data"] != {"kind":"STRING","value":"ACTIVE"}:_fail("Item is not active")
        effects.append({"kind":"LIFECYCLE_SET","effect_key":"destroy","entity_id":item,"lifecycle":"DESTROYED"});scopes.add(item)
    elif template_id=="RESOURCE_TRANSFER":
        source,target,resource=args["from_account"],args["to_account"],args["resource_id"]
        for e in (source,target,resource):_entity(snapshot,e)
        amount=Decimal(args["quantity"])
        if amount<=0 or source==target:_fail("Transfer needs positive amount and distinct accounts")
        for i,(account,quantity) in enumerate([(source,-amount),(target,amount)]):
            effects.append({"kind":"RESOURCE_DELTA","effect_key":f"resource:{i}","account_id":account,"resource_id":resource,"quantity":format(quantity,"f"),"unit":args["unit"]})
            scopes.add(account)
    elif template_id=="SET_RELATION":
        relation(args["subject_id"],args["predicate"],args["object_value"])
    elif template_id=="GUARD_LEAVE":
        guard,region=args["guard_id"],args["region_id"]
        _entity(snapshot,guard);_entity(snapshot,region)
        on_duty=[]
        for eid in snapshot["entities"]:
            if _value(snapshot,eid,"located_in",at)["data"]==ref(region) and _value(snapshot,eid,"on_duty",at)["data"]=={"kind":"BOOLEAN","value":True}:on_duty.append(eid)
        if guard not in on_duty or len(on_duty)<2:_fail("At least one guard must remain on duty","RESOURCE_CONFLICT")
        read|={"PREDICATE_RANGE:on_duty","PREDICATE_RANGE:located_in","ENTITY:"+region}
        relation(guard,"on_duty",{"kind":"BOOLEAN","value":False})
    elif template_id=="PASS_CLEAR_ROUTE":
        traveler,region,dest=args["traveler_id"],args["region_id"],args["destination_id"]
        for e in (traveler,region,dest):_entity(snapshot,e)
        if _current_relations(snapshot,region,"blocked_by",at):_fail("Route is blocked","RESOURCE_CONFLICT")
        read|={"PREDICATE_RANGE:blocked_by","ENTITY:"+region}
        relation(traveler,"located_in",ref(dest))
    elif template_id=="ADVANCE_SCOPE":
        _entity(snapshot,args["scope_id"]);scopes.add(args["scope_id"])
        read.add("ENTITY:"+args["scope_id"])
    elif template_id=="RECORD_COGNITION":
        _entity(snapshot,args["actor_id"])
        effects.append({"kind":"COGNITIVE_RECORD_ADD","effect_key":"cognition","actor_id":args["actor_id"],"statement":args["statement"],
                        "record_kind":args["record_kind"],"epistemic_mode":args["epistemic_mode"],"evidence_refs":args["evidence_refs"]})
        scopes.add(args["actor_id"])
    elif template_id=="REGISTER_PROCESS":
        # Registration selects a typed process record, not a fabricated event.
        process=args["process"]
        for eid in set(process["scope_ids"]) | set(process["participant_ids"]):
            _entity(snapshot,eid);read.add("ENTITY:"+eid)
        read.add("PROCESS_COVERAGE:"+process["process_id"])
        extra_bumps|={"SCOPE_REGISTRY:world","CHANNEL_REGISTRY:closed-world",
                      "PROCESS_COVERAGE:"+process["process_id"]}
    elif template_id=="SCHEDULE_ADVANCE":
        for scope in args["scope_ids"]:
            _entity(snapshot,scope);scopes.add(scope);read.add("ENTITY:"+scope)
        advance_targets={scope:args["target"] for scope in scopes}
    elif template_id=="JOINT_INTERACTION":
        from novel_factory.domain.scheduling import joint_effects
        joint=joint_effects(args["commands"],snapshot,proposal_id,args["at"],build)
        effects=joint["effects"];read|=joint["reads"]
        scopes|=joint["scopes"] | set(args["scope_ids"])
        for scope in args["scope_ids"]:_entity(snapshot,scope)
        # Process frontiers are versioned record metadata; world subject scopes
        # are advanced with the same accepted result, not in a second write.
        extra_bumps|=joint["bumps"] | {"CONFLICT_GROUP:"+args["interaction_id"],"SCOPE_REGISTRY:world"}
        read.add("CONFLICT_GROUP:"+args["interaction_id"])
        for pid in args["process_ids"]:
            read.add("PROCESS_COVERAGE:"+pid);extra_bumps.add("PROCESS_COVERAGE:"+pid)
        for outgoing in args["outgoing_processes"]:
            extra_bumps|={"CHANNEL_REGISTRY:closed-world","PROCESS_COVERAGE:"+outgoing["process"]["process_id"]}
    elif template_id=="REPAIR_EVENT":
        effects=args["replacement_event"]["effects"]
        # Repair replays the whole selected history. It is always strict.
        scopes=set(snapshot["entities"])
    er,ew=effects_footprint(effects)
    ew|=extra_bumps
    read|=er|{"RULESET:world","SCOPE_REGISTRY:world","CHANNEL_REGISTRY:closed-world"}
    for scope in scopes:read.add("PROCESS_COVERAGE:"+scope);ew.add("PROCESS_COVERAGE:"+scope)
    # A closure identity is immutable and refers to the precise controlled
    # outcome/window. Replacement/new registration bumps the same guard.
    closure_key="CONFLICT_GROUP:"+fingerprint({"subjects":sorted(scopes),"at":at})
    read.add(closure_key);ew.add(closure_key)
    # Predicate ranges are invalidated by every matching write. They need not
    # serialize independent writers unless the template actually reads that
    # range (negative lookup / cross-row cardinality constraint).
    guarded_writes={k for k in ew if not k.startswith("PREDICATE_RANGE:") or k in read}
    result={"effects":effects,"reads":sorted(read),"writes":sorted(guarded_writes),"bumps":sorted(ew),"process_scopes":sorted(scopes),"closure_key":closure_key}
    if advance_targets is not None:result["advance_targets"]=advance_targets
    return result
