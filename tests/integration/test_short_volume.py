"""P2-07 complete application integration, with explicit deterministic prose."""
import json
import os
from pathlib import Path

import pytest

from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError
from novel_factory.domain.world import resource_at, state_at
from novel_factory.fixtures import point
from novel_factory.orchestration.short_volume import ShortVolumeRunner
from novel_factory.orchestration.short_volume_fixture import REVISION_NEEDLE, TEXTS

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def test_six_scene_volume_uses_real_handoffs_and_retains_every_independent_gate(kernel_env, tmp_path):
    runner = ShortVolumeRunner(kernel_env["kernel"], output_dir=tmp_path / "volume")
    result = runner.run()
    assert result["execution_kind"] == "TEST_FIXTURE"
    assert result["engineering_status"] == "PASS"
    assert result["real_model_status"] == "BLOCKED_ENVIRONMENT"
    assert result["author_status"] == "PENDING_AUTHOR"
    assert result["literary_quality_status"] == "NOT_EVALUATED"
    assert result["scene_count"] == 6 and result["real_model_call_count"] == 0
    assert result["model_fixture_call_count"] == 15
    assert len(result["replan"]["report"]["affected_node_ids"]) == 2
    assert len(result["replan"]["report"]["preserved_node_ids"]) == 4
    assert result["world_events_before_writing"] == result["world_events_after_release"] > 0
    state = runner.world_snapshot()
    with kernel_env["kernel"].db.uow(write=False) as repo:
        initial = kernel_env["kernel"].revision(repo, result["initial_world_ref"])["snapshot"]
    assert state_at(initial, runner.ids["sword"], "lifecycle", point(1))["data"]["value"] == "ACTIVE"
    assert resource_at(initial, runner.ids["warehouse"], runner.ids["grain"], "bag", point(1))["quantity"] == "100"
    assert resource_at(state, runner.ids["warehouse"], runner.ids["grain"], "bag", point(7))["quantity"] == "70"
    assert resource_at(state, runner.ids["relief"], runner.ids["grain"], "bag", point(7))["quantity"] == "30"
    assert state_at(state, runner.ids["sword"], "lifecycle", point(7))["data"]["value"] == "DESTROYED"
    relations = state_at(state, runner.ids["friend"], "member_of", point(7))["data"]
    assert [relation["object_value"] for relation in relations] == [{"kind": "ENTITY", "entity_id": runner.ids["sect"]}]
    old_belief = state_at(state, runner.ids["sword"], "possessed_by", point(3), actor_id=runner.ids["friend"], known_at=point(3))
    own_belief = state_at(state, runner.ids["sword"], "possessed_by", point(3), actor_id=runner.ids["protagonist"], known_at=point(3))
    assert old_belief["data"] != own_belief["data"]
    assert state_at(state, runner.ids["rival"], "located_in", point(4))["data"] == {"kind": "ENTITY", "entity_id": runner.ids["north"]}
    body = (tmp_path / "volume" / "release.txt").read_text()
    assert body == "\n\n".join(TEXTS)
    assert REVISION_NEEDLE not in body
    assert result["initial_draft_ref"] != result["final_binding"]["ordered_draft_refs"][0]
    stages = {entry["stage"] for entry in runner.trace}
    assert {"world_prepared", "initial_plan", "sealed_action", "adjudication", "local_replan",
            "scene_outlines", "draft", "narrative_accepted", "full_text_reviews", "volume_editing",
            "literary_memory", "internal_release"} <= stages
    drafts = [entry for entry in runner.trace if entry["stage"] == "draft"]
    assert len(drafts) == 7
    assert len({fingerprint(entry["world_ref"]) for entry in drafts}) == 1
    assert len(drafts[3]["handoff_refs"]) == 2
    checksums = json.loads((tmp_path / "volume" / "checksums.json").read_text())
    assert "short_volume.md" in checksums and "artifact_index.json" in checksums
    assert not any("token" in name or "credential" in name for name in checksums)
    contexts = json.loads((tmp_path / "volume" / "contexts.json").read_text())
    actors = {row["payload"]["actor_id"]: row["payload"] for row in contexts if row["payload"]["role_id"] == "character_actor"}
    assert actors[runner.ids["friend"]]["facts"] != actors[runner.ids["protagonist"]]["facts"]
    assert actors[runner.ids["rival"]]["facts"] == []


@pytest.mark.parametrize(("case", "text", "blocker"), [
    ("quantity_30_to_40", TEXTS[2].replace("三十袋", "四十袋"), "STATE_CONFLICT"),
    ("destroyed_to_intact", TEXTS[2].replace("铜汛印被压毁了", "铜汛印仍完好"), "STATE_CONFLICT"),
    ("unrecognised_new_fact", TEXTS[2] + "\n\n铜汛印焕然一新。", "UNRESOLVED_STATE"),
])
def test_text_driven_extraction_rejects_real_writer_candidates(kernel_env, tmp_path, case, text, blocker):
    destination = Path(os.environ.get("NF_P2_ACCEPTANCE_EVIDENCE_DIR", tmp_path)) / "short_volume_rejections" / case
    runner = ShortVolumeRunner(kernel_env["kernel"], output_dir=destination,
        fixture_text_overrides={2: text})
    runner.initialize(); runner.initial_plan(); runner.simulate(); runner.scenes()
    accepted = [runner.reconcile(runner.writer(index, [])) for index in range(2)]
    before = runner.world_snapshot()
    bad_draft = runner.writer(2, accepted)
    assert bad_draft["text"] == text  # Actual Gateway candidate, not a forged extraction input.
    with pytest.raises(DomainError) as error:
        runner.reconcile(bad_draft)
    assert error.value.code == "NARRATIVE_BLOCKED"
    rejected = runner.trace[-1]
    assert rejected["stage"] == "narrative_rejected"
    report = runner.artifact(rejected["reconciliation"]["reconciliation_ref"])
    assert report["status"] == "BLOCKED"
    assert blocker in {issue["code"] for issue in report["hard_blockers"]}
    assert runner.world_snapshot() == before
    with runner.kernel.db.uow(write=False) as repo:
        assert repo.one("selections", {"work_id": runner.wr["work_id"],
            "selection_kind": "NARRATIVE_SCENE:" + runner.scene_cards[2]["scene_contract"]["scene_id"]}) is None
    last_call = runner.calls[-1]
    evidence = {"execution_kind": "TEST_FIXTURE", "case": case, "expected_rejection": error.value.code,
        "world_unchanged": True, "draft": runner.artifact(bad_draft["draft_ref"]),
        "gateway_result": last_call, "candidate": runner.artifact(last_call["output_ref"]),
        "model_call_evidence": runner.artifact(last_call["evidence_ref"]),
        "rejected": rejected, "reconciliation_report": report}
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
