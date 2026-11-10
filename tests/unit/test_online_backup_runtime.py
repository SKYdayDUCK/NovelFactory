"""A backup must not publish a mixed package during a multi-file deployment."""
from hashlib import sha256
from pathlib import Path

import pytest

from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure import online_backup
from novel_factory.infrastructure.online_backup import OnlineBackupService

pytestmark = pytest.mark.unit


@pytest.fixture
def package(tmp_path):
    source = tmp_path / "source"
    paths = {
        "module": source / "src" / "novel_factory" / "alpha.py",
        "schema": source / "contracts" / "one.schema.json",
        "asset": source / "runtime" / "query_profiles.json",
        "second_module": source / "src" / "novel_factory" / "beta.py",
        "roles": source / "runtime" / "roles" / "catalog.json",
        "last": source / "runtime" / "skills" / "catalog.json",
    }
    for key, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# original " + key + "\n", encoding="utf-8")
    service = OnlineBackupService(None, None, package_root=source / "src" / "novel_factory")
    return service, paths, tmp_path / "destination"


def test_unchanged_runtime_schema_and_policy_inventory_is_complete(package):
    service, paths, destination = package
    manifest = service._copy_interpretation(destination)
    assert set(manifest["runtime_files"]) == {"alpha.py", "beta.py"}
    assert set(manifest["schemas"]) == {"one.schema.json"}
    assert set(manifest["runtime_assets"]) == {
        "runtime/query_profiles.json", "runtime/roles/catalog.json", "runtime/skills/catalog.json"}
    for key, folder in (("runtime_files", "interpretation"), ("schemas", "schemas"),
                        ("runtime_assets", "runtime_assets")):
        for relative, digest in manifest[key].items():
            assert sha256((destination / folder / relative).read_bytes()).hexdigest() == digest
    assert (destination / "interpretation" / "alpha.py").read_bytes() == paths["module"].read_bytes()


@pytest.mark.parametrize("group", ["module", "schema", "asset"])
@pytest.mark.parametrize("change", ["modify_already_copied", "add_member", "remove_member"])
def test_late_deployment_change_rejects_the_complete_package(package, monkeypatch, group, change):
    service, paths, destination = package
    real_copy = online_backup.shutil.copyfile
    changed = []

    def copy_during_deploy(source, target):
        result = real_copy(source, target)
        if Path(source) == paths["last"]:
            victim = paths[group]
            if change == "modify_already_copied":
                victim.write_text("# newer deployment\n", encoding="utf-8")
            elif change == "add_member":
                suffix = {"module": ".py", "schema": ".schema.json", "asset": ".md"}[group]
                (victim.parent / ("new_plugin" + suffix)).write_text("new member\n", encoding="utf-8")
            else:
                victim.unlink()
            changed.append(True)
        return result

    monkeypatch.setattr(online_backup.shutil, "copyfile", copy_during_deploy)
    with pytest.raises(DomainError) as denied:
        service._copy_interpretation(destination)
    assert changed
    # Removing a required role/profile asset can fail at the asset inventory
    # gate before the general membership comparison; both reject completion.
    assert denied.value.code in {"INTERPRETATION_CHANGED", "INTERPRETATION_UNAVAILABLE"}


def test_copy_must_match_the_initial_inventory_even_if_source_changes_before_copy(package, monkeypatch):
    service, paths, destination = package
    real_copy = online_backup.shutil.copyfile

    def copy_replaced_source(source, target):
        if Path(source) == paths["module"]:
            paths["module"].write_text("# changed between inventory and copy\n", encoding="utf-8")
        return real_copy(source, target)

    monkeypatch.setattr(online_backup.shutil, "copyfile", copy_replaced_source)
    with pytest.raises(DomainError) as denied:
        service._copy_interpretation(destination)
    assert denied.value.code == "INTERPRETATION_CHANGED"


def test_stable_deploy_after_database_cut_cannot_replace_the_pinned_runtime(package):
    service, paths, destination = package
    # create() pins this inventory before exporting the database snapshot.
    initial = service._interpretation_inventory()
    paths["module"].write_text("# completed deployment during pg_dump\n", encoding="utf-8")
    with pytest.raises(DomainError) as denied:
        service._copy_interpretation(destination, initial=initial)
    assert denied.value.code == "INTERPRETATION_CHANGED"
