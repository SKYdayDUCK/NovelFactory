"""Paired real PG17 retcon with an explicit cross-time causal parent."""
import argparse
from copy import deepcopy
from pathlib import Path
import sys
from unittest.mock import patch
from uuid import uuid4
import scion_paired as paired


def prepare(kernel, case):
    from novel_factory.fixtures import create_bound_task, load_fixture, point, run_command
    assert case == "history-retcon"
    world = load_fixture(kernel, "r1_sword")
    principal, ids = world["principal"], world["ids"]
    gift = run_command(kernel, principal, world["world_ref"], "TRANSFER_ITEM", {
        "item_id": ids["sword"], "from_id": ids["smith"], "to_id": ids["protagonist"],
        "transfer_kind": "GIFT", "at": point(10)})
    loan = run_command(kernel, principal, gift["world_ref"], "TRANSFER_ITEM", {
        "item_id": ids["sword"], "from_id": ids["protagonist"], "to_id": ids["friend"],
        "transfer_kind": "LOAN", "at": point(20)})
    original = loan["receipt"]["accepted_event_refs"][0]
    with kernel.db.uow(write=False) as repo:
        replacement = deepcopy(repo.one("events", {"event_version_id": original["event_version_id"]})["payload"])
    replacement.update(event_version_id=str(uuid4()), representation_id=str(uuid4()),
                       causal_parents=[gift["receipt"]["accepted_event_refs"][0]])
    replacement["effects"].append({"kind": "RELATION_SET", "effect_key": "repair:gift:legal_owner",
                                  "subject_id": ids["sword"], "predicate": "legal_owner",
                                  "object_id": ids["friend"], "value_status": "VALUE"})
    old = loan["world_ref"]
    fork = kernel.fork(principal, old, "REPAIR", "scion-profile-retcon")
    reviewer = kernel.issue_grant(principal, old["work_id"], role_id="control_operator",
                   branch_ids=[fork["branch_id"]], capabilities=["READ","APPROVE","REPAIR"],
                   planes=["WORLD"], max_story_time=point(100))["principal"]
    task = create_bound_task(kernel, principal, fork, point(100))
    return {"old":old, "fork":fork, "principal":principal, "reviewer":reviewer,
            "original":original, "task_id":task["task_id"], "generation":task["generation"],
            "args":{"occurrence_id":original["occurrence_id"], "replacement_event":replacement,
                    "reason":"D20 gift with evidence from its D10 causal predecessor"}}


def measure(kernel, url, f, output, case):
    from benchmarks import scion_profile as profile
    from novel_factory.domain.canonical import fingerprint
    from novel_factory.contracts.registry import validate_contract
    opts=argparse.Namespace(output=output,repeats=1,profiles=False)
    with profile.DatabaseObserver(url) as observer, profile.Attribution(kernel) as meter:
        exp=profile.Experiment(opts,kernel,observer)
        principal, base = f["principal"], f["fork"]
        exp.dataset({"world_ref":base},case)
        def sample(label, callback):
            value,_=exp.measure(meter,label,callback,suite=case,checkpoint=2)
            return value
        proposal=sample("propose",lambda:kernel.propose(principal,f["task_id"],f["generation"],
                              "REPAIR_EVENT",f["args"],"profile-retcon-propose"))
        assert proposal["approval_id"] is None
        approval=sample("approve",lambda:kernel.approve(f["reviewer"],proposal["proposal_ref"],
                              base["work_id"],idempotency_key="profile-retcon-approve"))
        request={"schema_version":"2.1","task_id":f["task_id"],"generation":f["generation"],
                 "context_bundle_id":proposal["context_bundle_id"],"concurrency_mode":"STRICT_HEAD",
                 "base_world_ref":base,"proposal_ref":proposal["proposal_ref"],
                 "approval_refs":[approval],"dependency_handle":proposal["dependency_handle"],
                 "idempotency_key":"profile-retcon-cert"}
        certificate=sample("prepare",lambda:kernel.prepare(principal,request))
        accept={k:v for k,v in request.items() if k!="dependency_handle"}
        accept.update(acceptance_certificate_ref=certificate,dependency_fingerprint=proposal["dependency_fingerprint"],
                      idempotency_key="profile-retcon-accept")
        receipt=sample("accept",lambda:kernel.accept(principal,accept))
        validate_contract("GuardedCommitReceipt",receipt,"2.1")
        assert sample("accept_receipt_retry",lambda:kernel.accept(principal,accept))==receipt
        promoted=sample("promote",lambda:kernel.promote(principal,f["old"],receipt["world_ref"],"profile-promote"))
        with kernel.db.uow(write=False) as repo:
            old=kernel.revision(repo,f["old"])
            revised=kernel.revision(repo,receipt["world_ref"])
            assert old["selection"][f["original"]["occurrence_id"]]==f["original"]["event_version_id"]
            assert revised["selection"][f["original"]["occurrence_id"]]==f["args"]["replacement_event"]["event_version_id"]
            assert revised["provenance"]["history_epoch"]>old["provenance"]["history_epoch"]
            assert len(revised["selection"])==len(old["selection"])==2
            exp.result["selected_effects_identity"]=f["args"]["replacement_event"]["effect_group_id"]
            exp.result["verified_state_hash"]=fingerprint(revised["snapshot"])
            exp.result["verified_event_selection"]=revised["selection"]
            exp.result["promotion_result_keys"]=sorted(promoted)
        exp.summarize()
        exp.result["status"]="PROFILE_COMPLETE_WITH_INVARIANTS_CHECKED"
        if hasattr(kernel,"scion"):
            exp.result["scion_counters_after_measurement"]=kernel.scion.counts()
    exp.result["database_observer"]=observer.result()
    exp.save()


if __name__=="__main__":
    if sys.argv[1:]==["--child"]:
        with patch.object(paired,"profile_arm",measure):
            paired.child()
    else:
        with patch.object(paired,"__file__",__file__),patch.object(paired,"prepare",prepare):
            paired.main()
