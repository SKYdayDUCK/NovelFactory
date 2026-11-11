# Agent Workshop

Independent stdio MCP application for organization management. Run from this directory with Python 3.12 and a separately provisioned PostgreSQL 17 database. The role running a model may only access the broker's pipes. Only the trusted broker OS identity may read its binding or database credentials. Restrict CONNECT on both service databases to their respective users. The Workshop process does not read a NovelFactory DSN.

    uv sync --locked
    export AW_DATABASE_URL='<private Workshop PostgreSQL DSN>'
    uv run alembic upgrade head
    uv run aw-admin install my-installation my-nf-instance
    uv run aw-admin bootstrap my-org --preset Minimal
    uv run aw-admin enroll my-installation my-org manager /private/aw.json --revision 1
    uv run aw-mcp --binding /private/aw.json

The deployment operator creates private bindings (at most 168 hours). Revocation takes effect at the next database transaction, including on existing stdio sessions. Stop the service by closing stdin. Revoke with `uv run aw-admin revoke my-installation my-org manager`. Use libpq TLS settings and separate OS identities on non-test hosts.

The application protocol is `aw-nf.v1`. Call `aw_capabilities` after MCP initialize to negotiate; unsupported versions fail closed. Request identity comes from the binding. The argument limit is 32 KiB, the wire record limit 256 KiB and the maximum negotiated message TTL 24 hours. List actual tools on each connection. A catalog hash grants no capabilities.

Run `uv run pytest -q` against an isolated migrated database. To verify the NF catalog, run `python scripts/freeze_nf_catalog.py <clean-NF-checkout>` at the revision in `contracts/nf-catalog.json` and compare JSON. Restore backups to new isolated databases before changing any running bindings. No deployment command here sends a model request.

## Delegation and recovery

An NF operator first installs the Workshop service identity in NF and provisions a one-use runtime slot with its actor, scope, grant, world revision, budget and token allowance. In Workshop, configure the corresponding organization budget and slot with `aw-provision budget ORG USD 1.00` and `aw-provision allow-slot ORG INSTALL SLOT EMPLOYEE policy.json`. The private label and budget must match the exact NF policy. `generation` starts at zero. A Workshop supervisor with an active ASSIGN edge calls `aw_delegate`. The durable result contains the operation ID. It can be queried even after a device restart.

Use a separate, operator-provisioned NF launcher for each installation. For example, `aw-reconcile --org ORG --installation INSTALL -- nf-broker-for-this-installation` advances a bounded batch. Start this worker on events, or invoke periodically as a deterministic service. The launcher must use the dedicated NF profile `python -m novel_factory.interfaces.delegation_stdio --binding /private/nf-install.json`. In split-host deployments the launcher forwards stdio to the NF host. Only the NF OS identity needs the NF DSN. A submitted task and its accounting receipt are read back under the original operation ID. A lost reply retains the hold. Human handoff changes follow-up responsibility only.

After a database restore, stop normal workers on both systems. Start the Workshop worker with `--lookup-only`, match operation IDs against NF receipts/attempts and the other system's backup cut, and investigate missing or regressed receipts before re-enabling dispatch. A restored peer returning NOT_FOUND is insufficient to release a hold. Restore to a new instance before switching bindings. Failure before the first network write can leave an unresolved intent awaiting operator reconciliation. The worker deliberately retains its original identity and hold.

## Skills and independent reviews

A deployment operator registers exact content hashes and the trusted employee-to-producer identity mapping with `python -m agent_workshop.review_admin --help`. A manager can share individual skills using `aw_skill_share`. Freeze a resolved skill in a bound task and use `aw_skill_history` to verify its original hash after later releases. Resolution checks current tenure, scope and revocation.

Review intake accepts evidence references, a verified producer group and rule observations from a trusted source. Request a specialized review with `aw_review_request`; a positive deterministic result, an assigned human review and a separately budgeted model review have distinct receipts. `aw_review_decide` records the assigned independent reviewer's opinion. Formal acceptance of a world or narrative change remains in NF.
