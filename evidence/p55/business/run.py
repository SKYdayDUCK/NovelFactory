"""Preregistered synthetic writer rework using ordinary NF tasks on cloned PostgreSQL worlds."""
import argparse
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from time import perf_counter
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from benchmarks.p2_behavioral_load import isolated_kernel
from benchmarks.p55_semantic_comparison import guard
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import create_bound_task, load_fixture, point, prepare_command, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import ModelResponse
from novel_factory.infrastructure.objects import ObjectStore


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def synthetic_tokens(text):
    return len(re.findall(r"[A-Za-z0-9_]+|[^\s]", text))


class LocalWriter:
    configuration = {
        "adapter_kind": "DETERMINISTIC_TEST", "provider": "local", "model": "s9-oracle-writer-1",
        "max_output_tokens": 1024, "timeout_seconds": 10, "isolation": "STATELESS_TEXT_ONLY",
        "isolation_evidence": "In-process, no sockets, no tools, receives frozen authorized messages only",
        "execution_kind": "TEST_FIXTURE",
    }

    def check_available(self):
        pass

    def generate(self, request):
        context = json.loads(request.messages[1]["content"])["context"]
        # Read only facts released to this NF writer at its fixed WorldRef.
        rows = [json.loads(fact["statement"]) for fact in context["facts"]]
        assert len(rows) == 1 and context["role_id"] == "writer"
        fact = rows[0]
        if fact["predicate"] == "on_duty":
            assert fact["value"]["kind"] == "BOOLEAN"
            line = "守卫仍在岗。" if fact["value"]["value"] else "守卫已经离岗。"
        else:
            assert fact["predicate"] == "legal_owner" and fact["value"]["kind"] == "ENTITY"
            line = "登记的剑主为 " + fact["value"]["entity_id"] + "。"
        identity = {k: context[k] for k in ("task_id", "generation", "context_bundle_id", "world_ref", "role_id", "actor_id")}
        content = json.dumps({"schema_version": "2.1", **identity, "status": "SUCCEEDED",
                              "unresolved_questions": [], "payload": {"text": line, "event_refs": [],
                              "important_deltas": [], "evidence_refs": []}}, ensure_ascii=False)
        return ModelResponse(text=content, provider_receipt="synthetic:" + request.request_sha256,
            input_tokens=sum(synthetic_tokens(m["content"]) for m in request.messages),
            output_tokens=synthetic_tokens(content), cached_input_tokens=0,
            actual_amount=Decimal("0"), currency="USD", raw_response={"test_fixture": True},
            request_sha256=request.request_sha256)


def write_candidate(kernel, owner, world, subject, predicate, budget, at):
    gateway = AgentGateway(kernel, {"local": LocalWriter()})
    job = RuntimeTaskService(kernel, gateway).prepare(owner, world, role_id="writer", task_kind="scene_draft",
        story_time=at, subject_ids=[subject], predicates=[predicate], assignment="把唯一已提供的事实改写为一句记录。",
        adapter_id="local", max_amount="0.10", token_budget=100000, lease_seconds=600)
    req = {"schema_version":"2.1", "task_id":job.task["task_id"], "generation":job.task["generation"],
           "worker_id":job.worker_id, "input_ref":job.agent_input["input_ref"], "budget_id":budget,
           "call_key":"single-synthetic-writer-turn", "reserve_amount":"0.1", "token_reservation":100000}
    result = gateway.execute(job.principal, req)
    assert result.get("validation_status") == "VALIDATED_CANDIDATE" and result["execution_kind"] == "TEST_FIXTURE", result.get("reason")
    assert gateway.execute(job.principal, req) == result
    with kernel.db.uow(write=False) as repo:
        call = repo.one("call_attempts", {"attempt_id": result["attempt_id"]})
        task = repo.one("tasks", {"task_id": job.task["task_id"]})
        audit = kernel.artifact(repo, result["evidence_ref"], world["work_id"])["payload"]
        line = kernel.artifact(repo, result["output_ref"], world["work_id"], kind="AGENT_CANDIDATE")["payload"]["text"]
        counts = audit["usage"]
        assert task["state"] == "RESULT_STORED" and call["state"] == "SUCCEEDED"
        assert call["actual_amount"] == 0 and call["actual_tokens"] == counts["input_tokens"] + counts["output_tokens"]
    return {"text":line, "task_id":job.task["task_id"], "generation":job.task["generation"], "world_ref":world,
            "attempt_id":result["attempt_id"], "output_ref":result["output_ref"], "evidence_ref":result["evidence_ref"],
            "input_tokens":counts["input_tokens"], "output_tokens":counts["output_tokens"]}


def seed_setup(kernel, registration):
    f = load_fixture(kernel, work_id=str(uuid5(NAMESPACE_URL, registration['seed'])))
    wr, owner, ids = f["world_ref"], f["principal"], f["ids"]
    budget = kernel.tasks.create_budget(wr["work_id"], "5")
    old, prepared = {}, {}
    start = perf_counter()
    for case in registration["cases"]:
        label, subject, pred = case["scenario"], ids[case["entity"]], case["predicate"]
        old[label] = write_candidate(kernel, owner, wr, subject, pred, budget, point(1))
        # Same three proposal/read pairs as p55_semantic_comparison.sample.
        task = create_bound_task(kernel, owner, wr, point(3), [subject], [pred])
        WorldQueryService(kernel).query(owner, {"schema_version":"2.1", "task_id":task["task_id"],
            "generation":task["generation"], "read_contract":{"revision":{"mode":"FIXED","world_ref":wr},
            "process_policy":"ALLOW_PARTIAL","index_policy":"AUTHORITATIVE","max_wait_ms":0},
            "knowledge_cut":{"plane":"WORLD"}, "subject_time":{"kind":"POINT","at":point(3)},
            "query":{"kind":"STATE","subject_ids":[subject],"predicates":[pred]},"page_size":20,"cursor":None})
        if label == "same_object":
            kind = "GUARD_LEAVE"
            args = {"guard_id":ids["guard1"], "region_id":ids["north"], "at":point(3)}
        else:
            kind = "RESOURCE_TRANSFER"
            args = {"from_account":ids["warehouse"], "to_account":ids["relief"],
                    "resource_id":ids["grain"], "quantity":"1", "unit":"bag", "at":point(3)}
        prepared[label] = prepare_command(kernel, owner, wr, kind, args, task=task, mode="DEPENDENCY_GUARDED")
    change = run_command(kernel, owner, wr, "SET_RELATION", {"subject_id":ids["guard1"], "predicate":"on_duty",
                         "object_value":{"kind":"BOOLEAN","value":False}, "at":point(2)})
    common_seconds = perf_counter() - start
    assert old["same_object"]["text"] == old["causal_read_other_write"]["text"] == "守卫仍在岗。"
    assert old["independent"]["text"] == "登记的剑主为 " + ids["smith"] + "。"
    return wr, owner, ids, budget, old, prepared, change, common_seconds


@contextmanager
def clone(template, base_objects, parent):
    name = "s9_writer_" + uuid4().hex
    src = make_url(template)
    admin = create_engine(src.set(database="postgres"), isolation_level="AUTOCOMMIT")
    db = None
    try:
        with admin.connect() as conn:
            quoted = conn.dialect.identifier_preparer.quote_identifier(src.database)
            conn.exec_driver_sql(f'CREATE DATABASE "{name}" TEMPLATE {quoted}')
        objects = parent / name
        shutil.copytree(base_objects, objects)
        db = Database(src.set(database=name).render_as_string(hide_password=False), pool_size=2, max_overflow=0)
        yield Kernel(db, ObjectStore(objects))
    finally:
        if db is not None:
            db.close()
        with admin.connect() as conn:
            conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.dispose()


def run_arm(k, fixture, registration, arm, trial):
    wr, owner, ids, budget, old, prepared, change, common_seconds = fixture
    observations, calls = [], []
    start = perf_counter()
    for case in registration["cases"]:
        label, pred = case["scenario"], case["predicate"]
        tic = perf_counter()
        with k.db.uow(write=False) as repo:
            changed = k._proposal(repo, owner, change["request"])
            invalid = guard(k, repo, wr, owner, prepared[label], changed, arm)
        decision = perf_counter() - tic
        if invalid:
            selected = write_candidate(k, owner, change["world_ref"], ids[case["entity"]], pred, budget,
                                       point(2 if pred == "on_duty" else 1))
            calls.append(selected)
        else:
            selected = old[label]
        # Gold is independent of the selected policy and its runtime writer.
        expected = case["post_change_text"].replace("{smith}", ids["smith"])
        with k.db.uow(write=False) as repo:
            k.authorize(repo, owner, wr["work_id"], "READ", wr["branch_id"])
            delivered = k.artifact(repo, selected["output_ref"], wr["work_id"], kind="AGENT_CANDIDATE")["payload"]["text"]
        valid = delivered == expected
        observations.append({"scenario":label, "oracle_affected":case["oracle_affected"], "invalidated":invalid,
            "decision_seconds":decision, "useful_output":valid, "selected_ref":selected["output_ref"],
            "retained_original":selected["output_ref"] == old[label]["output_ref"],
            "expected_text_sha256":digest(expected), "actual_text_sha256":digest(selected["text"])})
    elapsed = perf_counter() - start
    attempts = list(old.values()) + calls
    with k.db.uow(write=False) as repo:
        assert k.head(repo, wr["work_id"], wr["branch_id"]) == change["world_ref"]
        ledger = repo.many("call_attempts", {"budget_id":budget})
        assert len(ledger) == len(attempts) and all(r["state"] == "SUCCEEDED" for r in ledger)
        assert {r["attempt_id"] for r in ledger} == {r["attempt_id"] for r in attempts}
    useful = sum(o["useful_output"] for o in observations)
    total = common_seconds + elapsed
    return {"arm":arm, "trial":trial, "observations":observations, "rework_calls":len(calls),
        "logical_outputs":len(observations), "useful_outputs":useful, "false_positive_reworks":sum(o["invalidated"] and not o["oracle_affected"] for o in observations),
        "false_negative_retentions":sum(not o["invalidated"] and o["oracle_affected"] for o in observations),
        "common_elapsed_seconds":common_seconds, "post_change_elapsed_seconds":elapsed, "business_elapsed_seconds":total,
        "useful_outputs_per_business_second":useful/total, "useful_outputs_per_post_change_second":useful/elapsed,
        "synthetic_input_tokens":sum(a["input_tokens"] for a in attempts), "synthetic_output_tokens":sum(a["output_tokens"] for a in attempts),
        "synthetic_rework_tokens":sum(a["input_tokens"]+a["output_tokens"] for a in calls),
        "attempts":[{key:a[key] for key in ("task_id","generation","attempt_id","output_ref","evidence_ref","input_tokens","output_tokens")} for a in attempts],
        "supplier_calls":0, "supplier_tokens":None, "invoiced_cost":None}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--registration", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    reg = json.loads(args.registration.read_text())
    rows = []
    with TemporaryDirectory() as tmp:
        parent = Path(tmp)
        original = parent / "objects"
        with isolated_kernel(os.environ["NF_TEST_DATABASE_URL"], original) as (k, url):
            fixture = seed_setup(k, reg)
            k.db.close()
            for trial, order in enumerate(reg["trial_orders"], 1):
                for arm in order:
                    with clone(url, original, parent) as subject:
                        rows.append(run_arm(subject, fixture, reg, arm, trial))
    result = {"physical_common_writer_calls":3, "physical_rework_calls":sum(row["rework_calls"] for row in rows),
              "external_model_requests":0, "real_provider_tokens":None, "actual_invoice":None,
              "source_sha":reg["source_sha"], "seed_sha256":digest([fixture[0],fixture[6]["world_ref"],fixture[4]]),
              "initial_world":fixture[0], "changed_world":fixture[6]["world_ref"], "rows":rows,
              "registration_sha256":hashlib.sha256(args.registration.read_bytes()).hexdigest(),
              "initial_writer_receipts":fixture[4], "world_writes_after_common_seed":0, "production_acceptance":"NOT_EVALUATED"}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    for arm in "ABCD":
        group = [r for r in rows if r["arm"]==arm]
        print(arm, json.dumps({k:sum(r[k] for r in group) for k in ("logical_outputs","useful_outputs","rework_calls","business_elapsed_seconds","synthetic_input_tokens","synthetic_output_tokens","synthetic_rework_tokens")}), flush=True)


if __name__ == "__main__":
    main()
