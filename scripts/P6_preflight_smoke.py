"""Check the P6 preflight with private, disposable synthetic inputs and no provider calls."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path
from uuid import uuid4


def uid():
    return str(uuid4())


def artifact():
    return {"artifact_id": uid(), "artifact_revision_id": uid(), "sha256": "b" * 64}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nf-checkout", type=Path, required=True)
    checkout = parser.parse_args().nf_checkout.resolve()
    def git(ref):
        return subprocess.check_output(["git", "-C", str(checkout), "rev-parse", ref], text=True).strip()
    endpoint = "https://p6-no-outbound.invalid/v1/chat/completions"
    env = {**os.environ, "NF_MODEL_ENDPOINT": endpoint, "NF_MODEL_NAME": "offline-fixture",
           "NF_MODEL_API_KEY": "P6-SYNTHETIC-ONLY", "NF_MODEL_MAX_OUTPUT_TOKENS": "24",
           "NF_MODEL_TIMEOUT_SECONDS": "60"}
    limits = dict(authorization_ref="synthetic-only", provider_endpoint=endpoint, model_name="offline-fixture",
                  currency="USD", total_amount="0.50", max_amount_per_attempt="0.02",
                  total_tokens=5000, max_output_tokens_per_attempt=24)
    experiment = dict(nf_source_sha=git("HEAD"), skills_git_tree=git("HEAD:runtime/skills"),
                      material_seed=0, target_scene_count=12, accepted_plan_ref=artifact(),
                      preregistered_conflict_labels=artifact(), reader_pseudonyms=["a", "b"], authorized_limits=limits)
    actors = [uid() for _ in range(3)]
    scene = dict(world_ref={key: uid() for key in ("work_id", "branch_id", "world_revision_id")},
                 story_time=dict(tick=31, phase=0), scope_ids=[uid()], volume_id=uid(), scene_limit=2,
                 participants=[dict(actor_id=a) for a in actors], subject_id=actors[0], destination_id=uid(),
                 budget_id=uid(), adapter_id="external", reserve_amount="0.010", token_reservation=250,
                 idempotency_key="offline-fixture", following_scenes=[dict(node_id=uid(), process_id=uid())])
    script = Path(__file__).with_name("P6_preflight.py")
    def run(label, exp, registration, errors=(), receipt=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, value in (("experiment", exp), ("scene", registration)):
                (root / name).write_text(json.dumps(value), encoding="utf-8")
            extra = []
            if receipt is not None:
                (root / "ledger").write_text(json.dumps(receipt), encoding="utf-8")
                extra = ["--budget-receipt", str(root / "ledger")]
            result = subprocess.run([sys.executable, str(script), "--nf-checkout", str(checkout),
                                     "--experiment", str(root / "experiment"), "--scene-file", str(root / "scene"), *extra],
                                    env=env, text=True, capture_output=True, check=False)
            record = json.loads(result.stdout)
            assert result.returncode == (2 if errors else 0)
            assert set(errors) <= set(record["missing_or_inconsistent"])
            assert record["production_model_requests_sent"] == record["http_provider_requests_sent"] == 0
            assert record["scenes_dispatched"] == record["db_connections_opened"] == 0
            assert endpoint not in result.stdout and env["NF_MODEL_API_KEY"] not in result.stdout
            assert record["actual_cost"] is None
            print(label, record["local_input_checks"])
    run("synthetic configuration", experiment, scene)
    ledger = dict(work_id=scene["world_ref"]["work_id"], budget_id=scene["budget_id"], currency="USD",
                  limit_amount="0.500000", reserved_amount="0.03", spent_amount="0.05",
                  total_tokens=5000, reserved_tokens=100, spent_tokens=700, available_tokens=4200,
                  unquantified_attempts=0)
    run("saved budget receipt", experiment, scene, receipt=ledger)
    run("unresolved usage", experiment, scene, ("shared_budget.receipt",),
        receipt={**ledger, "unquantified_attempts": 1})
    run("spent token allowance", experiment, scene, ("shared_budget.receipt",),
        receipt={**ledger, "spent_tokens": 4800, "available_tokens": 100})
    bad = deepcopy(experiment)
    bad["authorized_limits"].update(total_amount="0.005", total_tokens=200, model_name="mismatch",
                                    max_output_tokens_per_attempt=8)
    run("authorization mismatch", bad, scene, ("authorized_limits.amounts", "authorized_limits.tokens",
                                             "authorized_limits.model_name", "authorized_limits.output_vs_environment"))
    bad_scene = deepcopy(scene)
    bad_scene["participants"][1]["actor_id"] = actors[0]
    run("duplicate participant", experiment, bad_scene, ("first_scene.register_scene_arguments",))
    bad_scene = deepcopy(scene)
    bad_scene["world_ref"]["world_revision_id"] = None
    run("missing revision", experiment, bad_scene, ("first_scene.world_ref",))
    bad = {**experiment, "nf_source_sha": "0" * 40, "skills_git_tree": "0" * 40}
    run("unexpected source", bad, scene, ("nf_source_sha", "registered_runtime_skill_tree"))


if __name__ == "__main__":
    main()
