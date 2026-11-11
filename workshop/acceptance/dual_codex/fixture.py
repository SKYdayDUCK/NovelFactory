"""Create synthetic NF and Workshop identities in two pre-migrated, isolated service databases."""
import json
import os
from pathlib import Path
from uuid import uuid4

from agent_workshop.admin import enroll
from agent_workshop.auth import connect
from agent_workshop.organization import bootstrap
from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.kernel import Kernel
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.fixtures import load_fixture, point
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.objects import ObjectStore
from tests.integration.test_p5_mcp import config


def main():
    root = Path(os.environ["DUAL_FIXTURE_ROOT"])
    private = root / "private"
    nf_binding, aw_binding = private / "nf/role.json", private / "aw/employee.json"
    nf_url, aw_url = os.environ["NF_DATABASE_URL"], os.environ["AW_DATABASE_URL"]
    org, installation = uuid4().hex, uuid4().hex
    with connect(aw_url) as c:
        c.execute("INSERT INTO aw_installations VALUES (%s,%s,true)", (installation, uuid4().hex))
        bootstrap(c, org, installation_id=installation)
        token = enroll(c, installation, org, "actor-1", 1, 1)
        with os.fdopen(os.open(aw_binding, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump({"database_url": aw_url, "token": token}, f)
    db = Database(nf_url)
    try:
        store = ObjectStore(private / "nf/objects")
        kernel = Kernel(db, store)
        world = load_fixture(kernel)
        stub = OpenAICompatibleAdapter(endpoint="https://example.invalid/v1/chat/completions", model="test", api_key=None)
        task = RuntimeTaskService(kernel, AgentGateway(kernel, {"external":stub})).prepare(
            world["principal"], world["world_ref"], role_id="character_actor", task_kind="character_action",
            actor_id=world["ids"]["protagonist"], story_time=point(1), subject_ids=[world["ids"]["sword"]],
            predicates=["owner"], assignment="Read the assigned scene.", adapter_id="external")
        selected = {"task_id":task.task["task_id"],"generation":task.task["generation"],"worker_id":task.worker_id}
        config(nf_binding, {"store":store,"url":nf_url}, task.private_token, selected)
        challenge = {"task_id":str(task.task["task_id"]),"generation":task.task["generation"],"org_id":org,
                     "employee_id":"actor-1","protected_paths":[str(nf_binding),str(aw_binding)]}
        (root / "challenge.json").write_text(json.dumps(challenge))
    finally:
        db.close()


if __name__ == "__main__":
    main()
