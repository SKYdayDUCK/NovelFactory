"""Measure detect_revision on an isolated migrated PG17, including unrelated observations."""
import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import event, text

from novel_factory.application.kernel import Kernel
from novel_factory.application.semantic_dependencies import detect_revision
from novel_factory.fixtures import create_bound_task, load_fixture, point, run_command
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    n = 20000
    db = Database(os.environ['NF_TEST_DATABASE_URL'])
    with TemporaryDirectory() as directory:
        kernel = Kernel(db, ObjectStore(directory))
        f = load_fixture(kernel)
        wr, user, ids = f['world_ref'], f['principal'], f['ids']
        task = create_bound_task(kernel, user, wr, point(1), [ids['sword']], ['lifecycle'])
        after = run_command(kernel,user,wr,'DESTROY_ITEM',{'item_id':ids['sword'],'at':point(20)})['world_ref']
        # These deliberately unrelated synthetic indexed receipts all occupy
        # the same branch and observation generation as the real reader.
        with db.uow() as repo:
            repo.conn.execute(text("""
                INSERT INTO nf_semantic_reads(observation_id,task_id,generation,work_id,branch_id,world_revision_id,
                    branch_seq,keys,details)
                SELECT gen_random_uuid(),:task,:gen,:work_id,:branch_id,:world_revision_id,0,
                       ARRAY[concat('unrelated:',i)],'{"origin":"QUERY"}'::jsonb
                FROM generate_series(1,:n) i
            """), {**wr,'task':task['task_id'],'gen':task['generation'],'n':n})
            repo.conn.exec_driver_sql('ANALYZE nf_semantic_reads, nf_semantic_changes, nf_tasks')
        plans = []
        def capture(conn, clause, multi, params, options):
            sql = str(clause)
            if sql.lstrip().startswith('SELECT c.*,r.observation_id'):
                plan = conn.scalar(text('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) '+sql),params)
                plans.append(plan[0])
        event.listen(db.engine,'before_execute',capture)
        try:
            with db.uow() as repo:
                pg_version = repo.conn.scalar(text('SHOW server_version'))
                cases = detect_revision(repo,after)
        finally:
            event.remove(db.engine,'before_execute',capture)
        assert len(cases) == len(plans) == 1
        plan = plans[0]
        assert 'nf_sem_read_keys' in json.dumps(plan), 'Planner did not use the semantic read GIN index'
        result = {'postgresql_version':pg_version, 'unrelated_synthetic_receipts':n,
                  'affected_tasks':len(cases), 'actual_detector_plan':plan}
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({k:v for k,v in result.items() if k!='actual_detector_plan'}))
        print('Planning ms',plan['Planning Time'],'Execution ms',plan['Execution Time'])
    db.close()


if __name__ == '__main__':
    main()
