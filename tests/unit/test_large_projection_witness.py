"""A large immutable snapshot keeps a bounded, live content witness."""
import json
from uuid import uuid4

import pytest

from novel_factory.application import kernel as kernel_module
from novel_factory.application.kernel import Kernel
from novel_factory.domain.canonical import fingerprint
from novel_factory.domain.errors import DomainError


def test_large_snapshot_reuses_validation_and_still_examines_every_read(monkeypatch):
    wr = {key: str(uuid4()) for key in ("work_id", "branch_id", "world_revision_id")}
    snapshot = {"entities": {str(i): {"name": "x" * 160} for i in range(7000)}}
    raw = json.dumps(snapshot)
    assert len(raw) > 1_000_000
    row = {**wr, "snapshot": raw, "state_hash": fingerprint(snapshot), "provenance": {}}

    class Stored:
        def revision_with_snapshot_text(self, ref):
            assert ref == wr
            return row.copy()

    calls = []
    real = kernel_module.fingerprint
    def logged(obj):
        calls.append(1)
        return real(obj)
    monkeypatch.setattr(kernel_module, "fingerprint", logged)
    kernel = object.__new__(Kernel)
    kernel._last_verified_projection = None
    storage = Stored()
    first = kernel.revision(storage, wr)
    first["snapshot"]["entities"].clear()
    assert len(kernel.revision(storage, wr)["snapshot"]["entities"]) == 7000
    assert len(calls) == 1
    row["snapshot"] = raw.replace("x" * 160, "y" * 160, 1)
    with pytest.raises(DomainError) as failure:
        kernel.revision(storage, wr)
    assert failure.value.code == "PROJECTION_CORRUPT" and len(calls) == 2
