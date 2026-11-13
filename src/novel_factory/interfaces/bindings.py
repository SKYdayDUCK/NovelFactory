"""Construct a local NF application runtime from a private operator-provisioned binding."""
import json
from pathlib import Path

from novel_factory.application.kernel import Kernel
from novel_factory.application.mcp_runtime import CreativeControllerBinding, McpRuntime, ProjectBinding, TaskBinding, TaskOperationsBinding
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def read_private_config(path):
    from novel_factory.infrastructure.private_files import read_private_bytes

    return json.loads(read_private_bytes(path, 16384))


def parse_binding(binding):
    if set(binding) == {"work_id", "branch_id"}:
        bound = ProjectBinding(**binding)
    elif set(binding) == {"mode", "work_id", "branch_id"} and binding["mode"] == "creative_controller":
        bound = CreativeControllerBinding(binding["work_id"], binding["branch_id"])
    elif set(binding) == {"mode", "work_id", "branch_id"} and binding["mode"] == "task_operations":
        bound = TaskOperationsBinding(binding["work_id"], binding["branch_id"])
    elif set(binding) == {"task_id", "generation", "worker_id"} and type(binding["generation"]) is int:
        bound = TaskBinding(**binding)
    else:
        raise ValueError("Invalid binding")
    return bound


def runtime_from_config(config, *, compact_role_reads=False):
    required = {"token", "database_url", "object_root", "binding"}
    if set(config) != required or not Path(config["object_root"]).is_absolute():
        raise ValueError("Invalid server configuration")
    bound = parse_binding(config["binding"])
    database = Database(config["database_url"], pool_size=2)
    try:
        kernel = Kernel(database, ObjectStore(config["object_root"]))
        return McpRuntime(kernel, kernel.authenticate(config["token"]), bound, compact_role_reads=compact_role_reads)
    except BaseException:
        database.close()
        raise
