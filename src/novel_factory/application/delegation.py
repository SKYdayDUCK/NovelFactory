"""Accept one organization intent against an installation and a pre-authorized NF slot."""

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from secrets import token_urlsafe
from uuid import uuid4

from jsonschema import Draft202012Validator, FormatChecker
from sqlalchemy import text

from novel_factory.application.authorization import Principal
from novel_factory.application.kernel import require
from novel_factory.application.supervisor import RuntimeSupervisor
from novel_factory.domain.canonical import fingerprint

PROTOCOL = "aw-nf.v1"
_UUID = {"type": "string", "format": "uuid"}
_REF = {"type": "string", "minLength": 1, "maxLength": 128}
_BASE = {
    "protocol_version": {"const": PROTOCOL},
    "nf_instance_id": _UUID,
    "work_id": _UUID,
    "branch_id": _UUID,
    "operation_id": _UUID,
    "grant_epoch": {"type": "integer", "minimum": 1},
}
_SUBMIT = dict(
    _BASE,
    task_template_id=_REF,
    scope_ref=_REF,
    actor_binding_ref={"oneOf": [_UUID, {"type": "null"}]},
    role_id=_REF,
    generation={"type": "integer", "const": 0},
    budget_reservation_ref=_UUID,
    max_amount={"type": "string", "pattern": r"^(0|[1-9][0-9]{0,11})(\.[0-9]{1,6})?$"},
    currency={"enum": ["USD", "CNY"]},
    token_limit={"type": "integer", "minimum": 0, "maximum": 2147483647},
)
SCHEMAS = {
    name: {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }
    for name, props in [("nf_delegation_get", _BASE), ("nf_delegation_submit", _SUBMIT)]
}
_VALIDATORS = {
    k: Draft202012Validator(v, format_checker=FormatChecker()) for k, v in SCHEMAS.items()
}


def install(kernel, controller, *, nf_instance_id, org_id, work_id, branch_id, expires_at):
    """Private provisioning API. The installation credential has no general Kernel grant."""
    token, install_id = token_urlsafe(32), str(uuid4())
    with kernel.db.uow() as repo:
        work, _ = kernel._authorize_with_work(repo, controller, work_id, "ADMIN", branch_id)
        require(
            repo.one("branches", {"work_id": work_id, "branch_id": branch_id})
            and isinstance(expires_at, datetime)
            and expires_at.tzinfo
            and expires_at > repo.now(),
            "INVALID_ARGUMENT",
            "Expected a live, branch-bound installation",
        )
        repo.add(
            "delegation_installations",
            {
                "installation_id": install_id,
                "org_id": org_id,
                "work_id": work_id,
                "nf_instance_id": nf_instance_id,
                "branch_id": branch_id,
                "secret_hash": sha256(token.encode()).hexdigest(),
                "policy_version": work["policy_version"],
                "expires_at": expires_at,
            },
        )
    return {"installation_id": install_id, "token": token, "grant_epoch": 1}


def revoke(kernel, controller, installation_id):
    """Fence the installation and the distinct actor grants issued for its slots."""
    with kernel.db.uow() as repo:
        key = {"installation_id": installation_id}
        installation = repo.one("delegation_installations", key, lock="update")
        require(installation, "VIEW_DENIED", "Installation unavailable")
        kernel.authorize(
            repo, controller, installation["work_id"], "ADMIN", installation["branch_id"]
        )
        if not installation["active"]:
            return
        repo.change(
            "delegation_installations", key, {"active": False, "epoch": installation["epoch"] + 1}
        )
        for slot in repo.many("delegation_slots", key, order=["grant_id"]):
            gid = slot["grant_id"]
            grant = repo.one("grants", {"grant_id": gid}, lock="update")
            if grant["active"]:
                repo.change(
                    "grants", {"grant_id": gid}, {"active": False, "epoch": grant["epoch"] + 1}
                )
                repo.outbox(
                    "authorization.revoked",
                    "grant",
                    gid,
                    {"epoch": grant["epoch"] + 1},
                    f"revoke:{gid}:{grant['epoch'] + 1}",
                )
        repo.outbox(
            "delegation.revoked",
            "installation",
            installation_id,
            {"epoch": installation["epoch"] + 1},
            f"delegation-revoked:{installation_id}:{installation['epoch'] + 1}",
        )


RECOVERY_FIELDS = (
    "task_id",
    "receipt_ref",
    "generation",
    "task_state",
    "accounting_state",
    "attempts",
)


def _installation_ledger(repo, row):
    result = {}
    for receipt in repo.many("delegation_receipts", {"installation_id": row["installation_id"]}):
        slot = repo.one(
            "delegation_slots",
            {"installation_id": row["installation_id"], "scope_ref": receipt["scope_ref"]},
        )
        view = Delegations._read(repo, row, receipt, slot)
        result[receipt["operation_id"]] = {k: view[k] for k in RECOVERY_FIELDS}
    return result


def installation_ledger(kernel, controller, installation_id):
    with kernel.db.uow(repeatable_read=True) as repo:
        row = repo.one(
            "delegation_installations", {"installation_id": installation_id}, lock="share"
        )
        require(row, "VIEW_DENIED", "Installation unavailable")
        kernel.authorize(repo, controller, row["work_id"], "ADMIN", row["branch_id"])
        return _installation_ledger(repo, row)


def reconcile_restored_installation(kernel, controller, installation_id, expected_receipts):
    """Local NF administrator supplies the partner's reconciled operation and attempt ledger."""
    with kernel.db.uow() as repo:
        row = repo.one(
            "delegation_installations", {"installation_id": installation_id}, lock="update"
        )
        require(
            row and row["active"] and row["outbound_fenced"],
            "INVALID_ARGUMENT",
            "Installation is not fenced",
        )
        kernel.authorize(repo, controller, row["work_id"], "ADMIN", row["branch_id"])
        local = _installation_ledger(repo, row)
        require(expected_receipts == local, "DELEGATION_RECOVERY_FENCED", "Receipts disagree")
        repo.change(
            "delegation_installations",
            {"installation_id": installation_id},
            {"outbound_fenced": False},
        )
        repo.outbox(
            "delegation.reconciled",
            "installation",
            installation_id,
            {"receipt_digest": fingerprint(local), "epoch": row["epoch"]},
            "delegation-reconciled:" + str(uuid4()),
        )


def provision_slot(
    supervisor,
    controller,
    installation_id,
    *,
    scope_ref,
    task_template_id,
    frozen,
    budget_id,
    reserve_amount,
    token_reservation,
    context_tokens=20000,
    accept_summary=False,
):
    """Register an unused RuntimeTaskService.define result; registration never dispatches it."""
    kernel, spec = supervisor.kernel, frozen.spec
    with kernel.db.uow() as repo:
        wr = spec["world_ref"]
        installation = repo.one(
            "delegation_installations", {"installation_id": installation_id}, lock="share"
        )
        kernel.authorize(repo, controller, wr["work_id"], "ADMIN", wr["branch_id"])
        require(
            installation
            and installation["active"]
            and installation["expires_at"] > repo.now()
            and (installation["work_id"], installation["branch_id"], installation["policy_version"])
            == (wr["work_id"], wr["branch_id"], spec["policy_version"]),
            "VIEW_DENIED",
            "Installation unavailable",
        )
        grant = kernel.authorize(
            repo, frozen.principal, wr["work_id"], "TASK_CREATE", wr["branch_id"]
        )
        require(
            spec["state"] == "READY"
            and spec["generation"] == 0
            and spec["capability_grant_id"] == grant["grant_id"]
            and spec["role_id"] == grant["role_id"]
            and not repo.one("tasks", {"task_id": spec["task_id"]}),
            "INVALID_ARGUMENT",
            "Expected an unused runtime definition",
        )
        config, _ = supervisor.dispatch_config(
            repo,
            spec,
            grant,
            manifest_ref=frozen.manifest_ref,
            knowledge_cut=frozen.cut,
            context_tokens=context_tokens,
            input_ref=frozen.agent_input["input_ref"],
            budget_id=budget_id,
            reserve_amount=reserve_amount,
            token_reservation=token_reservation,
            accept_summary=accept_summary,
        )
        budget = repo.one("budgets", {"budget_id": budget_id, "work_id": wr["work_id"]})
        require(
            Decimal(spec["budget"]["max_amount"]) == Decimal(reserve_amount)
            and spec["budget"]["token_budget"] == token_reservation
            and spec["budget"]["currency"] == budget["currency"],
            "INVALID_ARGUMENT",
            "Slot and runtime budgets must agree",
        )
        repo.add(
            "delegation_slots",
            {
                "installation_id": installation_id,
                "scope_ref": scope_ref,
                "task_template_id": task_template_id,
                "principal_id": grant["principal_id"],
                "grant_id": grant["grant_id"],
                "grant_epoch": grant["epoch"],
                "actor_binding_ref": grant["actor_id"],
                "spec": spec,
                "job_config": config,
            },
        )


class Delegations:
    def __init__(self, kernel, installation_id, token):
        self.kernel, self.installation_id = kernel, installation_id
        self.secret_hash = sha256(token.encode()).hexdigest()

    def identity(self, repo):
        row = repo.one(
            "delegation_installations",
            {"installation_id": self.installation_id, "secret_hash": self.secret_hash},
            lock="share",
        )
        require(
            row and row["active"] and row["expires_at"] > repo.now(),
            "VIEW_DENIED",
            "Installation unavailable",
        )
        work = repo.one("works", {"work_id": row["work_id"]}, lock="share")
        require(
            work and row["policy_version"] == work["policy_version"],
            "VIEW_DENIED",
            "Installation unavailable",
        )
        return row

    def call(self, name, data):
        require(name in SCHEMAS, "VIEW_DENIED", "Unknown delegation tool")
        require(_VALIDATORS[name].is_valid(data), "INVALID_ARGUMENT", "Invalid delegation request")
        with self.kernel.db.uow() as repo:
            identity = self.identity(repo)
            require(
                (
                    identity["nf_instance_id"],
                    identity["work_id"],
                    identity["branch_id"],
                    identity["epoch"],
                )
                == (
                    data["nf_instance_id"],
                    data["work_id"],
                    data["branch_id"],
                    data["grant_epoch"],
                ),
                "VIEW_DENIED",
                "Installation unavailable",
            )
            key = {"installation_id": self.installation_id, "operation_id": data["operation_id"]}
            if name == "nf_delegation_submit":
                repo.conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
                    {"key": f"nf-delegation:{self.installation_id}:{data['operation_id']}"},
                )
            receipt = repo.one("delegation_receipts", key)
            if not receipt:
                require(
                    name == "nf_delegation_submit", "DELEGATION_NOT_FOUND", "No matching operation"
                )
            scope = receipt["scope_ref"] if receipt else data["scope_ref"]
            slot = repo.one(
                "delegation_slots",
                {"installation_id": self.installation_id, "scope_ref": scope},
                lock="update" if name == "nf_delegation_submit" else "share",
            )
            require(slot, "VIEW_DENIED", "Slot unavailable")
            spec = slot["spec"]
            owner = Principal(slot["grant_id"], slot["principal_id"])
            role = self.kernel.authorize(
                repo, owner, data["work_id"], "TASK_CREATE", data["branch_id"]
            )
            require(
                role["epoch"] == slot["grant_epoch"]
                and role["actor_id"] == slot["actor_binding_ref"]
                and role["role_id"] == spec["role_id"]
                and spec["policy_version"] == identity["policy_version"],
                "VIEW_DENIED",
                "Slot unavailable",
            )
            if name == "nf_delegation_submit":
                if receipt:
                    require(
                        receipt["request_hash"] == fingerprint(data),
                        "IDEMPOTENCY_MISMATCH",
                        "Operation content changed",
                    )
                else:
                    require(
                        not identity["outbound_fenced"],
                        "DELEGATION_RECOVERY_FENCED",
                        "Reconcile restored installations",
                    )
                    receipt = self._bind(repo, data, key, slot, owner)
            return self._read(repo, identity, receipt, slot)

    def _bind(self, repo, data, key, slot, owner):
        spec, config = slot["spec"], slot["job_config"]
        require(
            (
                data["role_id"],
                data["task_template_id"],
                data["actor_binding_ref"],
                data["generation"],
            )
            == (
                spec["role_id"],
                slot["task_template_id"],
                slot["actor_binding_ref"],
                spec["generation"],
            ),
            "VIEW_DENIED",
            "Slot unavailable",
        )
        task_budget = spec["budget"]
        require(
            data["currency"] == task_budget["currency"]
            and data["token_limit"] == task_budget["token_budget"]
            and Decimal(data["max_amount"]) == Decimal(task_budget["max_amount"]),
            "BUDGET_MISMATCH",
            "Allowances must match",
        )
        used = repo.one(
            "delegation_receipts",
            {"installation_id": self.installation_id, "scope_ref": slot["scope_ref"]},
        )
        require(not used, "SLOT_CONSUMED", "Slot already assigned")
        hold_key = data["budget_reservation_ref"]
        repo.conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"),
            {"key": f"nf-delegation-hold:{self.installation_id}:{hold_key}"},
        )
        require(
            not repo.one(
                "delegation_receipts",
                {"installation_id": self.installation_id, "budget_reservation_ref": hold_key},
            ),
            "BUDGET_MISMATCH",
            "Hold already bound",
        )
        budget = repo.one(
            "budgets",
            {"budget_id": config["dispatch"]["budget_id"], "work_id": data["work_id"]},
            lock="share",
        )
        require(
            budget
            and budget["currency"] == data["currency"]
            and budget["limit_amount"] - budget["spent_amount"] - budget["reserved_amount"]
            >= Decimal(data["max_amount"]),
            "BUDGET_EXCEEDED",
            "NF budget unavailable",
        )
        task = self.kernel.create_task_in_transaction(
            repo, owner, spec, "delegation:" + key["operation_id"]
        )
        RuntimeSupervisor.install_job(repo, task, config)
        receipt = dict(
            key,
            task_id=task["task_id"],
            receipt_ref=str(uuid4()),
            request_hash=fingerprint(data),
            scope_ref=slot["scope_ref"],
            budget_reservation_ref=hold_key,
        )
        repo.add("delegation_receipts", receipt)
        repo.outbox(
            "delegation.bound",
            "task",
            task["task_id"],
            {"receipt_ref": receipt["receipt_ref"]},
            f"delegation-bound:{self.installation_id}:{data['operation_id']}",
        )
        return receipt

    @staticmethod
    def _read(repo, identity, receipt, slot):
        task = repo.one("tasks", {"task_id": receipt["task_id"]}, lock="share")
        calls = repo.many("call_attempts", {"task_id": task["task_id"]}, order=["attempt_id"])
        states = {r["state"] for r in calls}
        accounting = (
            "UNKNOWN"
            if "UNKNOWN" in states
            else "RESERVED"
            if states & {"PREPARED", "SENT"}
            else "SETTLED"
            if calls
            else "NO_ATTEMPT"
        )
        attempts = [
            {
                "attempt_id": r["attempt_id"],
                "state": r["state"],
                "actual_amount": None if r["actual_amount"] is None else str(r["actual_amount"]),
                "actual_tokens": r["actual_tokens"],
            }
            for r in calls
        ]
        return {
            "protocol_version": PROTOCOL,
            "nf_instance_id": identity["nf_instance_id"],
            "work_id": identity["work_id"],
            "branch_id": identity["branch_id"],
            "operation_id": receipt["operation_id"],
            "task_id": task["task_id"],
            "receipt_ref": receipt["receipt_ref"],
            "world_ref": slot["spec"]["world_ref"],
            "generation": task["generation"],
            "task_state": task["state"],
            "accounting_state": accounting,
            "currency": slot["spec"]["budget"]["currency"],
            "actual_amount": format(
                sum((r["actual_amount"] or Decimal(0) for r in calls), Decimal(0)), "f"
            ),
            "actual_tokens": sum(r["actual_tokens"] or 0 for r in calls),
            "attempt_ids": [a["attempt_id"] for a in attempts],
            "attempts": attempts,
        }
