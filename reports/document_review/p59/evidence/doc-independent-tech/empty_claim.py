"""Isolated public route; emit only status and body, never its fixture credentials."""
import httpx
import json
from pathlib import Path
import tempfile
import sys
from benchmarks.http_load import loopback_server
from novel_factory.application.kernel import Kernel
from novel_factory.fixtures import load_fixture
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.http import create_app

db=Database()
try:
    with tempfile.TemporaryDirectory() as temporary:
        kernel=Kernel(db,ObjectStore(temporary))
        world=load_fixture(kernel)
        with loopback_server(create_app(kernel)) as url, httpx.Client(base_url=url,trust_env=False) as c:
            r=c.post("/api/v1/tasks/claim",json={"worker_id":"doc-review"},headers={"Authorization":"Bearer "+world["token"]})
        actual={"status_code":r.status_code,"json":r.json()}
        Path(sys.argv[1]).write_text(json.dumps(actual)+"\n")
        assert actual=={"status_code":200,"json":None}
finally:
    db.close()
