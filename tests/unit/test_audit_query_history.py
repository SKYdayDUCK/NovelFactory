"""Large-selection query regression: snapshot-only operations must not select event payloads."""
from contextlib import contextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from novel_factory.application.queries import WorldQueryService


def uid():
    return str(uuid4())


def fixture(query_kind="STATE", *, batch=False, size=9000):
    work, branch, rev = uid(), uid(), uid()
    wr = {"work_id": work, "branch_id": branch, "world_revision_id": rev}
    event_ids = [uid() for _ in range(size)]
    selection = {uid(): ev_id for ev_id in event_ids}
    grant = {"grant_id": uid(), "principal_id": uid(), "role_id": "control_operator",
             "capabilities": ["ADMIN", "READ", "COMPILE"], "planes": ["WORLD"],
             "actor_id": None, "max_story_time": {"tick": 100, "phase": 0}, "epoch": 0}
    task = {"work_id": work, "branch_id": branch}
    spec = {"policy_version": "nf-policy-1", "world_ref": wr, "story_time": {"tick": 20, "phase": 0}}
    revision = {**wr, "selection": selection, "snapshot": {"entities": {}, "relations": []},
                "interpretation_ref": {"artifact_id": uid(), "artifact_revision_id": uid(), "sha256": "f" * 64}}
    query = {"kind": query_kind, "subject_ids": [uid()], "predicates": ["legal_owner"]}
    if query_kind == "TIMELINE":
        query = {"kind": "TIMELINE", "subject_ids": [uid()],
                 "entry_kinds": ["STATE_ASSERTION"], "time_axis": "VALID", "include_inactive": True}
    request = {"task_id": uid(), "generation": 0, "knowledge_cut": {"plane": "WORLD"},
               "read_contract": {}, "subject_time": {"kind": "POINT", "at": {"tick": 20, "phase": 0}},
               "query": query}
    if batch:
        request.pop("query")
        request["items"] = [{"query": query}]
    class Repo:
        def __init__(self):
            self.event_calls = 0
            self.requested = 0
        def one(self, table, _):
            assert table == "branches", table
            return {"kind": "CANONICAL"}
        def many(self, table, where):
            if table == "index_watermarks": return []
            assert table == "events", table
            self.event_calls += 1
            self.requested += len(where["event_version_id"])
            return []
        @staticmethod
        def now(): return datetime.now(timezone.utc)
    repo = Repo()
    class DB:
        @contextmanager
        def uow(self, **_): yield repo
    class Kernel:
        db = DB()
        def authorize(self, *_args, **_kw): return grant
        def revision(self, *_args): return revision
        query_revision = revision
    service = WorldQueryService(Kernel())
    service._task = lambda *_a, **_k: (task, grant, spec)
    service._selector = lambda *_a, **_k: wr
    principal = SimpleNamespace(grant_id=grant["grant_id"], principal_id=grant["principal_id"])
    return service, principal, request, repo, event_ids


def test_large_state_and_relation_timeline_do_not_fetch_events():
    for kind, batch in [("STATE", False), ("STATE", True), ("TIMELINE", False), ("TIMELINE", True)]:
        service, principal, request, repo, _ = fixture(kind, batch=batch)
        packet = service._sample(principal, request)
        assert packet["events"] == []
        assert repo.event_calls == 0, (kind, batch)


def test_fact_timeline_still_fetches_selected_versions_exactly_once():
    for batch in (False, True):
        service, principal, request, repo, expected_ids = fixture("TIMELINE", batch=batch, size=150)
        fact_query = request["items"][0]["query"] if batch else request["query"]
        fact_query["entry_kinds"].append("FACT")
        packet = service._sample(principal, request)
        assert packet["events"] == []  # mocked DB returns no payload; selection itself remains immutable
        assert repo.event_calls == 1 and repo.requested == len(expected_ids)
