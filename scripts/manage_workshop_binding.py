"""Provision one NF installation or one preauthorized runtime slot from the local NF service account."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path

from novel_factory.application.agent_gateway import AgentGateway
from novel_factory.application.delegation import (
    install,
    provision_slot,
    reconcile_restored_installation,
    revoke,
)
from novel_factory.application.kernel import Kernel, require
from novel_factory.application.runtime_tasks import RuntimeTaskService
from novel_factory.application.supervisor import HostJournal, RuntimeSupervisor
from novel_factory.domain.errors import DomainError
from novel_factory.infrastructure.codex_host import CodexAppServerAdapter
from novel_factory.infrastructure.database import Database
from novel_factory.infrastructure.model_gateway import OpenAICompatibleAdapter
from novel_factory.infrastructure.objects import ObjectStore
from novel_factory.interfaces.mcp_stdio import read_private_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--access-file", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("install")
    for name in ["org-id", "nf-instance-id", "expires-at", "binding-output"]:
        add.add_argument("--" + name, required=True)
    for name in ["slot", "revoke", "reconcile"]:
        p = sub.add_parser(name)
        p.add_argument("--installation-id", required=True)
        if name != "revoke":
            p.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    access = read_private_config(args.access_file)
    kernel = Kernel(Database(access["database_url"]), ObjectStore(access["object_root"]))
    try:
        owner = kernel.authenticate(access["token"])
        with kernel.db.uow(write=False) as repo:
            kernel.authorize(repo, owner, access["work_id"], "ADMIN", access["branch_id"])
            if args.command != "install":
                require(
                    repo.one(
                        "delegation_installations",
                        {
                            "installation_id": args.installation_id,
                            "work_id": access["work_id"],
                            "branch_id": access["branch_id"],
                        },
                    ),
                    "VIEW_DENIED",
                    "Installation outside local grant",
                )
        if args.command == "install":
            path = Path(args.binding_output)
            opened = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            ticket = None
            try:
                with os.fdopen(opened, "w") as stream:
                    ticket = install(
                        kernel,
                        owner,
                        nf_instance_id=args.nf_instance_id,
                        org_id=args.org_id,
                        branch_id=access["branch_id"],
                        work_id=access["work_id"],
                        expires_at=datetime.fromisoformat(args.expires_at),
                    )
                    json.dump(
                        {
                            "database_url": access["database_url"],
                            "object_root": access["object_root"],
                            "installation_id": ticket["installation_id"],
                            "token": ticket["token"],
                        },
                        stream,
                    )
                    stream.flush()
                    os.fsync(stream.fileno())
            except BaseException:
                path.unlink(missing_ok=True)
                if ticket:
                    revoke(kernel, owner, ticket["installation_id"])
                raise
            print(
                json.dumps(
                    {
                        "installation_id": ticket["installation_id"],
                        "grant_epoch": ticket["grant_epoch"],
                    }
                )
            )
        elif args.command == "revoke":
            revoke(kernel, owner, args.installation_id)
        elif args.command == "reconcile":
            reconcile_restored_installation(
                kernel, owner, args.installation_id, read_private_config(args.input)
            )
        else:
            slot = read_private_config(args.input)
            definition = slot.pop("definition")
            wr = definition.pop("world_ref")
            require(
                (wr["work_id"], wr["branch_id"]) == (access["work_id"], access["branch_id"]),
                "VIEW_DENIED",
                "Wrong world",
            )
            backend = OpenAICompatibleAdapter.from_environment()
            gateway = AgentGateway(
                kernel,
                {
                    "external": backend,
                    "codex-text": CodexAppServerAdapter(backend, HostJournal(kernel)),
                },
            )
            frozen = RuntimeTaskService(kernel, gateway).define(owner, wr, **definition)
            try:
                provision_slot(
                    RuntimeSupervisor(kernel, gateway),
                    owner,
                    args.installation_id,
                    frozen=frozen,
                    **slot,
                )
            except Exception:
                kernel.revoke_grant(owner, frozen.principal.grant_id)
                raise
            print(
                json.dumps(
                    {"scope_ref": slot["scope_ref"], "task_template_id": slot["task_template_id"]}
                )
            )
    except DomainError as exc:
        raise SystemExit(exc.code) from None
    except Exception:  # noqa: BLE001 - driver errors may include deployment credentials
        raise SystemExit("NF binding operation failed; check local provisioning inputs") from None
    finally:
        kernel.db.close()


if __name__ == "__main__":
    main()
