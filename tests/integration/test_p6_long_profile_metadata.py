"""Synthetic scene references exercise only the long-plan metadata path, never publication."""
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import text

from novel_factory.application.kernel import Kernel
from novel_factory.application.production_profiles import (
    bind_profile,
    load_profile,
    resolve_profile,
)
from novel_factory.application.rolling_production import _store, partition_volumes
from novel_factory.domain.canonical import fingerprint
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


def ident(kind, index):
    return str(uuid5(NAMESPACE_URL, f"urn:novelfactory:p6:metadata:{kind}:{index}"))


def planned_volumes(count, chapters_per_volume=100):
    volumes = []
    for chapter in range(count):
        if chapter % chapters_per_volume == 0:
            volumes.append({"volume_id": ident("volume", chapter), "chapters": []})
        ref = {"artifact_id": ident("artifact", chapter), "artifact_revision_id": ident("revision", chapter),
               "sha256": sha256(str(chapter).encode()).hexdigest()}
        volumes[-1]["chapters"].append({"chapter_id": ident("chapter", chapter),
                                       "scenes": [{"scene_ref": ref, "handoff_refs": []}]})
    return volumes


@pytest.mark.parametrize(("chapters", "preset", "expected_windows"), [
    (100, "medium_long", 9), (501, "long", 36), (3000, "ultra_long", 150),
])
def test_ordered_multi_volume_metadata_after_database_reconnect(kernel_env, chapters, preset, expected_windows):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel)
    wr, owner = fixture["world_ref"], fixture["principal"]
    profile = resolve_profile(None, "无限流", complexity={"target_chapters": chapters, "storylines": 3})
    assert profile["preset"] == preset
    volumes = planned_volumes(chapters)
    windows = partition_volumes(volumes, profile["window_chapters"])
    assert len(windows) == expected_windows
    assert max(len(window["chapters"]) for window in windows) <= profile["window_chapters"]
    with kernel.objects.lock(), kernel.db.uow() as repo:
        pinned = bind_profile(kernel, repo, owner, wr, profile)
        refs = [_store(kernel, repo, owner, wr, "ROLLING_WINDOW", window) for window in windows]

    other = Database(kernel_env["url"], pool_size=2)
    try:
        recovered = Kernel(other, ObjectStore(kernel_env["store"].root))
        ordered = []
        with recovered.db.uow(write=False, repeatable_read=True) as repo:
            deployed = load_profile(recovered, repo, owner, wr["work_id"], wr["branch_id"])
            assert deployed["profile_ref"] == pinned["profile_ref"]
            for ordinal, ref in enumerate(refs):
                data = recovered.artifact(repo, ref, wr["work_id"], kind="ROLLING_WINDOW")["payload"]
                assert fingerprint(data) == ref["sha256"] and data == windows[ordinal]
                recovered.objects.verify(ref["sha256"])
                ordered.extend((data["volume_id"], row["chapter_id"]) for row in data["chapters"])
            assert repo.conn.scalar(text("SELECT COUNT(*) FROM nf_tasks WHERE work_id=:id"), {"id": wr["work_id"]}) == 0
            assert repo.conn.scalar(text("SELECT COUNT(*) FROM nf_artifacts WHERE work_id=:id AND kind='RELEASE_RECORD'"),
                                    {"id": wr["work_id"]}) == 0
        assert ordered == [(v["volume_id"], c["chapter_id"]) for v in volumes for c in v["chapters"]]
        assert len(set(ordered)) == chapters
    finally:
        other.close()
