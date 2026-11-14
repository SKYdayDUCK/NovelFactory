"""Consecutive story intervals, formal coverage, immutable freeze revalidation.

Engineering scenes and review records exercise contracts, not literary quality.
"""
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from test_narrative import arrange, assertion, extract
from test_release import runtime_grant, task_for

from novel_factory.application import frozen_editing, rolling_production
from novel_factory.application.narrative import NarrativeService
from novel_factory.application.planning import PlanningService
from novel_factory.application.production_pipeline import _artifact
from novel_factory.application.release import ReleaseService
from novel_factory.application.world_coverage_recovery import prove_pure_coverage
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import point, run_command

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def uid():
    return str(uuid4())


def two_scenes(kernel, *, dependent=False):
    f = arrange(kernel)
    f.update(volume_id=f["plan"]["volume_id"], narrative_revision_id=uid())
    slots = [{"scene_id": uid(), "story_interval": {"start": point(i + 2), "end": point(i + 2, 1), "bounds": "[)"},
              "reader_cut_ordinal": i, "required_predecessor_scene_ids": []} for i in range(2)]
    if dependent:
        slots[1]["required_predecessor_scene_ids"] = [slots[0]["scene_id"]]
    release = ReleaseService(kernel)
    batch = release.create_writing_batch(f["principal"], {"schema_version": "2.2", "world_ref": f["world_ref"],
        "volume_id": f["volume_id"], "plan_ref": f["plan_ref"], "style_refs": [],
        "narrative_revision_id": f["narrative_revision_id"], "arrangement_version": "arrangement-1", "scene_slots": slots}, uid())
    refs, scenes = [], []
    for i, slot in enumerate(slots):
        contract = {"schema_version": "2.0", "scene_id": slot["scene_id"], "input_world_ref": f["world_ref"],
            "story_interval": slot["story_interval"], "event_refs": [], "entry_commitments": [], "exit_commitments": [],
            "pov_actor_id": f["ids"]["protagonist"], "narrator_mode": "limited", "narrative_revision_id": f["narrative_revision_id"],
            "arrangement_version": "arrangement-1", "reader_cut_ordinal": i, "allowed_invention": ["工程表层细节"],
            "style_refs": [], "dependency_fingerprint": f["plan_ref"]["sha256"]}
        scene = {"schema_version": "2.2", "scene_contract": contract, "plan_ref": f["plan_ref"],
            "plan_node_ids": [f["plan"]["nodes"][i]["node_id"]], "batch_ref": batch, "prior_scene_ref": None,
            "required_event_refs": [], "required_predecessor_scene_ids": slot["required_predecessor_scene_ids"],
            "entry_checks": [], "exit_checks": [], "allowed_disclosures": [], "literary_purpose": "逐场覆盖边界工程复现"}
        refs.append(NarrativeService(kernel).save_scene(f["principal"], scene, uid()))
        scenes.append(scene)
    assert scenes[0]["scene_contract"]["story_interval"] != scenes[1]["scene_contract"]["story_interval"]
    return {**f, "batch_ref": batch, "scene_refs": refs, "scenes": scenes}


def writer_task(kernel, f, i, parents=()):
    principal = runtime_grant(kernel, f, "writer")
    task = task_for(kernel, f, principal, [f["plan_ref"], f["batch_ref"], f["scene_refs"][i], *parents],
                    story_time=f["scenes"][i]["scene_contract"]["story_interval"]["end"])
    PlanningService(kernel).bind_task_for_controller(f["principal"], task["task_id"], task["generation"],
        f["plan_ref"], f["scenes"][i]["plan_node_ids"], uid())
    return principal, task


def write(kernel, f, i):
    principal, task = writer_task(kernel, f, i)
    ReleaseService(kernel).bind_writer_task(principal, task["task_id"], task["generation"], f["scene_refs"][i], [], uid())
    text = "第一场的窗纸响了一声。" if i == 0 else "第二场的窗纸又响了一声。"
    ref = NarrativeService(kernel).store_draft(principal, task["task_id"], task["generation"], f["scene_refs"][i], text, uid())
    return {"draft_ref": ref, "text": text, "scene_ref": f["scene_refs"][i], "scene": f["scenes"][i], "task": task}


def close_accept(kernel, f, item, current_wr):
    wr = run_command(kernel, f["principal"], current_wr, "SCHEDULE_ADVANCE", {
        "scope_ids": [f["ids"]["sword"], f["ids"]["protagonist"]],
        "target": item["scene"]["scene_contract"]["story_interval"]["end"]})["world_ref"]
    with kernel.db.uow(write=False) as repo:
        revision = kernel.revision(repo, wr)
        assert revision["frontiers"][f["ids"]["sword"]] == item["scene"]["scene_contract"]["story_interval"]["end"]
    extracted = extract(kernel, {**f, "world_ref": wr}, item, [assertion(item["text"])])
    service = NarrativeService(kernel)
    report = service.build_alignment(f["principal"], item["draft_ref"], extracted["extraction_ref"], wr, uid())
    assert report["status"] == "CLEAR"
    accepted = service.accept_narrative(f["principal"], item["draft_ref"], report["reconciliation_ref"], wr, uid(),
                                      alignment_ref=report["alignment_ref"])
    return {**item, **report, "acceptance_ref": accepted, "extraction_ref": extracted["extraction_ref"], "world_ref": wr}


def chapter_board(kernel, f, accepted):
    nodes, chapters = {}, []
    with kernel.db.uow() as repo:
        for item in accepted:
            sid = item["scene"]["scene_contract"]["scene_id"]
            chapter = uid()
            key = "draft:" + sid
            facts = _artifact(kernel, repo, f["world_ref"]["work_id"], f["principal"], "ROLLING_FACT_REPORT",
                              {"acceptance_ref": item["acceptance_ref"], "status": "CLEAR"})
            advice = _artifact(kernel, repo, f["world_ref"]["work_id"], f["principal"], "CHAPTER_READING_ADVICE",
                               {"draft_refs": [item["draft_ref"]]})
            nodes[key] = {"output_ref": item["draft_ref"], "scene_ref": item["scene_ref"],
                          "task_id": item["task"]["task_id"], "generation": item["task"]["generation"]}
            nodes["facts:" + sid] = {"state": "FACTS_ACCEPTED", "output_ref": facts}
            nodes["review:" + chapter] = {"state": "ADVISORY_REVIEW", "output_ref": advice, "parents": [key]}
            chapters.append({"chapter_id": chapter})
        grant = repo.one("grants", {"grant_id": f["principal"].grant_id})
        cursor = {"world_ref": f["world_ref"], "run_id": uid(), "series_id": uid(), "index": 0,
            "last_checkpoint": None, "frozen_chapters": 0, "target_chapters": 2,
            "authorization": {"grant_id": f["principal"].grant_id, "principal_id": f["principal"].principal_id,
                "epoch": grant["epoch"], "policy_version": repo.one("works", {"work_id": f["world_ref"]["work_id"]})["policy_version"],
                "deadline": (repo.now() + timedelta(hours=1)).isoformat()}}
    return {"world_ref": f["world_ref"], "volume_id": f["volume_id"], "nodes": nodes}, chapters, cursor


def test_second_chapter_handoff_uses_first_accepted_text_after_scene_coverage(kernel_env):
    kernel = kernel_env["kernel"]
    f = two_scenes(kernel, dependent=True)
    first = close_accept(kernel, f, write(kernel, f, 0), f["world_ref"])
    assert first["world_ref"] != f["world_ref"]
    release = ReleaseService(kernel)
    ready = release.ready_scenes(f["principal"], f["batch_ref"], [first["acceptance_ref"]])
    assert ready["ready_scene_ids"] == [f["scenes"][1]["scene_contract"]["scene_id"]]
    principal, task = writer_task(kernel, f, 1, [first["acceptance_ref"]])
    release.bind_writer_task(principal, task["task_id"], task["generation"], f["scene_refs"][1], [first["acceptance_ref"]], uid())
    text = "第二场的声音来自走廊。"
    ref = NarrativeService(kernel).store_draft(principal, task["task_id"], task["generation"], f["scene_refs"][1], text, uid(),
        handoff_refs=[first["acceptance_ref"]])
    second = close_accept(kernel, f, {"draft_ref": ref, "text": text, "scene_ref": f["scene_refs"][1],
        "scene": f["scenes"][1], "task": task}, first["world_ref"])
    assert second["world_ref"] != first["world_ref"]


def test_multi_chapter_window_revalidates_earlier_acceptance_at_current_head(kernel_env):
    kernel = kernel_env["kernel"]
    f = two_scenes(kernel)
    drafts = [write(kernel, f, i) for i in range(2)]
    first = close_accept(kernel, f, drafts[0], f["world_ref"])
    second = close_accept(kernel, f, drafts[1], first["world_ref"])
    assert first["world_ref"] != second["world_ref"]
    board, chapters, cursor = chapter_board(kernel, f, [first, second])
    with kernel.db.uow() as repo:
        rolling_production._freeze_window(kernel, repo, f["principal"], cursor, board,
                                         {"volume_id": f["volume_id"], "chapters": chapters})
        checkpoint = kernel.artifact(repo, cursor["last_checkpoint"], f["world_ref"]["work_id"])["payload"]
        assert cursor["frozen_chapters"] == 2
        accepted = [kernel.artifact(repo, scene["acceptance_ref"], f["world_ref"]["work_id"])["payload"]
                    for chapter in checkpoint["chapters"] for scene in chapter["scenes"]]
        assert all(a["world_ref"] == second["world_ref"] for a in accepted)
        assert [a["draft_ref"] for a in accepted] == [first["draft_ref"], second["draft_ref"]]
        assert kernel.artifact(repo, first["acceptance_ref"], f["world_ref"]["work_id"])["payload"]["world_ref"] == first["world_ref"]


def test_single_chapter_windows_get_immutable_checkpoint_overlay_before_editing(kernel_env):
    kernel = kernel_env["kernel"]
    f = two_scenes(kernel)
    drafts = [write(kernel, f, i) for i in range(2)]
    first = close_accept(kernel, f, drafts[0], f["world_ref"])
    board, first_chapters, cursor = chapter_board(kernel, f, [first])
    with kernel.db.uow() as repo:
        rolling_production._freeze_window(kernel, repo, f["principal"], cursor, board,
                                         {"volume_id": f["volume_id"], "chapters": first_chapters})
    old_checkpoint = cursor["last_checkpoint"]
    second = close_accept(kernel, f, drafts[1], first["world_ref"])
    second_board, second_chapters, _ = chapter_board(kernel, f, [second])
    cursor["index"] = 1
    windows = [{"volume_id": f["volume_id"], "chapters": chapters} for chapters in [first_chapters, second_chapters]]
    with kernel.db.uow() as repo:
        rolling_production._freeze_window(kernel, repo, f["principal"], cursor, second_board, windows[1])
        cursor.update(state="FROZEN", window_count=2, windows=[rolling_production._store(kernel, repo, f["principal"],
            f["world_ref"], "ROLLING_WINDOW", window) for window in windows])
        rolling_production._save(kernel, repo, f["principal"], cursor)
    work, branch = f["world_ref"]["work_id"], f["world_ref"]["branch_id"]
    state = frozen_editing.start(kernel, f["principal"], work, branch)
    assert state["status"] == "EDITING_ACTIVE"
    assert frozen_editing.start(kernel, f["principal"], work, branch)["status"] == "EDITING_ACTIVE"
    with kernel.db.uow(write=False) as repo:
        current = rolling_production._load(kernel, repo, work, branch)
        assert current["last_checkpoint"] != cursor["last_checkpoint"]
        scenes = rolling_production.frozen_volume_scenes(kernel, repo, current, f["volume_id"])
        renewed = scenes[0]["acceptance_ref"]
        assert renewed != first["acceptance_ref"]
        before = kernel.artifact(repo, first["acceptance_ref"], work)["payload"]
        after = kernel.artifact(repo, renewed, work)["payload"]
        assert before["draft_ref"] == after["draft_ref"] and before["text_ref"] == after["text_ref"]
        assert after["world_ref"] == second["world_ref"]
        assert kernel.artifact(repo, old_checkpoint, work)["payload"]["chapters"][0]["scenes"][0]["acceptance_ref"] == first["acceptance_ref"]
        assert kernel.head(repo, work, branch) == second["world_ref"]


@pytest.mark.parametrize("change", ["canonical", "branch", "non_schedule"])
def test_coverage_compatibility_refuses_semantic_or_nonformal_history(kernel_env, change):
    kernel = kernel_env["kernel"]
    f = two_scenes(kernel)
    first = close_accept(kernel, f, write(kernel, f, 0), f["world_ref"])
    if change == "canonical":
        target = run_command(kernel, f["principal"], first["world_ref"], "TRANSFER_ITEM", {
            "item_id": f["ids"]["sword"], "from_id": f["ids"]["smith"], "to_id": f["ids"]["protagonist"],
            "transfer_kind": "GIFT", "at": point(3)})["world_ref"]
    elif change == "branch":
        target = kernel.fork(f["principal"], first["world_ref"], "CANDIDATE", uid())
    else:
        target = run_command(kernel, f["principal"], first["world_ref"], "ADVANCE_SCOPE", {
            "scope_id": f["ids"]["sword"], "target": point(3, 1)})["world_ref"]
    with kernel.db.uow(write=False) as repo:
        with pytest.raises(DomainError) as error:
            prove_pure_coverage(kernel, repo, first["world_ref"], target)
        assert error.value.code == ("BRANCH_CHANGED" if change == "branch" else "NARRATIVE_ALIGNMENT_REQUIRED")


@pytest.mark.parametrize("unstable", ["PENDING", "UNKNOWN"])
def test_actual_old_mcp_uncertainty_cannot_be_promoted_to_clear_by_coverage(kernel_env, monkeypatch, unstable):
    from test_production_verification import coverage_fixture, drive_coverage

    from novel_factory.application.agent_gateway import AgentGateway
    from novel_factory.application.candidate_delivery import CandidateDelivery
    from novel_factory.application.production_pipeline import _load
    from novel_factory.application.review_mcp import ReviewMcpBroker
    from novel_factory.application.rolling_facts import revalidate_fact_reading
    from novel_factory.application.world_coverage_recovery import prove_review_compatibility

    kernel = kernel_env["kernel"]
    gateway, owner, wr, _, _, _ = coverage_fixture(kernel, monkeypatch, covered=True)
    original_call = ReviewMcpBroker.call

    async def uncertain_query(broker, request):
        if request["tool"] == "nf_query_batch":
            with kernel.db.uow(write=False) as repo:
                task = repo.one("tasks", {"task_id": broker.scope["task_id"]})
                board = _load(kernel, repo, wr["work_id"], wr["branch_id"])
            if unstable == "PENDING" and task["role_id"] == "evidence_maintainer":
                request = deepcopy(request)
                request["arguments"]["lookups"][0]["at"] = point(1, 1)
            elif unstable == "UNKNOWN" and task["task_kind"] == "epistemic":
                request = deepcopy(request)
                request["arguments"]["lookups"][0].update(plane="BELIEF", actor_id=board["subject_ids"][0], known_at=point(1))
        return await original_call(broker, request)

    monkeypatch.setattr(ReviewMcpBroker, "call", uncertain_query)
    board, cursor = drive_coverage(kernel, gateway, owner, wr)
    assert cursor["state"] == "FROZEN"
    facts = next(node for node in board["nodes"].values() if node.get("state") == "FACTS_ACCEPTED")
    with kernel.db.uow(write=False) as repo:
        head = kernel.head(repo, wr["work_id"], wr["branch_id"])
        report = kernel.artifact(repo, facts["output_ref"], wr["work_id"])["payload"]
        if unstable == "PENDING":
            extraction = kernel.artifact(repo, report["extraction_ref"], wr["work_id"])["payload"]
            reader = repo.one("tasks", {"task_id": extraction["task_id"]})
        else:
            reading = kernel.artifact(repo, report["knowledge_ref"], wr["work_id"])["payload"]
            reader = repo.one("tasks", {"task_id": reading["reader_task"]})
        evidence, _ = CandidateDelivery(kernel, AgentGateway(kernel, {})).audited_candidate(repo, reader, production_accounting=True)
    target = run_command(kernel, owner, head, "SCHEDULE_ADVANCE", {"scope_ids": board["subject_ids"], "target": point(3, 1)})["world_ref"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        repo.one("works", {"work_id": wr["work_id"]}, lock="update")
        before = len(repo.many("call_attempts", {}))
        with pytest.raises(DomainError) as direct:
            prove_review_compatibility(kernel, repo, reader, evidence, target)
        assert direct.value.code == "REVIEW_QUERY_REQUIRED"
        with pytest.raises(DomainError) as error:
            revalidate_fact_reading(kernel, repo, owner, facts["output_ref"], target)
        assert error.value.code in {"REVIEW_QUERY_REQUIRED", "DEPENDENCY_CONFLICT"}
        assert len(repo.many("call_attempts", {})) == before
