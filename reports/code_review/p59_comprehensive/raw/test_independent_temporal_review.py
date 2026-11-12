"""Exercise timeline axis projections through public services with accepted provenance."""
import pytest

from tests.queries.conftest import query_env, p, request, seed, start, uid
from novel_factory.domain.propositions import encode_proposition
from novel_factory.fixtures import entity_value, load_fixture, point, run_command, seed_sword_history


def test_native_belief_acquisition_has_source_time(query_env):
    k = query_env["kernel"]
    world = seed_sword_history(k, load_fixture(k))
    ids = world["ids"]
    owner, wr = world["principal"], world["world_ref"]
    actor = ids["protagonist"]
    document = k.put_artifact(owner, wr["work_id"], b"Observation record at day 51", "EVIDENCE")
    evidence = {"kind": "ARTIFACT", "artifact_ref": document}
    observed = run_command(k, owner, wr, "RECORD_COGNITION", {
        "actor_id": actor, "record_kind": "OBSERVATION", "epistemic_mode": "HEARD", "at": point(51),
        "statement": encode_proposition(ids["sword"], "possessed_by", entity_value(ids["friend"])),
        "evidence_refs": [evidence]})
    task = start(query_env, {**world, "world_ref":observed["world_ref"]}, subjects=[ids["sword"]])
    for day, allowed in ((point(20,1), "EVENT"), (point(51), "COGNITION")):
        req = request(task, at=day, plane="BELIEF", actor=actor,
                      operation={"kind":"TIMELINE","subject_ids":[ids["sword"]],"entry_kinds":["OBSERVATION"],
                                 "time_axis":"SOURCE","include_inactive":True})
        for axis, count in (("SOURCE",1),("RELEVANT",1),("TARGET",0),("VALID",0)):
            req["query"]["time_axis"] = axis
            result = query_env["query"].query(owner, req)["payload"]
            assert result["coverage"] == "UNDECLARED"
            assert len(result["entries"]) == count
            if count:
                entry, = result["entries"]
                assert entry["source"]["kind"] == allowed and entry["source_story_time"]["at"] == day
                assert entry["evidence_refs"] and entry["target_time"] is None and entry["valid_time"] is None


def test_unspecified_and_bounded_plan_targets(query_env):
    k = query_env["kernel"]
    world = seed(query_env)
    owner = world["principal"]
    work = world["world_ref"]["work_id"]
    subject = world["ids"]["sword"]
    story = k.put_artifact(owner, work, b"Unscheduled scene", "TEXT")
    random_id, bounded_id = uid(), uid()
    base = {"plan_ref":story,"status":"APPROVED","subject_ids":[subject],"summary":"An optional scene",
            "requires_active":[subject],"fulfillment_event_refs":[]}
    plans = [{**base, "plan_id":random_id, "target_time":{"kind":"UNSPECIFIED","constraint_refs":[]}},
             {**base, "plan_id":bounded_id, "target_time":{"kind":"BOUNDED","earliest":p(30),"latest":p(40),"constraint_refs":[]}}]
    manifest = k.put_artifact(owner, work, {"schema_version":"p1","plans":plans}, "PLAN_MANIFEST")
    t = start(query_env,world)
    op = {"kind":"TIMELINE","subject_ids":[subject],"entry_kinds":["PLAN"],"time_axis":"TARGET","include_inactive":True}
    for day, expected in ((p(29),[random_id]),(p(30),[bounded_id,random_id]),(p(40),[bounded_id,random_id]),(p(41),[random_id])):
        req = request(t,at=day,operation=op.copy())
        req["knowledge_cut"] = {"plane":"PLAN","plan_manifest_ref":manifest}
        result = query_env["query"].query(owner,req)["payload"]["entries"]
        assert [e["entry_id"] for e in result] == expected
        assert result[-1]["target_time"] == plans[0]["target_time"]
        req["query"]["time_axis"] = "SOURCE"
        assert not query_env["query"].query(owner,req)["payload"]["entries"]
