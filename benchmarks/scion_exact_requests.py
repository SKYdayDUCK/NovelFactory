"""Time identical application requests, including fixed proposal and certificate refs.

Frozen setup mints a real approval and certificate. Each arm receives an exact
clone of their database rows and object bytes plus matching task/principal.
Fresh idempotency keys measure normal prepare/propose; the accept reuses the
frozen authentic signed certificate. A repeated accept must return the same receipt.
"""
import argparse
from copy import deepcopy
import sys
from unittest.mock import patch
import scion_paired as paired


def prepare(kernel, case):
    from novel_factory.application.queries import WorldQueryService
    original = case.removesuffix("-exact")
    fixture = base_prepare(kernel, original)
    world, context = fixture["world"], fixture["request"]
    principal, wr = world["principal"], world["world_ref"]
    service = WorldQueryService(kernel)
    assert service.context_compile(principal, context)["status"] == "READY"
    proposal = kernel.propose(principal, context["task_id"], context["generation"], fixture["template"],
                              fixture["action"], "exact-fixture-proposal")
    mode = "DEPENDENCY_GUARDED" if fixture["template"] == "RESOURCE_TRANSFER" else "STRICT_HEAD"
    prepare_req = dict(schema_version="2.1", task_id=context["task_id"], generation=context["generation"],
                       context_bundle_id=proposal["context_bundle_id"], concurrency_mode=mode,
                       base_world_ref=wr, proposal_ref=proposal["proposal_ref"],
                       approval_refs=[proposal["approval_id"]], dependency_handle=proposal["dependency_handle"],
                       idempotency_key="exact-fixture-certificate")
    cert = kernel.prepare(principal, prepare_req)
    accept = {k:v for k,v in prepare_req.items() if k != "dependency_handle"}
    accept.update(acceptance_certificate_ref=cert, dependency_fingerprint=proposal["dependency_fingerprint"],
                  idempotency_key="exact-accept")
    prepare_req["idempotency_key"] = "exact-measured-certificate"
    fixture.update(known_prepare=prepare_req, known_accept=accept,
                   known_propose={"task_id":context["task_id"], "generation":context["generation"],
                                  "template_id":fixture["template"], "args":fixture["action"],
                                  "idempotency_key":"exact-measured-propose"})
    return fixture


base_prepare = paired.prepare


def measure(kernel, url, fixture, output, case):
    from benchmarks import scion_profile as profile
    from novel_factory.application.queries import WorldQueryService
    from novel_factory.contracts.registry import validate_contract
    from novel_factory.domain.canonical import fingerprint
    opts = argparse.Namespace(output=output, repeats=1, profiles=False)
    with profile.DatabaseObserver(url) as observer, profile.Attribution(kernel) as meter:
        exp = profile.Experiment(opts, kernel, observer)
        world = fixture["world"]
        n = fixture["count"]
        exp.dataset(world, case)
        principal = world["principal"]
        service = WorldQueryService(kernel)
        def sample(label, req, callback):
            value,_ = exp.measure(meter,label,callback,suite=case,checkpoint=n,
                                  extra={"request_sha256":fingerprint(req)})
            return value
        q, p, c, a = (fixture[key] for key in ("query","known_propose","known_prepare","known_accept"))
        sample("query", q, lambda:service.query(fixture["read_principal"],q))
        sample("propose",p,lambda:kernel.propose(principal,**p))
        sample("prepare",c,lambda:kernel.prepare(principal,c))
        receipt = sample("accept",a,lambda:kernel.accept(principal,a))
        validate_contract("GuardedCommitReceipt",receipt,"2.1")
        assert sample("accept_receipt_retry",a,lambda:kernel.accept(principal,a)) == receipt
        assert receipt["parent_world_ref"] == world["world_ref"]
        with kernel.db.uow(write=False) as repo:
            post = kernel.revision(repo,receipt["world_ref"])
            assert len(post["selection"])==n+1
            assert kernel.head(repo,post["work_id"],post["branch_id"])==receipt["world_ref"]
            exp.result["verified_state_hash"]=fingerprint(post["snapshot"])
            exp.result["verified_event_selection"]=post["selection"]
        exp.summarize()
        exp.result["status"]="PROFILE_COMPLETE_WITH_INVARIANTS_CHECKED"
        if hasattr(kernel,"scion"):
            exp.result["scion_counters_after_measurement"]=kernel.scion.counts()
    exp.result["database_observer"]=observer.result()
    exp.save()


if __name__ == "__main__":
    if sys.argv[1:]==["--child"]:
        # Use the normal history branch of the child dispatcher to run this meter.
        with patch.object(paired,"profile_arm",measure):
            paired.child()
    else:
        with patch.object(paired,"__file__",__file__),patch.object(paired,"prepare",prepare):
            paired.main()
