"""Source releases retain frozen tasks without mixing old and new prompts."""
import json
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from novel_factory.application.creative_context import runtime_provenance
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.runtime_catalog import RuntimeCatalog

ROOT = Path(__file__).resolve().parents[2]
PREVIOUS_SOURCE = "e057e94aedcc674c3e1c4bb0c8cbf05755509c6f"


def git_source(commit, path):
    return subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=ROOT)


def release_manifest():
    return json.loads((ROOT / "runtime/provenance.json").read_text(encoding="utf-8"))


def blob_tree(commit):
    tree = {}
    output = subprocess.check_output(["git", "ls-tree", "-rz", commit, "--", "runtime"], cwd=ROOT)
    for entry in output.split(b"\0"):
        if entry:
            meta, path = entry.split(b"\t", 1)
            tree[path.decode()] = meta.split()[2].decode()
    return tree


def historical_catalog(tmp_path):
    paths = ("roles/catalog.json", "skills/catalog.json")
    for path in paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git_source(PREVIOUS_SOURCE, "runtime/" + path))
    for role in json.loads((tmp_path / paths[0]).read_text(encoding="utf-8"))["roles"]:
        path = "roles/" + role["contract_path"]
        (tmp_path / path).write_bytes(git_source(PREVIOUS_SOURCE, "runtime/" + path))
    for skill in json.loads((tmp_path / paths[1]).read_text(encoding="utf-8"))["skills"]:
        path = "skills/" + skill["path"]
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(git_source(PREVIOUS_SOURCE, "runtime/" + path))
    return RuntimeCatalog(tmp_path)


@pytest.mark.parametrize("role,mode,skill", [
    ("reviewer", "fact", "review-consistency"),
    ("evidence_maintainer", "assertion_extract", "maintain-memory"),
    ("chief_editor", "editorial_decision", "edit-volume"),
])
def test_new_and_historical_auditors_resolve_their_own_released_source(tmp_path, role, mode, skill):
    current = RuntimeCatalog().snapshot(role, mode, skill)["snapshot"]
    historic = historical_catalog(tmp_path).snapshot(role, mode, skill)["snapshot"]
    assert runtime_provenance(current)["source_commit"] == release_manifest()["source_commit"]
    old = runtime_provenance(historic)
    assert old["source_commit"] == PREVIOUS_SOURCE
    assert runtime_provenance(historic, old) == old
    # Changing task instructions does not expose executable tools to text calls.
    assert current["effective_model_tools"] == historic["effective_model_tools"] == []


def test_current_skill_cannot_be_paired_with_an_unreleased_historical_role(tmp_path):
    current = RuntimeCatalog().snapshot("reviewer", "fact", "review-consistency")["snapshot"]
    old = historical_catalog(tmp_path).snapshot("reviewer", "fact", "review-consistency")["snapshot"]
    mixed = {**old, "skill_instructions": current["skill_instructions"]}
    with pytest.raises(DomainError) as error:
        runtime_provenance(mixed)
    assert error.value.code == "RUNTIME_UNAVAILABLE"


def test_previous_release_record_is_preserved_in_full():
    old = json.loads(git_source(PREVIOUS_SOURCE, "runtime/provenance.json"))
    expected = {key: old[key] for key in ("roles", "skills", "source_commit")}
    current = release_manifest()
    assert expected in current["previous"]
    assert all(row in current["previous"] for row in old.get("previous", []))
    previous = next(row for row in current["previous"] if row["source_commit"] == PREVIOUS_SOURCE)
    tree = blob_tree(PREVIOUS_SOURCE)
    paths = json.loads((ROOT / "workshop/contracts/nf-catalog.json").read_text(encoding="utf-8"))
    for family in ("roles", "skills"):
        assert previous[family] == {name: tree[item["path"]] for name, item in paths[family].items()}


def test_workshop_and_nf_pin_the_same_committed_catalog_and_instruction_bytes():
    nf = release_manifest()
    workshop = json.loads((ROOT / "workshop/contracts/nf-catalog.json").read_text(encoding="utf-8"))
    assert nf["source_commit"] == workshop["nf_commit"]
    commit = nf["source_commit"]
    tree = blob_tree(commit)
    for path, digest in workshop["catalogs"].items():
        assert sha256(git_source(commit, path)).hexdigest() == digest
    for family in ("roles", "skills"):
        assert workshop[family].keys() == nf[family].keys()
        for name, item in workshop[family].items():
            assert sha256(git_source(commit, item["path"])).hexdigest() == item["sha256"]
            assert tree[item["path"]] == nf[family][name]
