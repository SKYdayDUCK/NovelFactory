"""AT-43. Capture the real actor HTTP input after reading its own organization directory."""
import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

from cross_service_scene import Mail, backend, dual_databases, kernel_env
from test_p5_mcp import config, exercise

from agent_workshop.auth import connect
from agent_workshop.organization import bootstrap
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.domain.propositions import encode_proposition
from novel_factory.fixtures import fixture_definition, point


def test_roster_routing_stays_outside_belief_and_other_actors_prompt(kernel_env, dual_databases, tmp_path):
    kernel = kernel_env["kernel"]
    fixture = fixture_definition("r1_sword")
    ids = fixture["ids"]
    actor, other, sword = ids["protagonist"], ids["rival"], ids["sword"]
    route_mark = "aw-route-" + uuid4().hex
    private_value = str(uuid4())
    genesis = fixture["genesis"]
    genesis["entities"][private_value] = {
        "entity_id": private_value, "entity_kind": "CHARACTER", "name": "isolated synthetic holder",
        "created_at": point(1),
    }
    genesis["cognitions"].append({
        "record_id": str(uuid4()), "actor_id": other, "record_kind": "BELIEF",
        "statement": encode_proposition(sword, "possessed_by", {"kind": "ENTITY", "entity_id": private_value}),
        "acquired_at": point(1), "evidence_refs": [],
    })
    world = kernel.bootstrap_work("AT-43 isolated synthetic project", genesis, frontiers=fixture["frontiers"])
    wr, owner = world["world_ref"], world["principal"]
    with kernel.db.uow(write=False) as repo:
        original_cognitions = kernel.revision(repo, wr)["snapshot"]["cognitions"]
        assert len(original_cognitions) == 1 and original_cognitions[0]["actor_id"] == other
        assert original_cognitions[0]["object_value"]["entity_id"] == private_value
    org, inst, nf_instance = (str(uuid4()) for _ in range(3))
    with connect(dual_databases["aw"]) as conn:
        conn.execute("INSERT INTO aw_installations(installation_id,nf_instance_id) VALUES (%s,%s)", (inst, nf_instance))
        bootstrap(conn, org, "Standard", scope_ref=route_mark, installation_id=inst)

    async def read_roster():
        client = Mail(dual_databases["aw"], org, inst, tmp_path)
        async with client.stack:
            return await client.call("actor-1", "aw_roster_get", {})

    roster = asyncio.run(read_roster())
    assert {e["employee_id"] for e in roster["employees"]} == {"actor-1", "volume"}
    assert roster["routes"] and all(e["scope_ref"] == route_mark for e in roster["routes"])
    staff = {member["employee_id"]: member for member in roster["employees"]}
    assert staff["actor-1"]["authorized_scope"] == {"organization": org, "scope_ref": route_mark}
    assert staff["actor-1"]["allowed_routes"] == [edge for edge in roster["routes"] if edge["src"] == "actor-1"]
    assert "allowed_routes" not in staff["volume"] and "authorized_scope" not in staff["volume"]
    assert private_value not in json.dumps(roster)
    # Pass the authenticated tool result through NF's ordinary frozen assignment input.
    # It is an organizational document, separate from the compiled ACTOR knowledge cut.
    assignment = json.dumps({"organization_directory": roster}, ensure_ascii=False)
    with backend() as (http, received, server):
        gateway = AgentGateway(kernel, {"loopback": http})
        service = RuntimeTaskService(kernel, gateway)

        def prepare(character, instructions):
            return service.prepare(owner, wr, role_id="character_actor", task_kind="character_action",
                                   story_time=point(1), actor_id=character, subject_ids=[sword],
                                   predicates=["possessed_by"], assignment=instructions, adapter_id="loopback")

        local = prepare(actor, assignment)
        private = prepare(other, "Inspect only the holder known to this character.")

        async def actor_read(job, name):
            result = {}
            binding = {"task_id": job.task["task_id"], "generation": job.task["generation"], "worker_id": job.worker_id}
            async def read(session):
                for key, tool, args in (
                    ("context", "nf_context_get", {}),
                    ("query", "nf_query_batch", {"lookups": [{"subject_id": sword, "predicate": "possessed_by"}]}),
                ):
                    out = await session.call_tool(tool, args)
                    assert not out.isError, out.structuredContent
                    result[key] = out.structuredContent
            await exercise(config(tmp_path / (name + ".json"), kernel_env, job.private_token, binding), read)
            return result

        own_read = asyncio.run(actor_read(local, "visible-actor"))
        other_read = asyncio.run(actor_read(private, "private-actor"))

        def answer(read):
            return read["query"]["items"][0]["payload"]["state_items"][0]["answer"]

        assert own_read["context"]["actor_id"] == actor
        assert answer(own_read)["knowledge_status"] == "UNKNOWN" and answer(own_read)["data"] is None
        assert answer(other_read)["knowledge_status"] == "KNOWN" and answer(other_read)["data"]["entity_id"] == private_value
        assert route_mark not in json.dumps([own_read, other_read]) and private_value not in json.dumps(own_read)

        def choose(ctx):
            return {
                "schema_version": "2.0", "proposal_id": str(uuid4()),
                "task_id": ctx["task_id"], "generation": ctx["generation"],
                "context_bundle_id": ctx["context_bundle_id"], "world_ref": wr, "actor_id": ctx["actor_id"],
                "intent": "Wait.", "targets": [], "story_interval": {"start": point(1), "end": point(2), "bounds": "[)"},
                "resource_requests": [], "preconditions": [], "evidence_refs": [],
                "dependency_fingerprint": ctx["dependency_fingerprint"], "idempotency_key": "at43-action",
            }

        server.candidate = choose
        result = gateway.execute(local.principal, {
            "schema_version": "2.1", "task_id": local.task["task_id"], "generation": local.task["generation"],
            "worker_id": local.worker_id, "call_key": "at43", "input_ref": local.agent_input["input_ref"],
            "budget_id": kernel.tasks.create_budget(wr["work_id"], "1"), "reserve_amount": "0.10",
            "token_reservation": 100000,
        })
        assert result["validation_status"] == "VALIDATED_CANDIDATE" and len(received) == 1
        packet = json.loads(received[0]["messages"][1]["content"])
        assert json.loads(packet["assignment"])["organization_directory"] == roster
        assert packet["read_cut"]["knowledge_cut"]["actor_id"] == actor
        assert packet["read_cut"]["knowledge_cut"]["plane"] == "BELIEF"
        assert route_mark not in json.dumps(packet["context"]) and private_value not in json.dumps(received)
        with kernel.db.uow(write=False) as repo:
            assert kernel.head(repo, wr["work_id"], wr["branch_id"]) == wr
            persisted = kernel.revision(repo, wr)["snapshot"]["cognitions"]
            assert persisted == original_cognitions and route_mark not in json.dumps(persisted)
            assert private_value in json.dumps(persisted) and len(repo.many("call_attempts", {"task_id": local.task["task_id"]})) == 1
        out = Path(os.environ["AW_ACCEPTANCE_OUTPUT"])
        out.mkdir(exist_ok=True, parents=True)
        (out / "at43.json").write_text(json.dumps({
            "scope": "Authenticated AW directory supplied as frozen task instructions; NF builds and validates the actor input.",
            "commercial_calls": 0, "world_ref_before_and_after": wr, "route_marker": route_mark,
            "other_actor_belief_marker": private_value, "roster_tool_result": roster,
            "actor_mcp": own_read, "private_actor_mcp_positive_control": other_read,
            "persisted_cognitions_after": persisted, "loopback_http_body": received[0],
            "validated_candidate_attempt": result["attempt_id"],
        }, ensure_ascii=False, indent=2) + "\n")
