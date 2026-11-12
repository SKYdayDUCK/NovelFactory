"""Live employee Skill verification over Workshop's authenticated stdio boundary."""

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from time import monotonic

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.application.queries import WorldQueryService
from novel_factory.contracts.registry import validate_contract
from novel_factory.domain.errors import DomainError


def _check(value):
    require(value, "DYNAMIC_SKILL_DENIED", "The employee, skill or delegated assignment is unavailable")


def _credential(nf, task, employee):
    # The role can supply neither a credential path nor a process command.
    index = os.environ.get("NF_DYNAMIC_WORKSHOP_BINDINGS")
    if not index:
        return os.environ["NF_DYNAMIC_WORKSHOP_BINDING"]
    from novel_factory.interfaces.mcp_stdio import read_private_config

    rows = read_private_config(index)
    matches = [
        row["binding_file"]
        for row in rows
        if row["installation_id"] == nf["installation_id"]
        and (
            (task and row.get("scope_ref") == task["scope_ref"])
            or (not task and employee and row.get("employee_id") == employee)
        )
    ]
    _check(len(matches) == 1)
    return matches[0]


async def _call(binding_id, nf, task, freeze, employee):
    params = StdioServerParameters(
        command=os.environ.get("NF_DYNAMIC_WORKSHOP_EXECUTABLE", "aw-mcp"),
        args=["--binding", _credential(nf, task, employee)],
    )
    args = {"binding_id": binding_id, "nf": nf, "freeze_task": freeze}
    if task:
        args["task"] = task
    async with asyncio.timeout(10):
        async with stdio_client(params) as (read, write), ClientSession(read, write) as client:
            await client.initialize()
            result = await client.call_tool("aw_dynamic_skill_attest", args)
            _check(not result.isError)
            return result.structuredContent


@dataclass(frozen=True)
class _SkillWitness:
    nf: dict
    task: dict | None
    grant: dict
    result: dict
    checked_at: float
    frozen: bool


def _attest(binding_id, nf, task, grant, *, freeze, employee=None):
    try:
        result = asyncio.run(_call(binding_id, nf, task, freeze, employee))
        _check(result and result["nf"] == nf and result["task"] == task and result["frozen"] is freeze)
        binding = validate_contract("WorkshopBinding", result["binding"], "harness")
        _check(
            binding["binding_id"] == binding_id
            and binding["org_id"] == nf["org_id"]
            and binding["installation_id"] == nf["installation_id"]
            and (not employee or binding["employee_id"] == employee)
        )
        value = {
            **grant,
            **(task or {}),
            "role_id": nf["role_id"],
            "task_kind": nf["task_kind"],
            "installation_epoch": nf["installation_epoch"],
            "binding": binding,
            **{k: result[k] for k in ("skill_mode", "skill_epoch", "expires_at")},
        }
        return _SkillWitness(nf, task, grant, value, monotonic(), freeze)
    except DomainError:
        raise
    except Exception:
        _check(False)


class DynamicSkillAuthority:
    def __init__(self, kernel):
        self.kernel = kernel

    def _installation(self, repo, installation_id, world_ref, role_id, task_kind):
        work = repo.one("works", {"work_id": world_ref["work_id"]}, lock="share")
        row = repo.one("delegation_installations", {"installation_id": installation_id}, lock="share")
        _check(
            row
            and work
            and row["active"]
            and not row["outbound_fenced"]
            and row["expires_at"] > repo.now()
            and row["policy_version"] == work["policy_version"]
            and (row["work_id"], row["branch_id"]) == (world_ref["work_id"], world_ref["branch_id"])
        )
        return {
            "role_id": role_id,
            "task_kind": task_kind,
            "installation_epoch": row["epoch"],
            **{k: row[k] for k in ("installation_id", "org_id", "nf_instance_id", "work_id", "branch_id")},
        }

    def _origin(self, repo, task_id, generation, principal=None):
        row = repo.one("tasks", {"task_id": task_id}, lock="share")
        _check(row)
        owner = Principal(row["grant_id"], row["principal_id"])
        _check(principal is None or owner == principal)
        task, grant, spec = WorldQueryService(self.kernel)._task(repo, owner, task_id, generation)
        receipt = repo.one("delegation_receipts", {"task_id": task_id}, lock="share")
        _check(receipt)
        nf = self._installation(
            repo, receipt["installation_id"], spec["world_ref"], task["role_id"], task["task_kind"]
        )
        slot = repo.one(
            "delegation_slots",
            {"installation_id": receipt["installation_id"], "scope_ref": receipt["scope_ref"]},
            lock="share",
        )
        _check(
            slot
            and slot["spec"]["task_id"] == task_id
            and slot["spec"]["role_id"] == task["role_id"]
            and slot["spec"]["task_kind"] == task["task_kind"]
            and (slot["principal_id"], slot["grant_id"], slot["grant_epoch"], slot["actor_binding_ref"])
            == (grant["principal_id"], grant["grant_id"], grant["epoch"], grant["actor_id"])
        )
        fence = {
            "task_id": task_id,
            "generation": generation,
            "task_template_id": slot["task_template_id"],
            **{k: receipt[k] for k in ("operation_id", "receipt_ref", "scope_ref")},
        }
        authority = {
            "permission_epoch": grant["epoch"],
            **{k: grant[k] for k in ("principal_id", "grant_id", "capabilities", "planes")},
        }
        return nf, fence, authority

    def resolve(self, task_id, generation, binding_id, *, freeze=False):
        with self.kernel.db.uow(write=False) as repo:
            nf, task, grant = self._origin(repo, task_id, generation)
        return _attest(binding_id, nf, task, grant, freeze=freeze)

    def check(self, repo, task_id, generation, binding_id, *, live):
        nf, task, grant = self._origin(repo, task_id, generation)
        _check(
            isinstance(live, _SkillWitness)
            and (live.nf, live.task, live.grant) == (nf, task, grant)
            and live.result["binding"]["binding_id"] == binding_id
            and 0 <= monotonic() - live.checked_at <= 10
            and datetime.fromisoformat(live.result["expires_at"]) > repo.now()
        )
        return live.result

    def register(self, controller, world_ref, *, workshop_binding, role_id, task_kind):
        """Authorize pre-task selection; the future NF/AW delegated slot must still match at dispatch."""
        validate_contract("WorkshopBinding", workshop_binding, "harness")

        def scope(repo):
            self.kernel.authorize(repo, controller, world_ref["work_id"], "ADMIN", world_ref["branch_id"])
            self.kernel.revision(repo, world_ref)
            nf = self._installation(repo, workshop_binding["installation_id"], world_ref, role_id, task_kind)
            _check(nf["org_id"] == workshop_binding["org_id"])
            return nf

        with self.kernel.db.uow(write=False) as repo:
            nf = scope(repo)
        fresh = _attest(
            workshop_binding["binding_id"],
            nf,
            None,
            {},
            freeze=False,
            employee=workshop_binding["employee_id"],
        )
        with self.kernel.db.uow(write=False) as repo:
            _check(
                scope(repo) == nf
                and fresh.result["binding"] == workshop_binding
                and datetime.fromisoformat(fresh.result["expires_at"]) > repo.now()
            )
        return fresh.result

    def refresh(self, principal, task_id, generation, *, workshop_binding):
        validate_contract("WorkshopBinding", workshop_binding, "harness")
        with self.kernel.db.uow(write=False) as repo:
            nf, task, grant = self._origin(repo, task_id, generation, principal)
        _check(
            nf["org_id"] == workshop_binding["org_id"]
            and nf["installation_id"] == workshop_binding["installation_id"]
        )
        live = _attest(
            workshop_binding["binding_id"],
            nf,
            task,
            grant,
            freeze=True,
            employee=workshop_binding["employee_id"],
        )
        _check(live.result["binding"] == workshop_binding)
        return live

    def verify(self, repo, principal, task_id, generation, *, workshop_binding, live):
        current = self.check(repo, task_id, generation, workshop_binding["binding_id"], live=live)
        _check(
            live.frozen
            and current["principal_id"] == principal.principal_id
            and current["grant_id"] == principal.grant_id
            and current["binding"] == workshop_binding
        )
        return current
