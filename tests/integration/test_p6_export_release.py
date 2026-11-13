"""Use the frozen six-scene TEST_FIXTURE to exercise the author's local handoff."""

from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.private_files import create_private_file, read_private_bytes
from novel_factory.orchestration.short_volume import ShortVolumeRunner
from novel_factory.orchestration.short_volume_fixture import TEXTS

pytestmark = [pytest.mark.integration, pytest.mark.postgres]
ROOT = Path(__file__).resolve().parents[2]


def run_cli(script, *args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / script), *map(str, args)],
                          cwd=ROOT, capture_output=True, text=True, timeout=90)


def binding(path, env, token, wr):
    config = {"token": token, "database_url": env["url"], "object_root": str(env["store"].root),
              "binding": {"work_id": wr["work_id"], "branch_id": wr["branch_id"]}}
    with os.fdopen(create_private_file(path), "w") as stream:
        json.dump(config, stream)
    return path


@pytest.fixture(scope="module")
def volume(kernel_env, tmp_path_factory):
    directory = tmp_path_factory.mktemp("frozen-short-volume")
    runner = ShortVolumeRunner(kernel_env["kernel"], output_dir=directory)
    result = runner.run()
    assert result["execution_kind"] == "TEST_FIXTURE" and result["real_model_call_count"] == 0
    ref_file = directory / "frozen-ref.json"
    ref_file.write_text(json.dumps(result["release"]["release_ref"]))
    return runner, result, ref_file


def export(binding_path, ref_path, output):
    return run_cli("p6_export_release.py", "--binding", binding_path,
                   "--release-ref", ref_path, "--output", output)


def test_export_frozen_prose_scoped_log_and_blank_reader_forms(kernel_env, volume, tmp_path):
    runner, result, ref = volume
    kernel, wr = kernel_env["kernel"], result["world_ref"]
    author = kernel.issue_grant(runner.principal, wr["work_id"], role_id="volume_director",
                                capabilities=["READ", "PLAN_READ"], planes=["WORLD", "PLAN"],
                                branch_ids=[wr["branch_id"]], max_story_time=point(100))
    author_config = binding(tmp_path / "author.json", kernel_env, author["token"], wr)
    out = tmp_path / "release.txt"
    response = export(author_config, ref, out)
    assert response.returncode == 0, response.stderr
    content = out.read_bytes()
    assert content == "\n\n".join(TEXTS).encode() == (runner.output_dir / "release.txt").read_bytes()
    exported = json.loads(response.stdout)
    digest = sha256(content).hexdigest()
    assert exported["release_ref"] == result["release"]["release_ref"]
    assert exported["manifest_sha256"] == result["release"]["manifest"]["manifest_sha256"]
    assert exported["text_sha256"] == digest and exported["scene_count"] == len(TEXTS) == 6
    assert exported["bytes"] == len(content) and exported["external_delivery"] == "NOT_REQUESTED"
    assert read_private_bytes(out, len(content)) == content
    out.write_bytes(b"existing author file")
    retry = export(author_config, ref, out)
    assert retry.returncode != 0 and retry.stderr.strip() == "OUTPUT_EXISTS"
    assert out.read_bytes() == b"existing author file"

    trace_id = exported["release_ref"]["artifact_revision_id"]
    args = ("trace", "--trace-id", trace_id)
    denied = run_cli("p55_statistics.py", "--binding", author_config, *args)
    assert denied.returncode != 0 and denied.stderr.strip() == "VIEW_DENIED" and not denied.stdout
    auditor = kernel.issue_grant(runner.principal, wr["work_id"], role_id="control_operator",
                                 capabilities=["ADMIN"], branch_ids=[wr["branch_id"]])
    audit_config = binding(tmp_path / "audit.json", kernel_env, auditor["token"], wr)
    traced = run_cli("p55_statistics.py", "--binding", audit_config, *args)
    assert traced.returncode == 0, traced.stderr
    rows = json.loads(traced.stdout)
    assert any(row["event_type"] == "selection.release_freeze" and row["trace_id"] == trace_id
               and row["work_id"] == wr["work_id"] and row["branch_id"] == wr["branch_id"] for row in rows)

    stage = tmp_path / "handoff"
    stage.mkdir()
    (stage / "release.txt").write_bytes(content)
    (stage / "trace.json").write_text(traced.stdout, encoding="utf-8")
    template = (ROOT / "reports/handoff/P6_READER_FORM.md").read_text(encoding="utf-8")
    for label in ("A", "B"):
        form = template.replace("{{TEXT_SHA256}}", digest).replace("{{FORM_CODE}}", label)
        assert form.count("|  |  |  |") == 5
        (stage / f"reader-{label}.md").write_text(form, encoding="utf-8")
    receipt = {"execution_kind": result["execution_kind"], "real_model_calls": 0,
               "fixture_calls": result["model_fixture_call_count"], "fixture_cost": result["fixture_cost"],
               "literary_acceptance": result["literary_quality_status"], "human_reviews_received": 0,
               "author_status": result["author_status"], "export": exported,
               "trace_id": trace_id, "trace_event_types": [row["event_type"] for row in rows],
               "sha256": {p.name: sha256(p.read_bytes()).hexdigest() for p in sorted(stage.iterdir())}}
    (stage / "author-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    if target := os.environ.get("NF_P6_AUTHOR_EVIDENCE"):
        destination = Path(target)
        destination.mkdir(parents=True, exist_ok=True)
        assert not any(destination.iterdir()), "choose an empty evidence directory"
        for member in stage.iterdir():
            shutil.copyfile(member, destination / member.name)


def test_export_rejects_another_work_bound_branch_actor_and_revocation(kernel_env, volume, tmp_path):
    runner, result, ref = volume
    kernel, wr = kernel_env["kernel"], result["world_ref"]
    second = kernel.fork(runner.principal, wr, "CANDIDATE", "author-export-test")
    multi = kernel.issue_grant(runner.principal, wr["work_id"], role_id="volume_director",
                               branch_ids=[wr["branch_id"], second["branch_id"]],
                               capabilities=["READ", "PLAN_READ"], planes=["WORLD", "PLAN"],
                               max_story_time=point(100))
    actor = kernel.issue_grant(runner.principal, wr["work_id"], role_id="character_actor",
                               actor_id=runner.ids["protagonist"], capabilities=["READ"],
                               planes=["BELIEF"], max_story_time=point(100))
    other = load_fixture(kernel)
    for name, token, bound in (("other-work", other["token"], other["world_ref"]),
                               ("other-branch", multi["token"], second),
                               ("actor", actor["token"], wr)):
        config = binding(tmp_path / f"{name}.json", kernel_env, token, bound)
        out = tmp_path / f"{name}.txt"
        response = export(config, ref, out)
        assert response.returncode != 0 and response.stderr.strip() == "VIEW_DENIED"
        assert not response.stdout and not out.exists()
    config = binding(tmp_path / "revoked.json", kernel_env, multi["token"], wr)
    kernel.revoke_grant(runner.principal, multi["principal"].grant_id)
    out = tmp_path / "revoked.txt"
    response = export(config, ref, out)
    assert response.returncode != 0 and not response.stdout and not out.exists()
