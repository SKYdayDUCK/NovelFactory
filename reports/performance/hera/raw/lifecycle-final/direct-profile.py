from datetime import datetime, timedelta, UTC
from uuid import uuid4
from tempfile import TemporaryDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
import argparse,cProfile,pstats,os
from pathlib import Path
from novel_factory.application.kernel import Kernel
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.application.task_operations import TaskOperations
from novel_factory.contracts.registry import validate_contract
from novel_factory.fixtures import load_fixture
from novel_factory.interfaces.http import jsonable_encoder, plain
from decimal import Decimal
from seven_call import fresh_database
def run():
 with TemporaryDirectory() as tmp, fresh_database(os.environ['NF_TEST_DATABASE_URL'],Path.cwd()) as url:
  db=Database(url,pool_size=4); k=Kernel(db,ObjectStore(tmp))
  books=[load_fixture(k),load_fixture(k)]
  with db.uow() as r: r.conn.execute(text('UPDATE nf_durable_admission_policy SET enabled=true'))
  lanes=[(k.issue_grant(b['principal'],b['world_ref']['work_id'],role_id='writer',branch_ids=[b['world_ref']['branch_id']]),b['world_ref']) for b in books]
  def cycle(i):
   actor,wr=lanes[i%2];tid=str(uuid4());spec=dict(schema_version='2.0',task_id=tid,generation=0,role_id='writer',task_kind='direct',state='READY',world_ref=wr,story_time={'tick':0,'phase':0},context_bundle_id=None,policy_version='nf-policy-1',dependency_fingerprint=None,runtime_contract_version='1'*64,capability_grant_id=actor['principal'].grant_id,input_artifacts=[],budget={'currency':'USD','max_amount':'1','token_budget':10},wall_deadline=(datetime.now(UTC)+timedelta(hours=1)).isoformat())
   for j in range(7):
    op=TaskOperations(k,k.authenticate(actor['token']),fenced_status=True)
    if j in (0,2): val=validate_contract('TaskCreateRequest',{'spec':spec,'idempotency_key':tid},'http');out=op.create(**val)
    elif j==3:args=validate_contract('TaskClaimRequest',{'task_id':tid,'worker_id':'worker','lease_seconds':300},'http');out=k.claim_task(op.principal,**args)
    elif j==5:args=validate_contract('TaskCancelHttpRequest',{'task_id':tid,'generation':1,'idempotency_key':'c'+tid},'http');out=op.cancel(**args)
    else:validate_contract('UUID',tid);out=op.status(tid)
    assert jsonable_encoder(plain(out),custom_encoder={Decimal:str})['task_id']==tid
  for i in range(6):cycle(i)
  prof=cProfile.Profile()
  prof.enable()
  for i in range(32):cycle(i)
  prof.disable();prof.dump_stats(args.output);db.close()
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--output', required=True);args=parser.parse_args();run()
