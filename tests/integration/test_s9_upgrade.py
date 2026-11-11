"""The published S9 checkpoint has live, immutable receipts that 0015 must retain."""
from pathlib import Path
import subprocess
import sys

import pytest
from historical_release import release_database, run_release
from sqlalchemy import text

from novel_factory.application.authorization import Principal
from novel_factory.application.conflict_resolution import ConflictResolutionService
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.application.semantic_dependencies import check_task_dependencies, revalidate_task
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.recovery import database_snapshot

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


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
