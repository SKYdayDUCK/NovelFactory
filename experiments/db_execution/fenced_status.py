"""Singleton candidate: pin task/input and lock the owning work before locking the live grant."""
from functools import lru_cache

from sqlalchemy import and_, bindparam, select, true

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import Kernel, require


@lru_cache(maxsize=4)
def status_query(tasks, inputs, works):
    same_generation = and_(inputs.c.task_id == tasks.c.task_id,
                           inputs.c.generation == tasks.c.generation)
    locked_work = (select(works.c.work_id.label('locked_work_id'))
                   .where(works.c.work_id == tasks.c.work_id).correlate(tasks)
                   .with_for_update(read=True, of=works).lateral('locked_work'))
    return (select(tasks, inputs.c.spec['world_ref'].label('world_ref'), locked_work.c.locked_work_id)
            .select_from(tasks.outerjoin(inputs, same_generation).outerjoin(locked_work, true()))
            .where(tasks.c.task_id == bindparam('requested_task')))


class FencedStatusKernel(Kernel):
    def get_task(self, principal, task_id):
        if not isinstance(principal, Principal):
            # Keep the incumbent's error precedence for malformed trusted callers.
            return super().get_task(principal, task_id)
        with self.db.uow(write=False) as repo:
            t = repo.tables
            row = repo.conn.execute(status_query(t['tasks'], t['task_inputs'], t['works']),
                                    {'requested_task': task_id}).mappings().one_or_none()
            require(row is not None, 'VIEW_DENIED', 'Task unavailable')
            task = dict(row)
            work_id = task.pop('locked_work_id')
            grant = repo.one('grants', {'grant_id': principal.grant_id}, lock='share')
            self._validate_locked_grant(repo, principal, task['work_id'], 'READ', task['branch_id'],
                                        {'work_id': work_id} if work_id else None, grant)
            self._require_task_owner(task, principal)
            require(task['world_ref'] is not None, 'INTERNAL_ERROR', 'Task input unavailable')
            return self._public_task(task, task['world_ref'])
