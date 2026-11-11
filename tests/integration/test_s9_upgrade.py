"""The published S9 checkpoint has live, immutable receipts that 0015 must retain."""
from pathlib import Path
import subprocess
import sys

import pytest
from tests.fixtures.historical_release import release_database, run_release
from sqlalchemy import text

from novel_factory.application.authorization import Principal
from novel_factory.application.conflict_resolution import ConflictResolutionService
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.application.semantic_dependencies import check_task_dependencies, revalidate_task
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.recovery import database_snapshot

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def assert_downgrade_preserves_database(db, env, root, current, target, reason):
    with db.uow(write=False) as repo:
        assert repo.conn.scalar(text("SELECT version_num FROM alembic_version")) == current
        before = database_snapshot(repo.conn)
    downgrade = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", target], cwd=root,
        env=env, capture_output=True, text=True,
    )
    assert downgrade.returncode != 0
    assert f"psycopg.errors.RaiseException: {reason}" in downgrade.stderr
    with db.uow(write=False) as repo:
        assert repo.conn.scalar(text("SELECT version_num FROM alembic_version")) == current
        assert database_snapshot(repo.conn) == before


def test_published_checkpoint_receipts_survive_and_partial_reads_fail_closed(release_database, tmp_path):
    db, env = release_database
    k = Kernel(db, ObjectStore(tmp_path / "objects"))
    repo_root = Path(__file__).resolve().parents[2]
    old = run_release(repo_root, tmp_path, "01f640c96e2ebc985f8687ac5d0bbe2afd552fb9", """
import json, os
from sqlalchemy import text
from novel_factory.application.conflict_resolution import ConflictResolutionService
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.fixtures import create_bound_task, load_fixture, point, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
db = Database()
try:
    kernel = Kernel(db, ObjectStore(os.environ["NF_OBJECT_ROOT"]))
    f = load_fixture(kernel)
    base, owner, ids = f["world_ref"], f["principal"], f["ids"]
    changed_reader = create_bound_task(kernel, owner, base, point(1), [ids["sword"]], ["lifecycle"])
    still_open = create_bound_task(kernel, owner, base, point(1), [ids["sword"]], ["lifecycle"])
    other = create_bound_task(kernel, owner, base, point(1), [ids["guard1"]], ["on_duty"])
    changed = run_command(kernel, owner, base, "DESTROY_ITEM",
                          {"item_id": ids["sword"], "at": point(40)})["world_ref"]
    svc = ConflictResolutionService(kernel)
    args = (owner, base["work_id"], base["branch_id"])
    while svc.scan_one(*args) is not None:
        pass
    with db.uow() as r:
        check_task_dependencies(r, other["task_id"], other["generation"])
        case = str(r.conn.execute(text("SELECT case_id FROM nf_conflict_cases WHERE task_id=:t"),
                                  {"t": changed_reader["task_id"]}).scalar_one())
        opened = str(r.conn.execute(text("SELECT case_id FROM nf_conflict_cases WHERE task_id=:t"),
                                    {"t": still_open["task_id"]}).scalar_one())
    receipt = svc.resolve(*args, case)
    print(json.dumps({"owner": {"grant_id": owner.grant_id, "principal_id": owner.principal_id},
                      "world_ref": changed, "other": [other["task_id"], other["generation"]],
                      "case": case, "open_case": opened, "receipt": receipt}))
finally:
    db.close()
""", env)

    with k.db.uow(write=False) as r:
        before = database_snapshot(r.conn)
        old_reads = r.conn.execute(text(
            "SELECT to_jsonb(r) FROM nf_semantic_reads r ORDER BY observation_id")).scalars().all()
    assert old_reads and old["receipt"]["outcome"] == "RECOMPUTE_QUEUED"
    assert before["nf_conflict_resolutions"]["rows"] > 0
    assert_downgrade_preserves_database(
        db, env, repo_root, "0014_p55_resolution", "0013_p55_semantic",
        "Archive conflict receipts and drain semantic scans before downgrade",
    )
    with db.uow(write=False) as r:
        assert r.conn.scalar(text("""SELECT EXISTS (SELECT FROM pg_trigger
            WHERE tgrelid='nf_outbox'::regclass AND tgname='nf_semantic_scan_enqueue'
              AND tgenabled='O')"""))
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=repo_root,
                   env=env, check=True, capture_output=True)
    head = old["world_ref"]
    with k.db.uow() as r:
        after = database_snapshot(r.conn)
        for table in before.keys() - {"alembic_version", "nf_semantic_reads"}:
            assert before[table] == after[table], table
        reads = r.conn.execute(text(
            "SELECT to_jsonb(r) - 'created_at' FROM nf_semantic_reads r ORDER BY observation_id")).scalars().all()
        assert old_reads == reads
        assert not r.conn.scalar(text("SELECT count(*) FROM nf_semantic_reads WHERE created_at IS NOT NULL"))
        assert r.conn.scalar(text(
            "SELECT branch_seq FROM nf_semantic_upgrade_cuts WHERE branch_id=:b"),
            {"b": head["branch_id"]}) == r.conn.scalar(text(
            "SELECT max(branch_seq) FROM nf_semantic_changes WHERE branch_id=:b"), {"b": head["branch_id"]})
        with pytest.raises(DomainError) as blocked:
            check_task_dependencies(r, *old["other"])
        assert blocked.value.code == "DEPENDENCY_CONFLICT"
        assert not revalidate_task(r, k, *old["other"], head)
    restored = ConflictResolutionService(k).resolve(Principal(**old["owner"]), head["work_id"],
                                                    head["branch_id"], old["case"])
    assert restored == old["receipt"]
    svc = ConflictResolutionService(k)
    args = (Principal(**old["owner"]), head["work_id"], head["branch_id"])
    resumed = svc.resolve_one(*args)
    assert resumed["case_id"] == old["open_case"]
    assert svc.resolve(*args, old["open_case"]) == resumed


def test_observed_scopes_upgrade_fences_existing_graph_and_material_candidates(release_database, tmp_path):
    """Two candidates allowed by 0015 are still pending when the operator upgrades."""
    db, env = release_database
    root = Path(__file__).resolve().parents[2]
    old = run_release(root, tmp_path, "ef371a32c6c8df5fea41b96702c9903132b85737", """
import json, os
from uuid import uuid4
from sqlalchemy import text
from novel_factory.application.conflict_resolution import ConflictResolutionService
from novel_factory.application.queries import WorldQueryService
from novel_factory.application.semantic_dependencies import check_task_dependencies
from novel_factory.fixtures import create_bound_task, entity_value, fixture_definition, point, prepare_command, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
db = Database()
try:
    k = Kernel(db, ObjectStore(os.environ["NF_OBJECT_ROOT"]))
    f = fixture_definition("white_bridge")
    ids = f["ids"]
    carrier = str(uuid4())
    f["genesis"]["entities"][carrier] = {"entity_id":carrier, "entity_kind":"KNOWLEDGE_CARRIER",
                                        "name":"密信", "created_at":point(1)}
    f["frontiers"][carrier] = point(1,1)
    for x in ("north","smith"):
        f["frontiers"][ids[x]] = point(12)
    f["genesis"]["relations"].append({"subject_id":ids["north"], "predicate":"blocked_by",
        "object_value":entity_value(ids["sword"]), "valid_time":{"start":point(1),"end":None,"bounds":"[)"}})
    world = k.bootstrap_work("0015 readers", f["genesis"], frontiers=f["frontiers"])
    user, base = world["principal"], world["world_ref"]
    material = k.put_artifact(user, base["work_id"], b"material", "TEXT")
    named = run_command(k,user,base,"SET_RELATION",{"subject_id":carrier,"predicate":"message_content_ref",
        "object_value":{"kind":"STRING","value":json.dumps(material,indent=2)},"at":point(2)})["world_ref"]
    graph, reader = [create_bound_task(k,user,named,point(20),[ids["guard2"]],["lifecycle"]) for _ in range(2)]
    query = {"schema_version":"2.1","task_id":graph["task_id"],"generation":graph["generation"],
        "read_contract":{"revision":{"mode":"FIXED","world_ref":named},"process_policy":"ALLOW_PARTIAL",
                         "index_policy":"AUTHORITATIVE","max_wait_ms":0},
        "knowledge_cut":{"plane":"WORLD"},"subject_time":{"kind":"POINT","at":point(10)},
        "query":{"kind":"NEIGHBORHOOD","seed_ids":[ids["north"]],"predicates":["blocked_by","possessed_by"],
                 "max_hops":3,"max_edges":30},"page_size":30,"cursor":None}
    assert WorldQueryService(k).query(user,query)["payload"]["coverage"] == "PARTIAL"
    assert k.get_artifact(user,named["work_id"],material,task_id=reader["task_id"],generation=reader["generation"],
        context_bundle_id=reader["compiled"]["compiled"]["bundle"]["context_bundle_id"],world_ref=named,
        story_time=point(20),knowledge_cut={"plane":"WORLD"}) == b"material"
    proposals = [prepare_command(k,user,named,"RESOURCE_TRANSFER",{"from_account":ids["warehouse"],
       "to_account":ids["relief"],"resource_id":ids["grain"],"quantity":"1","unit":"bag","at":point(20)},
       mode="DEPENDENCY_GUARDED",task=t)["request"] for t in (graph,reader)]
    progress = run_command(k,user,named,"ADVANCE_SCOPE",{"scope_id":ids["sword"],"target":point(11)})["world_ref"]
    changed = run_command(k,user,progress,"SET_RELATION",{"subject_id":carrier,"predicate":"message_authentic",
        "object_value":{"kind":"BOOLEAN","value":False},"at":point(5)})["world_ref"]
    svc = ConflictResolutionService(k)
    while svc.scan_one(user,named["work_id"],named["branch_id"]) is not None:
        pass
    # Execute the complete P5.5 acceptance transaction on a savepoint, then roll it
    # back so both identical input certificates remain outstanding at migration time.
    with db.uow() as r:
        for task, proposal in zip((graph,reader),proposals):
            check_task_dependencies(r, task["task_id"], task["generation"])
            with r.conn.begin_nested() as hypothetical:
                assert k.in_transaction(r).accept(user,proposal)["parent_world_ref"] == changed
                hypothetical.rollback()
    print(json.dumps({"owner":{"grant_id":user.grant_id,"principal_id":user.principal_id},
        "head":changed,"scope":named, "candidates":proposals,"tasks":[graph["task_id"],reader["task_id"]]}))
finally:
    db.close()
""", env)
    with db.uow(write=False) as r:
        before = database_snapshot(r.conn)
        assert before["nf_semantic_scans"]["rows"] == 0
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root,
                   env=env, check=True, capture_output=True)
    kernel = Kernel(db, ObjectStore(tmp_path / "objects"))
    user, scope = Principal(**old["owner"]), old["scope"]
    with db.uow(write=False) as r:
        after = database_snapshot(r.conn)
        for table in before.keys() - {"alembic_version", "nf_semantic_scans"}:
            assert after[table] == before[table], table
        assert after["nf_semantic_scans"]["rows"] >= 2
        assert after["nf_semantic_read_supplements"]["rows"] >= 2
    assert_downgrade_preserves_database(
        db, env, root, "0016_p55_observed_scopes", "0015_p55_revalidation",
        "Retain or archive semantic index corrections before downgrade",
    )
    with db.uow(write=False) as r:
        assert r.conn.scalar(text("SELECT indisvalid FROM pg_index WHERE indexrelid="
                                  "to_regclass('nf_sem_window_candidate')"))
    for request in old["candidates"]:
        with pytest.raises(DomainError) as rejected:
            kernel.accept(user, request)
        assert rejected.value.code == "DEPENDENCY_CONFLICT"
    resolver = ConflictResolutionService(kernel)
    while resolver.scan_one(user, scope["work_id"], scope["branch_id"]) is not None:
        pass
    with db.uow(write=False) as r:
        affected = r.conn.scalars(text("SELECT DISTINCT task_id::text FROM nf_conflict_cases")).all()
        assert set(old["tasks"]).issubset(affected)
        assert kernel.head(r, scope["work_id"], scope["branch_id"]) == old["head"]
