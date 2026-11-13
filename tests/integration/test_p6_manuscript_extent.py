from uuid import uuid4

import pytest
from test_release import accept_draft, setup_batch, write_scene

from novel_factory.application.production_extent import (
    _chapter_count,
    _count_han,
    bind_extent,
    check_length,
    measure_submission,
)
from novel_factory.application.rolling_production import _store
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture


def test_han_length_counts_prose_not_punctuation_and_accepts_explicit_bounds_only():
    assert _count_han("A 42，水\n𠀀〇。") == 3
    for value in (6000, 10000):
        check_length({"unit": "han_characters", "min": 6000, "max": 10000}, value, None)
    for value in (5999, 10001):
        with pytest.raises(DomainError) as err:
            check_length({"unit": "han_characters", "min": 6000, "max": 10000}, value, None)
        assert err.value.code == "MANUSCRIPT_EXTENT_NOT_MET"
    with pytest.raises(DomainError):
        check_length({"unit": "chapters", "min": 100, "max": 500}, 500000, None)


@pytest.mark.postgres
def test_minimum_chapters_cannot_be_satisfied_by_scene_count_or_reordered_scenes(kernel_env):
    kernel = kernel_env["kernel"]
    f = load_fixture(kernel)
    owner, wr = f["principal"], f["world_ref"]
    uid = str(uuid4())
    length = {"preset": "medium_long", "unit": "chapters", "min": 100, "max": 500}
    with kernel.objects.lock(), kernel.db.uow() as repo:
        ref = bind_extent(kernel, repo, owner, wr, length)
        assert bind_extent(kernel, repo, owner, wr, length) == ref
        prior = None
        draft_refs = []
        for window_index in range(2):
            scenes = []
            for i in range(50):
                item = _store(kernel, repo, owner, wr, "EXTENT_TEST_ONLY", {"number": i + window_index * 50})
                draft_refs.append(item)
                scenes.append({"draft_ref": item})
            checkpoint = {"world_ref": wr, "run_id": uid, "index": window_index, "previous": prior,
                          "chapters": [{"chapter_id": str(uuid4()), "ordinal": window_index + 1, "scenes": scenes}]}
            prior = _store(kernel, repo, owner, wr, "ROLLING_CHECKPOINT", checkpoint)
        cursor = {"run_id": uid, "last_checkpoint": prior, "window_count": 2, "frozen_chapters": 2, "chapter_count": 2}
        count = _chapter_count(kernel, repo, wr, cursor, draft_refs)
        assert count == 2 and len(draft_refs) == 100
        with pytest.raises(DomainError) as err:
            check_length(length, 10000, count)
        assert err.value.code == "MANUSCRIPT_EXTENT_NOT_MET"
        with pytest.raises(DomainError):
            _chapter_count(kernel, repo, wr, cursor, list(reversed(draft_refs)))
        with pytest.raises(DomainError) as rebound:
            bind_extent(kernel, repo, owner, wr, {**length, "min": 1})
        assert rebound.value.code == "TASK_REBIND_REQUIRED"


@pytest.mark.postgres
def test_formally_accepted_import_still_lacks_native_writer_attempt(kernel_env):
    kernel = kernel_env["kernel"]
    f = setup_batch(kernel, count=1)
    draft = accept_draft(kernel, f, write_scene(kernel, f, 0, "北窗的雨声很轻。"))
    with kernel.db.uow(write=False) as repo:
        work = f["world_ref"]["work_id"]
        acceptance = kernel.artifact(repo, draft["acceptance_ref"], work, kind="NARRATIVE_ACCEPTANCE")["payload"]
        ref = draft["draft_ref"]
        text_ref = kernel.artifact(repo, ref, work, kind="NARRATIVE_DRAFT")["payload"]["text_ref"]
        with pytest.raises(DomainError) as err:
            measure_submission(kernel, repo, {"world_ref": f["world_ref"], "volume_id": f["volume_id"]},
                               [{"acceptance": acceptance}], [text_ref])
        assert err.value.code == "INVALID_EVIDENCE"
