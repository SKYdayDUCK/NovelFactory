"""Create one author-configured book and its offline, unsubmitted production request."""
import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from bootstrap_p5_m0 import save

from novel_factory.application.budget_limits import SharedBudgetService
from novel_factory.application.kernel import Kernel, require
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.release import ReleaseService
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.infrastructure.native_files import file_lock
from novel_factory.infrastructure.private_files import prepare_private_directory, read_private_bytes


def _id():
    return str(uuid4())


def _time(tick, phase=0):
    return {"tick": tick, "phase": phase}


def _seed(kernel, book, work_id):
    """Use the native kernel for a new world and native accepted PLAN inputs."""
    actor, place = _id(), _id()
    start, end = _time(1), _time(1, 1)
    interval = {"start": start, "end": end, "bounds": "[)"}
    entities = {uid: {"entity_id": uid, "entity_kind": kind, "name": name, "created_at": start}
                for uid, kind, name in ((actor, "CHARACTER", book["protagonist"]), (place, "REGION", book["place"]))}
    relation = {"subject_id": actor, "predicate": "located_in", "object_value": {"kind": "ENTITY", "entity_id": place},
                "valid_time": {"start": start, "end": None, "bounds": "[)"}}
    created = kernel.bootstrap_work(book["title"], {"entities": entities, "relations": [relation], "resources": [],
                                   "cognitions": [], "lifecycle": {}}, frontiers={uid: end for uid in entities}, work_id=work_id)
    owner, wr = created["principal"], created["world_ref"]
    plans = PlanningService(kernel)
    def contract(brief):
        volume = _id()
        body = {"schema_version": "2.0", "volume_id": volume, "contract_revision_id": _id(), "input_world_ref": wr,
                "creative_brief": brief, "focus_subject_ids": [actor, place], "narrative_intents": [brief],
                "protected_commitments": [], "open_choices": ["人物选择随正式事件演进"],
                "completion_or_stop_conditions": ["完成获准章节或预算耗尽"],
                "budget": {"max_semantic_calls": 2 * len(book["chapters"]) + 1, "max_candidate_branches": 1},
                "upstream_literary_refs": [], "dependency_fingerprint": fingerprint(wr)}
        return volume, plans.save_volume_contract(owner, body, volume)
    volume, current = contract(book["volume_one"])
    _, future = contract(book["volume_two"])
    nodes = [{"node_id": _id(), "goal": chapter, "status": "VIABLE", "target_time": {"kind": "EXACT", "at": start},
              "subject_ids": [actor], "hard_preconditions": [], "risk_notes": ["未发生的情节仍为计划"],
              "failure_exit": "保留未决选择", "fulfillment_event_refs": []} for chapter in book["chapters"]]
    plan = {"schema_version": "2.1", "plan_id": _id(), "plan_revision_id": _id(), "volume_id": volume,
            "input_world_ref": wr, "volume_contract_ref": current, "prior_plan_ref": None, "nodes": nodes,
            "published_prefix_node_ids": [], "dependencies": [], "decision": {"reason": "采用作者在本次配置中提供的细纲",
            "considered_options": [{"option_id": _id(), "summary": book["volume_one"], "disposition": "SELECTED",
                                    "reason": "作者设定"}]}}
    plan_ref = plans.save_plan(owner, plan, "first-outline")
    plans.adopt_plan(owner, wr, plan_ref, None, "select-first-outline")
    slots = [{"scene_id": _id(), "story_interval": interval, "reader_cut_ordinal": idx,
              "required_predecessor_scene_ids": []} for idx, _ in enumerate(nodes)]
    narrative_id = _id()
    batch = ReleaseService(kernel).create_writing_batch(owner, {"schema_version": "2.2", "world_ref": wr,
        "volume_id": volume, "plan_ref": plan_ref, "style_refs": [], "narrative_revision_id": narrative_id,
        "arrangement_version": "author-v1", "scene_slots": slots}, "first-writing-batch")
    chapters = []
    for node, slot in zip(nodes, slots, strict=True):
        card = {"schema_version": "2.0", "scene_id": slot["scene_id"], "input_world_ref": wr, "story_interval": interval,
                "event_refs": [], "entry_commitments": [], "exit_commitments": [], "pov_actor_id": actor,
                "narrator_mode": "limited", "narrative_revision_id": narrative_id, "arrangement_version": "author-v1",
                "reader_cut_ordinal": slot["reader_cut_ordinal"], "allowed_invention": ["不改变持久世界状态的感官细节"],
                "style_refs": [], "dependency_fingerprint": fingerprint([wr, plan_ref, slot])}
        ref = NarrativeService(kernel).save_scene(owner, {"schema_version": "2.2", "scene_contract": card,
            "plan_ref": plan_ref, "plan_node_ids": [node["node_id"]], "batch_ref": batch, "prior_scene_ref": None,
            "required_event_refs": [], "required_predecessor_scene_ids": [], "entry_checks": [], "exit_checks": [],
            "allowed_disclosures": [], "literary_purpose": node["goal"]}, "scene:" + slot["scene_id"])
        chapters.append({"chapter_id": _id(), "scenes": [{"scene_ref": ref, "handoff_refs": []}]})
    limits = book["budget"]
    budget_id = kernel.tasks.create_budget(work_id, limits["total_amount"], currency=limits["currency"])
    SharedBudgetService(kernel).set_limit(owner, work_id, wr["branch_id"], budget_id, limits["total_tokens"])
    pipeline = {"world_ref": wr, "series_id": _id(), "volume_id": volume, "chapters": chapters, "future": [
        {"candidate_id": _id(), "distance": 1, "volume_contract_ref": future, "assumptions": [
         {"kind": "LITERARY_COMMITMENT", "node_id": nodes[-1]["node_id"]}]}], "story_time": end,
        "subject_ids": [actor, place], "predicates": ["located_in"], "routes": book["routes"],
        "budget_id": budget_id, "reserve_amount": limits["reserve_amount"], "token_reservation": limits["token_reservation"],
        "limits": {"draft": 1, "review": 1, "plan": 1, "lookahead": 1, "review_backlog": 1, "window_goals": 4},
        "deadline_seconds": 86400, "idempotency_key": "initial-production:" + volume}
    access = {"work_id": work_id, "branch_id": wr["branch_id"], "token": created["token"]}
    return access, pipeline


def provision(db, objects, book, private):
    require(set(book) == {"title", "protagonist", "place", "volume_one", "chapters", "volume_two", "routes", "budget"}
            and all(isinstance(book[k], str) and bool(book[k].strip()) for k in
                    ("title", "protagonist", "place", "volume_one", "volume_two"))
            and isinstance(book["chapters"], list) and 1 <= len(book["chapters"]) <= 16
            and all(isinstance(c, str) and c.strip() for c in book["chapters"])
            and isinstance(book["routes"], dict) and set(book["routes"]) == {"draft", "review", "plan"}
            and all(isinstance(v, str) and v for v in book["routes"].values())
            and isinstance(book["budget"], dict) and set(book["budget"]) == {
                "currency", "total_amount", "reserve_amount", "total_tokens", "token_reservation"},
            "INVALID_ARGUMENT", "Check the book, chapter, route and budget fields")
    marker_path = private / "project.json"
    digest = fingerprint(book)
    if not marker_path.exists():
        require(not any(path.name != ".lock" for path in private.iterdir()), "INVALID_ARGUMENT", "Use a fresh project directory")
        save(private, marker_path.name, {"work_id": _id(), "book_hash": digest, "objects": str(objects.root),
                                         "database": db.engine.url.render_as_string(hide_password=True)})
    marker = json.loads(marker_path.read_text())
    require(marker["book_hash"] == digest and marker["objects"] == str(objects.root)
            and marker["database"] == db.engine.url.render_as_string(hide_password=True),
            "INVALID_ARGUMENT", "The directory belongs to a different book or installation")
    work = marker["work_id"]
    with objects.lock(), db.uow() as repo:
        # An interrupted run either committed the entire work plus access files or can be repeated here.
        kernel = Kernel(db, objects).in_transaction(repo)
        if repo.one("works", {"work_id": work}):
            access = json.loads(read_private_bytes(private / "supervisor.json", 16384))
            kernel.authorize(repo, kernel.authenticate(access["token"]), work, "ADMIN", access["branch_id"])
            pipeline = json.loads((private / "pipeline.json").read_text())
            require(pipeline["world_ref"]["work_id"] == work, "VIEW_DENIED", "Saved request belongs to another work")
            return {"status": "EXISTS", "world_ref": pipeline["world_ref"], "budget_id": pipeline["budget_id"]}
        access, pipeline = _seed(kernel, book, work)
        save(private, "control.json", {"token": access["token"], "object_root": str(objects.root),
                                     "database_url": db.engine.url.render_as_string(hide_password=False),
                                     "binding": {k: access[k] for k in ("work_id", "branch_id")}})
        save(private, "supervisor.json", access)
        save(private, "pipeline.json", pipeline)
        return {"status": "CREATED", "world_ref": pipeline["world_ref"], "budget_id": pipeline["budget_id"],
                "model_requests": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", required=True, type=Path)
    parser.add_argument("--private-dir", required=True, type=Path)
    args = parser.parse_args()
    directory = args.private_dir
    prepare_private_directory(directory)
    db = Database()
    try:
        book = json.loads(args.book.read_text(encoding="utf-8"))
        with file_lock(directory / ".lock"):
            print(json.dumps(provision(db, ObjectStore(os.environ.get("NF_OBJECT_ROOT", "var/objects")), book, directory)))
    except DomainError as error:
        parser.exit(2, error.code + "\n")
    finally:
        db.close()


if __name__ == "__main__":
    main()
