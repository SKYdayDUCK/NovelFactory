# Agent Workshop

Independent stdio MCP application for organization management. Run from this directory with Python 3.12 and a separately provisioned PostgreSQL 17 database. The role running a model may only access the broker's pipes. Only the trusted broker OS identity may read its binding or database credentials. Restrict CONNECT on both service databases to their respective users. The Workshop process does not read a NovelFactory DSN.

    uv sync --locked
    export AW_DATABASE_URL='<private Workshop PostgreSQL DSN>'
    uv run alembic upgrade head
    uv run aw-admin install my-installation my-nf-instance
    uv run aw-admin bootstrap my-org --preset Minimal --installation my-installation
    uv run aw-admin enroll my-installation my-org manager /private/aw.json --revision 1
    uv run aw-mcp --binding /private/aw.json

The deployment operator creates private bindings (at most 168 hours). Revocation takes effect at the next database transaction, including on existing stdio sessions. Stop the service by closing stdin. Revoke with `uv run aw-admin revoke my-installation my-org manager`. Use libpq TLS settings and separate OS identities on non-test hosts.

The application protocol is `aw-nf.v1`. Call `aw_capabilities` after MCP initialize to negotiate; unsupported versions fail closed. Request identity comes from the binding. The argument limit is 32 KiB, the wire record limit 256 KiB and the maximum negotiated message TTL 24 hours. List actual tools on each connection. A catalog hash grants no capabilities.

Run `uv run pytest -q` against an isolated migrated database. To verify the NF catalog, run `python scripts/freeze_nf_catalog.py <clean-NF-checkout>` at the revision in `contracts/nf-catalog.json` and compare JSON. Restore backups to new isolated databases before changing any running bindings. No deployment command here sends a model request.

## Frozen W0 protocol

The `aw_capabilities` response identifies the authenticated installation, its NF instance, its live grant epoch, the NF source commit, both SHA-256 catalog fingerprints, and the argument and TTL limits. The bundled manifest enumerates the ten role files and twelve Skill files. A client can compare its pinned source and hashes with these values before selecting a role. Tool visibility and permission to call it come from each live server's authenticated session. NF project bindings select work and branch. NF task bindings additionally select task, role and generation, and NF revalidates current grants on each call. An AW identity alone confers no work, branch or task access.

The W0 scope reference consists of NF instance, work, branch, and the NF task generation when the operation is task-bound. The organization identity is the separately authenticated installation, organization, employee and employment revision. Later message cursors are scoped to an authorized projection; a cursor or catalog hash cannot be used as a credential. An operation identifier is scoped to the authenticated principal and its operation scope; repeated identifiers with a different request must be rejected by the operation owner. Messaging and delegation use their own subsequent stage contracts. W0 exposes only `aw_capabilities` on the AW connection. NF discovery must be repeated on each project or task connection.

`INVALID_ARGUMENT` denotes a rejected AW schema or size, `PROTOCOL_UNSUPPORTED` an incompatible application protocol, and `VIEW_DENIED` an unavailable capability or binding. A revoked connection lists no tools. `SERVICE_UNAVAILABLE` withholds private database and binding details on service failure.

## W1 organization ownership and upgrades

An installation owns the organization directory it provisions. Use `aw-admin bootstrap ORG --installation INSTALLATION --preset Standard` to create a directory under that installation. A trusted operator can omit the installation option when there is exactly one enabled installation. The existing four presets are Minimal, Standard, Long-form and Research-Audit. Creating or reading a roster makes zero model calls.

An upgraded W0 connection can continue negotiating `aw_capabilities`. Provision the organization explicitly, then issue a fresh binding with `aw-admin enroll INSTALLATION ORG EMPLOYEE PATH --revision CURRENT_TENURE` for organization access. Entering a matching employee name in a pre-W1 binding never grants the role. Each process validates the installation owner and current active tenure for organization tools, including on an already open pipe.

Historic w001 directories predate the ownership field. After `alembic upgrade head`, review the intended installation and then run `aw-admin claim-org ORG INSTALLATION`. This trusted operation retains the roster, revision history and operation receipts. Re-enroll active employees against their recorded tenures. Explicit ownership resolves ambiguous historical names without inferring authority from bearer tokens. Preserve the prior backup for rollback.
