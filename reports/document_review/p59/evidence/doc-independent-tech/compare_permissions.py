"""Verify the branch comparison prerequisite on a disposable book."""
import json
from pathlib import Path
import sys
import tempfile

from novel_factory.application.kernel import Kernel
from novel_factory.domain.errors import DomainError
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore

db=Database()
try:
    with tempfile.TemporaryDirectory() as directory:
        kernel=Kernel(db,ObjectStore(directory))
        seed=load_fixture(kernel)
        ref=seed['world_ref']
        results={}
        for cap in ['READ','BRANCH']:
            grant=kernel.issue_grant(seed['principal'],ref['work_id'], role_id='volume_director',capabilities=[cap],planes=['WORLD'])
            actor=kernel.authenticate(grant['token'])
            try:
                reply=kernel.compare(actor,ref,ref)
            except DomainError as exc:
                results[cap]=exc.code
            else:
                assert reply['left']==reply['right']==ref
                results[cap]='RETURNED_COMPARISON'
        assert results=={'READ':'VIEW_DENIED','BRANCH':'RETURNED_COMPARISON'}
        Path(sys.argv[1]).write_text(json.dumps(results)+'\n')
finally:
    db.close()
