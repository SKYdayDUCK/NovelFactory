import json
from hashlib import sha256

import pytest

from novel_factory.application import knowledge_profiles as knowledge
from novel_factory.domain.errors import DomainError


@pytest.mark.unit
def test_public_knowledge_routing_and_negative_controls():
    get = knowledge.freeze_knowledge_profile
    modern = get("writer", "scene_draft", "现代")
    ancient = get("writer", "scene_draft", "古代")
    assert [item["id"] for item in modern["entries"]] == ["draft", "modern"]
    assert [item["id"] for item in ancient["entries"]] == ["draft", "ancient"]
    assert modern["profile_hash"] != ancient["profile_hash"]
    assert get("writer", "character_action", "古代")["entries"] == []
    assert get("control_operator", "scene_draft", "古代")["entries"] == []
    assert (
        get("writer", "scene_draft", "../../private")["entries"]
        == get("writer", "scene_draft", None)["entries"]
    )
    assert all("original" not in item for item in modern["entries"])


@pytest.mark.unit
def test_recovery_checks_full_source_hash_only_on_escalation(tmp_path, monkeypatch):
    root, catalog, roles = knowledge._directory()
    monkeypatch.setattr(knowledge, "_directory", lambda: (tmp_path, catalog, roles))
    (tmp_path / "methods.json").write_bytes((root / "methods.json").read_bytes())
    (tmp_path / "references").mkdir()
    original = (root / "references/draft.md").read_bytes()
    path = tmp_path / "references/draft.md"
    path.write_bytes(original)
    select = knowledge.freeze_knowledge_profile
    frozen = select("writer", "scene_draft", reread_reason="known_error")
    assert frozen["entries"][0]["original"] == original.decode()
    assert frozen["entries"][0]["original_sha256"] == sha256(original).hexdigest()
    path.write_text("A later revision cannot change the frozen bytes.")
    with pytest.raises(DomainError, match="differs"):
        select("writer", "scene_draft", reread_reason="review_failure")
    assert select("writer", "scene_draft")["entries"][0].get("original") is None
    assert frozen["entries"][0]["original"] == original.decode()


@pytest.mark.unit
def test_all_published_methods_have_valid_role_scope_and_recoverable_bytes():
    root, catalog, roles = knowledge._directory()
    methods = json.loads((root / "methods.json").read_bytes())
    assert set(methods) == {entry["id"] for entry in catalog["entries"]}
    for entry in catalog["entries"]:
        assert entry["roles"] and set(entry["roles"]) <= roles.keys()
        assert entry["sources"] and set(entry["sources"]) <= catalog["sources"].keys()
        for source in entry["sources"]:
            provenance = catalog["sources"][source]
            assert all(
                provenance[key]
                for key in ("url", "hash", "hash_kind", "grade", "license", "author")
            )
        valid_modes = {mode for role in entry["roles"] for mode in roles[role]}
        assert set(entry["tasks"]) <= valid_modes
        assert (
            sha256((root / entry["original_ref"]).read_bytes()).hexdigest()
            == entry["original_sha256"]
        )
        assert set(methods[entry["id"]]) == {"method", "limit"}
        assert set(entry["genres"]) <= set(catalog["genres"].values())
