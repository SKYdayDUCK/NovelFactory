from concurrent.futures import ThreadPoolExecutor

import pytest

from novel_factory.application.message_sources import MessageSourceService
from novel_factory.application.narrative import NarrativeService
from novel_factory.domain.errors import DomainError
from novel_factory.domain.propositions import encode_proposition
from novel_factory.fixtures import load_fixture, point, run_command
from test_narrative import arrange, assertion, draft_for, extract
from test_release import runtime_grant

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_message_versions_preserve_belief_evidence_across_candidate_branches(kernel_env):
    kernel = kernel_env["kernel"]
    f = load_fixture(kernel)
    admin, wr, ids = f["principal"], f["world_ref"], f["ids"]
    service = MessageSourceService(kernel)
    def save(_):
        return service.save_version(admin, wr, "守卫会在黎明离岗。", "letter")
    with ThreadPoolExecutor(2) as pool:
        first, retry = list(pool.map(save, range(2)))
    assert first == retry
    original = first["source_ref"]
    evidence = {"kind": "ARTIFACT", "artifact_ref": original}
    statement = encode_proposition(ids["guard1"], "on_duty", {"kind": "BOOLEAN", "value": False})
    belief = run_command(kernel, admin, wr, "RECORD_COGNITION", {
        "actor_id": ids["protagonist"], "statement": statement, "record_kind": "BELIEF",
        "epistemic_mode": "HEARD", "evidence_refs": [evidence], "at": point(2)})
    wr2 = belief["world_ref"]
    fork = kernel.fork(admin, wr2, "CANDIDATE", "alternate-messenger")
    versions = [service.save_version(admin, branch, body, key, prior_ref=original)
                for branch, body, key in [(wr2, "守卫将继续值守。", "corrected"),
                                          (fork, "信使承认伪造了命令。", "alternative")]]
    refs = [original, *(v["source_ref"] for v in versions)]
    assert len({r["artifact_id"] for r in refs}) == 1
    assert len({r["artifact_revision_id"] for r in refs}) == 3
    for v in versions:
        assert v["prior_ref"] == original
    assert kernel.get_artifact(admin, wr["work_id"], original).decode() == "守卫会在黎明离岗。"
    with kernel.db.uow(write=False) as repo:
        assert kernel.revision(repo, wr)["snapshot"]["cognitions"] == []
        for branch in (wr2, fork):
            record, = kernel.revision(repo, branch)["snapshot"]["cognitions"]
            assert record["statement"] == statement and record["evidence_refs"] == [evidence]
            assert kernel.head(repo, wr["work_id"], branch["branch_id"]) == branch
    with pytest.raises(DomainError):
        service.save_version(runtime_grant(kernel, f, "writer"), wr2, "替换", "unauthorized", prior_ref=original)
    with pytest.raises(DomainError) as mismatch:
        service.save_version(admin, wr, "不同的原文", "letter")
    assert mismatch.value.code == "IDEMPOTENCY_MISMATCH"


def test_finished_unpublished_draft_fenced_when_its_explicit_world_read_changes(kernel_env):
    from novel_factory.application.queries import WorldQueryService
    k = kernel_env["kernel"]
    f = arrange(k)
    admin, wr, ids = f["principal"], f["world_ref"], f["ids"]
    item = draft_for(k, f, "他把准备好的运粮清单压在桌上。")
    task = item["task"]
    # The writer checks the premise independently of the literal text. A surface
    # extraction alone cannot rediscover that the draft assumed this holder.
    request = {"schema_version": "2.1", "task_id": task["task_id"], "generation": task["generation"],
        "read_contract": {"revision": {"mode": "FIXED", "world_ref": wr}, "process_policy": "ALLOW_PARTIAL",
                          "index_policy": "AUTHORITATIVE", "max_wait_ms": 0},
        "knowledge_cut": {"plane": "WORLD"}, "subject_time": {"kind": "POINT", "at": point(2, 1)},
        "query": {"kind": "STATE", "subject_ids": [ids["sword"]], "predicates": ["possessed_by"]},
        "page_size": 16, "cursor": None}
    WorldQueryService(k).query(item["writer"], request)
    s = NarrativeService(k)
    captured = extract(k, f, item, [assertion(item["text"])])
    report = s.reconcile(admin, item["draft_ref"], captured["extraction_ref"], wr, "clear")
    assert report["status"] == "CLEAR"
    change = run_command(k, admin, wr, "TRANSFER_ITEM", {"item_id": ids["sword"],
        "from_id": ids["smith"], "to_id": ids["friend"], "transfer_kind": "GIFT", "at": point(1, 2)})
    with pytest.raises(DomainError) as stale:
        s.accept_narrative(admin, item["draft_ref"], report["reconciliation_ref"], wr, "publish")
    assert stale.value.code in {"DEPENDENCY_CONFLICT", "STALE_PRECONDITION", "SEMANTIC_DEPENDENCY_CONFLICT"}
    with k.db.uow(write=False) as repo:
        assert k.head(repo, wr["work_id"]) == change["world_ref"]
        assert s._draft(repo, item["draft_ref"], wr["work_id"])["text_ref"]["sha256"]


def test_delivered_draft_selected_as_letter_retains_its_carrier_dependencies(kernel_env):
    from novel_factory.application.semantic_dependencies import check_task_dependencies
    from novel_factory.domain.canonical import canonical_bytes
    from test_release import task_for
    from test_s9_dependencies import set_fact, world

    kernel = kernel_env["kernel"]
    f = arrange(kernel, world(kernel))
    admin, base = f["principal"], f["world_ref"]
    draft = draft_for(kernel, f, "密信里写的是明早启程。")["draft_ref"]
    narrative = NarrativeService(kernel)
    with kernel.db.uow(write=False) as repo:
        body_ref = narrative._draft(repo, draft, base["work_id"])["text_ref"]
    named = set_fact(kernel, f, base, f["letter"], "message_content_ref",
                     {"kind": "STRING", "value": canonical_bytes(body_ref).decode()}, 2)
    reviewer = runtime_grant(kernel, f, "reviewer")
    reader = task_for(kernel, f, reviewer, [draft], "extract", wr=named, story_time=point(20))
    pair = (reader["task_id"], reader["generation"])
    narrative.bind_reader_task(admin, *pair, [draft], "reader-scope")
    delivery = narrative.read_text(reviewer, *pair, draft)
    assert delivery["text"] == "密信里写的是明早启程。" and delivery["text_ref"] == body_ref
    with kernel.db.uow(write=False) as repo:
        check_task_dependencies(repo, *pair)
    set_fact(kernel, f, named, f["letter"], "message_authentic", {"kind": "BOOLEAN", "value": False}, 5)
    with kernel.db.uow(write=False) as repo, pytest.raises(DomainError) as stale:
        check_task_dependencies(repo, *pair)
    assert stale.value.code == "DEPENDENCY_CONFLICT"
