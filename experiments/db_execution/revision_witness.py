"""Experimental fixed-revision transfer control. A matching live PG byte witness saves the payload."""
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256

from sqlalchemy import LargeBinary, Text, bindparam, case, func, select

from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.repository import KernelRepository


@dataclass(frozen=True)
class Snapshot:
    ref: tuple
    digest: bytes
    text: str


@lru_cache(maxsize=4)
def conditional_read(revisions):
    body = revisions.c.snapshot.cast(Text)
    unchanged = func.sha256(func.convert_to(body, 'UTF8')) == bindparam('expected', type_=LargeBinary)
    projection = case((unchanged, None), else_=body).label('snapshot')
    columns = [projection if col.key == 'snapshot' else col for col in revisions.c]
    return select(*columns).where(*(revisions.c[key] == bindparam(key) for key in
                                     ('work_id', 'branch_id', 'world_revision_id')))


class ReadProjection:
    def __init__(self, repo, previous):
        self.repo = repo
        self.previous = previous
        self.next_snapshot = None

    def revision_with_snapshot_text(self, world_ref):
        ref = tuple(world_ref[k] for k in ('work_id', 'branch_id', 'world_revision_id'))
        previous = self.previous
        if previous is not None and previous.ref == ref:
            result = self.repo.conn.execute(conditional_read(self.repo.tables['revisions']),
                                            {**world_ref, 'expected': previous.digest}).mappings().one_or_none()
            if result is None:
                return None
            row = dict(result)
            if row['snapshot'] is None:
                row['snapshot'] = previous.text
                return row
        else:
            row = self.repo.revision_with_snapshot_text(world_ref)
        if row is not None and len(row['snapshot']) <= 2 * 1024 * 1024:
            data = row['snapshot'].encode('utf8')
            if len(data) <= 2 * 1024 * 1024:
                self.next_snapshot = Snapshot(ref, sha256(data).digest(), row['snapshot'])
        return row


class WitnessKernel(Kernel):
    """At most one previously verified immutable string per kernel. No cached business response."""
    def __init__(self, db, objects):
        super().__init__(db, objects)
        self._snapshot = None

    def revision(self, repo, world_ref):
        if not isinstance(repo, KernelRepository):
            return super().revision(repo, world_ref)
        # Capture one immutable object for this invocation, including when
        # another thread concurrently replaces the process-local entry.
        reader = ReadProjection(repo, self._snapshot)
        revision = super().revision(reader, world_ref)
        if reader.next_snapshot is not None:
            self._snapshot = reader.next_snapshot
        return revision
