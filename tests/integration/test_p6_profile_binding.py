from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from novel_factory.application.kernel import Kernel
from novel_factory.application.production_profiles import (
    bind_profile,
    compile_dag,
    load_profile,
    resolve_profile,
)
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.mark.parametrize("preset", ["short", "medium", "medium_long", "long", "ultra_long"])
def test_authorized_profile_and_dag_survive_new_pool_and_client(kernel_env, preset):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel)
    owner, wr = fixture["principal"], fixture["world_ref"]
    profile = resolve_profile(preset, "现实主义")
    with kernel.objects.lock(), kernel.db.uow() as repo:
        refs = bind_profile(kernel, repo, owner, wr, profile)
    other = Database(kernel_env["url"], pool_size=2)
    try:
        recovered = Kernel(other, ObjectStore(kernel_env["store"].root))
        with recovered.db.uow() as repo:
            observed = load_profile(recovered, repo, owner, wr["work_id"], wr["branch_id"])
            assert observed == {**refs, "profile": profile, "nodes": compile_dag(profile)}
            assert bind_profile(recovered, repo, owner, wr, profile) == refs
    finally:
        other.close()

    reader = kernel.issue_grant(owner, wr["work_id"], role_id="reviewer", capabilities=["READ"])["principal"]
    with pytest.raises(DomainError) as err, kernel.db.uow() as repo:
        load_profile(kernel, repo, reader, wr["work_id"], wr["branch_id"])
    assert err.value.code == "VIEW_DENIED"
    with pytest.raises(DomainError) as err, kernel.db.uow() as repo:
        bind_profile(kernel, repo, reader, wr, profile)
    assert err.value.code == "VIEW_DENIED"

    with pytest.raises(DomainError) as err, kernel.db.uow() as repo:
        bind_profile(kernel, repo, owner, wr, resolve_profile(preset, "切换题材"))
    assert err.value.code == "TASK_REBIND_REQUIRED"
    with pytest.raises(DomainError) as err, kernel.db.uow() as repo:
        bind_profile(kernel, repo, owner, {**wr, "world_revision_id": str(uuid4())}, profile)
    assert err.value.code == "HEAD_CONFLICT"


def test_rollback_concurrent_binding_and_cross_work_isolation(kernel_env):
    kernel = kernel_env["kernel"]
    fixture = load_fixture(kernel)
    owner, wr = fixture["principal"], fixture["world_ref"]
    profile = resolve_profile("short", "灵异")

    with pytest.raises(RuntimeError), kernel.objects.lock(), kernel.db.uow() as repo:
        bind_profile(kernel, repo, owner, wr, profile)
        raise RuntimeError("injected rollback")
    with kernel.db.uow(write=False) as repo:
        assert load_profile(kernel, repo, owner, wr["work_id"], wr["branch_id"]) is None

    def pin(genre):
        try:
            # The PG work lock guards distinct transactions even without the object-store guard.
            with kernel.db.uow() as repo:
                return bind_profile(kernel, repo, owner, wr, resolve_profile("short", genre))
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(pin, ("现实", "奇幻")))
    assert outcomes.count("TASK_REBIND_REQUIRED") == 1
    expected = next(row for row in outcomes if isinstance(row, dict))
    with kernel.db.uow(write=False) as repo:
        seen = load_profile(kernel, repo, owner, wr["work_id"], wr["branch_id"])
    assert all(seen[k] == expected[k] for k in expected)
    foreign = load_fixture(kernel)
    with pytest.raises(DomainError) as err, kernel.db.uow(write=False) as repo:
        load_profile(kernel, repo, foreign["principal"], wr["work_id"], wr["branch_id"])
    assert err.value.code == "VIEW_DENIED"
