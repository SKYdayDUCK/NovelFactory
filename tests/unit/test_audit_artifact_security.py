"""P1 audit: an earlier ContextBundle never acts as a reusable grant."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from uuid import uuid4

import pytest

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import Kernel
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError


def uid():
    return str(uuid4())


def at(tick):
    return {"tick": tick, "phase": 0}


class MemoryRepo:
    def __init__(self, rows):
        self.rows = rows

    def one(self, table, where, lock=None):
        return next((deepcopy(row) for row in self.rows.get(table, ())
                     if all(row.get(key) == value for key, value in where.items())), None)

    def many(self, table, where):
        return [deepcopy(row) for row in self.rows.get(table, ())
                if all(row.get(key) == value for key, value in where.items())]

    @staticmethod
    def now():
        return datetime.now(timezone.utc)


class MemoryDB:
    def __init__(self, rows):
        self.rows = rows

    @contextmanager
    def uow(self, **_):
        yield MemoryRepo(self.rows)


class MemoryObjects:
    def read(self, _):
        return b"confidential future cognition"


def scenario():
    work, branch, revision, principal_id, grant_id = [uid() for _ in range(5)]
    old_task, current_task, old_context, current_context = [uid() for _ in range(4)]
    ref = {"artifact_id": uid(), "artifact_revision_id": uid(),
           "sha256": sha256(b"confidential future cognition").hexdigest()}
    wr = {"work_id": work, "branch_id": branch, "world_revision_id": revision}
    grant = {"grant_id": grant_id, "principal_id": principal_id, "work_id": work,
             "active": True, "capabilities": ["READ", "COMPILE"], "planes": ["BELIEF"],
             "role_id": "character_actor", "actor_id": uid(), "max_story_time": at(50),
             "branch_ids": [branch], "epoch": 0}
    now = datetime.now(timezone.utc)
    rows = {"works": [{"work_id": work, "policy_version": "nf-policy-1"}],
            "branches": [{"branch_id": branch, "work_id": work, "head_revision_id": revision}],
            "revisions": [{**wr, "snapshot": {}, "state_hash": fingerprint({}), "branch_seq": 0}],
            "grants": [grant],
            "artifacts": [{"work_id": work, **ref, "kind": "EVIDENCE"}],
            "tasks": [], "task_inputs": [], "contexts": []}
    for task_id, bundle_id, story_tick, generation, knowledge_tick in (
        (old_task, old_context, 40, 0, 35),
        (current_task, current_context, 20, 1, 15),
    ):
        spec = {"world_ref": wr, "story_time": at(story_tick), "policy_version": "nf-policy-1",
                "task_id": task_id, "generation": generation}
        cut = {"plane": "BELIEF", "actor_id": grant["actor_id"], "known_at": at(knowledge_tick)}
        read_cut = {"world_ref": wr, "knowledge_cut": cut,
                    "policy_version": "nf-policy-1", "permission_epoch": 0}
        rows["tasks"].append({"task_id": task_id, "work_id": work, "branch_id": branch,
                              "principal_id": principal_id, "grant_id": grant_id,
                              "generation": generation, "state": "RUNNING", "deadline": now + timedelta(hours=1),
                              "lease_expiry": now + timedelta(hours=1), "context_bundle_id": bundle_id})
        rows["task_inputs"].append({"task_id": task_id, "generation": generation,
                                    "spec": spec, "input_hash": fingerprint(spec)})
        rows["contexts"].append({"context_bundle_id": bundle_id, "work_id": work,
                                 "task_id": task_id, "generation": generation,
                                 "principal_id": principal_id, "grant_id": grant_id,
                                 "read_cut": read_cut,
                                 "payload": {"task_id": task_id, "generation": generation,
                                             "world_ref": wr, "story_time": at(story_tick),
                                             "role_id": grant["role_id"], "actor_id": grant["actor_id"],
                                             "policy_version": "nf-policy-1",
                                             "permitted_evidence_refs": ([{"kind": "ARTIFACT", "artifact_ref": ref}]
                                                                         if task_id == old_task else [])}})
    kernel = object.__new__(Kernel)
    kernel.db, kernel.objects = MemoryDB(rows), MemoryObjects()
    return kernel, Principal(grant_id, principal_id), work, ref, wr, rows, old_task, current_task, old_context, current_context


def call(kernel, principal, work, ref, wr, task, context, *, generation, story_tick, known_tick, actor):
    return kernel.get_artifact(
        principal, work, ref, task_id=task, generation=generation,
        context_bundle_id=context, world_ref=wr, story_time=at(story_tick),
        knowledge_cut={"plane": "BELIEF", "actor_id": actor, "known_at": at(known_tick)})


def test_unqualified_context_from_prior_task_must_not_authorize_artifact():
    kernel, principal, work, ref, wr, rows, old, current, old_ctx, current_ctx = scenario()
    # Regression reproduction: the original implementation grants this solely
    # because *any* context exists for the same grant.
    with pytest.raises(DomainError) as rejected:
        kernel.get_artifact(principal, work, ref)
    assert rejected.value.code == "VIEW_DENIED"


def test_current_task_may_not_borrow_old_task_context():
    kernel, principal, work, ref, wr, rows, old, current, old_ctx, current_ctx = scenario()
    actor = rows["grants"][0]["actor_id"]
    with pytest.raises(DomainError) as rejected:
        call(kernel, principal, work, ref, wr, current, old_ctx, generation=1,
             story_tick=20, known_tick=15, actor=actor)
    assert rejected.value.code == "VIEW_DENIED"


@pytest.fixture
def material_receipts(monkeypatch):
    """Observe the SQL receipt boundary after the real authorization and fence checks."""
    rows = []
    def record(repo, task, generation, revision, ref, cut, story_time, head):
        rows.append((task, generation, revision["world_revision_id"], ref, cut, story_time, head))
    monkeypatch.setattr("novel_factory.application.semantic_dependencies.record_material_read", record)
    return rows


def test_cut_and_story_time_cannot_be_swapped_within_same_context(material_receipts):
    kernel, principal, work, ref, wr, rows, old, current, old_ctx, current_ctx = scenario()
    actor = rows["grants"][0]["actor_id"]
    assert call(kernel, principal, work, ref, wr, old, old_ctx, generation=0,
                story_tick=40, known_tick=35, actor=actor) == b"confidential future cognition"
    for story_tick, known_tick in [(20, 15), (40, 15), (20, 35)]:
        with pytest.raises(DomainError) as rejected:
            call(kernel, principal, work, ref, wr, old, old_ctx, generation=0,
                 story_tick=story_tick, known_tick=known_tick, actor=actor)
        assert rejected.value.code == "VIEW_DENIED"
    assert material_receipts == [(old, 0, wr["world_revision_id"], ref,
        {"plane": "BELIEF", "actor_id": actor, "known_at": at(35)}, at(40), wr["world_revision_id"])]


def test_task_fence_revocation_and_grant_epoch_invalidate_evidence(material_receipts):
    kernel, principal, work, ref, wr, rows, old, current, old_ctx, current_ctx = scenario()
    actor = rows["grants"][0]["actor_id"]
    def access():
        return call(kernel, principal, work, ref, wr, old, old_ctx, generation=0,
                    story_tick=40, known_tick=35, actor=actor)
    assert access() == b"confidential future cognition"
    rows["tasks"][0]["state"] = "CANCELLED"
    with pytest.raises(DomainError): access()
    rows["tasks"][0]["state"] = "RUNNING"
    rows["tasks"][0]["generation"] += 1
    with pytest.raises(DomainError): access()
    rows["tasks"][0]["generation"] -= 1
    rows["grants"][0]["epoch"] += 1
    with pytest.raises(DomainError): access()
    rows["grants"][0]["active"] = False
    with pytest.raises(DomainError): access()
    assert len(material_receipts) == 1
