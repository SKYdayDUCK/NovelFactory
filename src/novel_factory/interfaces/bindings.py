"""Construct a local NF application runtime from a private operator-provisioned binding."""
import json
from pathlib import Path

from novel_factory.application.kernel import Kernel
from novel_factory.application.mcp_runtime import CreativeControllerBinding, McpRuntime, ProjectBinding, TaskBinding
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.objects import ObjectStore


def read_private_config(path):
    from novel_factory.infrastructure.private_files import read_private_bytes

    return json.loads(read_private_bytes(path, 16384))


def runtime_from_config(config, *, pool_slots=2, compact_role_reads=False):
    required = {"token", "database_url", "object_root", "binding"}
    if set(config) != required or not Path(config["object_root"]).is_absolute():
        raise ValueError("Invalid server configuration")
    binding = config["binding"]
    if set(binding) == {"work_id", "branch_id"}:
        bound = ProjectBinding(**binding)
    elif set(binding) == {"mode", "work_id", "branch_id"} and binding["mode"] == "creative_controller":
        bound = CreativeControllerBinding(binding["work_id"], binding["branch_id"])
    elif set(binding) == {"task_id", "generation", "worker_id"} and type(binding["generation"]) is int:
        bound = TaskBinding(**binding)
    else:
        raise ValueError("Invalid binding")
    if type(pool_slots) is not int or pool_slots not in (1, 2):
        raise ValueError("A stdio binding supports one or two local database slots")
    database = Database(config["database_url"], pool_size=pool_slots)
    try:
        kernel = Kernel(database, ObjectStore(config["object_root"]))
        return McpRuntime(kernel, kernel.authenticate(config["token"]), bound, compact_role_reads=compact_role_reads)
    except BaseException:
        database.close()
        raise
