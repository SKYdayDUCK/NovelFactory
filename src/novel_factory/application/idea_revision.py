"""Replay an audited ordinary revision that was historically bound to an ending patch schema."""

from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.canonical import fingerprint, strict_json_loads

from .kernel import require
from .production_pipeline import _artifact


def full_design_contract(profile, split_models):
    if not split_models:
        return "FirstWorldDesign"
    return "EnvironmentalOpeningWorldDesign" if profile["environment_processes"] else "OpeningWorldDesign"


def replay_revision_patch(kernel, repo, wr, designer, evidence, patch, intake):
    work = wr["work_id"]
    require(designer.get("review_ref") and not designer.get("revision_purpose")
            and evidence["input_ref"] == designer["input_ref"]
            and evidence["role_id"] == "world_designer" and evidence["world_ref"] == wr
            and evidence["outcome"] == "VALIDATED_CANDIDATE" and evidence["result_status"] == "SUCCEEDED",
            "INVALID_EVIDENCE", "Expected an ordinary reviewed design revision with a stored candidate")
    binding = kernel.artifact(repo, designer["input_ref"], work, kind="AGENT_INPUT")["payload"]
    require(binding["output_contract"] == {"name": "ShortEndingPlan", "version": "production_profile"},
            "INVALID_EVIDENCE", "Historical revision has a different frozen patch schema")
    packet = strict_json_loads(binding["assignment"])
    source = kernel.artifact(repo, designer["candidate_ref"], work)["payload"]
    original = kernel.artifact(repo, evidence["output_ref"], work, kind="AGENT_CANDIDATE")
    review = kernel.artifact(repo, designer["review_ref"], work, kind="AGENT_CANDIDATE")["payload"]
    require(source == packet["candidate"] and designer["candidate_ref"] == packet["candidate_ref"]
            and designer["review_ref"] == packet["review_ref"] and packet["creative_input"] == intake["creative_input"]
            and review["issues"] and original["payload"] == patch and fingerprint(patch) == original["sha256"],
            "INVALID_EVIDENCE", "Frozen ordinary revision does not match its source, review or patch")
    patch = validate_contract("ShortEndingPlan", patch, "production_profile")
    name = full_design_contract(intake["profile"], intake.get("model_routes"))
    validate_contract(name, source, "production_profile")
    return validate_contract(name, {**source, **patch}, "production_profile")


def derive_revision_patch(kernel, repo, owner, wr, designer, evidence, patch, intake):
    candidate = replay_revision_patch(kernel, repo, wr, designer, evidence, patch, intake)
    request = {
        "input_ref": designer["input_ref"], "prior_design_ref": designer["candidate_ref"],
        "review_ref": designer["review_ref"], "response_ref": evidence["response_ref"],
        "patch_ref": evidence["output_ref"],
    }
    attempt = evidence["attempt_id"]
    proof = kernel._prior(repo, owner, wr["work_id"], "P6_COMPLETE_REVISION_PATCH", attempt, request)
    if proof is None:
        selected = _artifact(kernel, repo, wr["work_id"], owner, "COMPLETE_REVISION_CANDIDATE", candidate)
        proof = {**request, "attempt_id": attempt, "output_ref": selected}
        kernel._receipt(repo, owner, wr, "P6_COMPLETE_REVISION_PATCH", attempt, request, proof)
    return {**evidence, "output_ref": proof["output_ref"], "revision_patch": proof}, candidate
