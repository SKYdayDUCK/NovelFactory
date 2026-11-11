# Agent Workshop

Independent stdio MCP application for organization management. Run from this directory with Python 3.12 and a separately provisioned PostgreSQL 17 database. The role running a model may only access the broker's pipes. Only the trusted broker OS identity may read its binding or database credentials. Restrict CONNECT on both service databases to their respective users. The Workshop process does not read a NovelFactory DSN.

    uv sync --locked
    export AW_DATABASE_URL='<private Workshop PostgreSQL DSN>'
    uv run alembic upgrade head
    uv run aw-admin install my-installation my-nf-instance
    uv run aw-admin enroll my-installation my-org manager /private/aw.json --revision 1
    uv run aw-mcp --binding /private/aw.json

The deployment operator creates private bindings (at most 168 hours). Revocation takes effect at the next database transaction, including on existing stdio sessions. Stop the service by closing stdin. Revoke with `uv run aw-admin revoke my-installation my-org manager`. Use libpq TLS settings and separate OS identities on non-test hosts.

The application protocol is `aw-nf.v1`. Call `aw_capabilities` after MCP initialize to negotiate; unsupported versions fail closed. Request identity comes from the binding. The argument limit is 32 KiB, the wire record limit 256 KiB and the maximum negotiated message TTL 24 hours. List actual tools on each connection. A catalog hash grants no capabilities.

Run `uv run pytest -q` against an isolated migrated database. To verify the NF catalog, run `python scripts/freeze_nf_catalog.py <clean-NF-checkout>` at the revision in `contracts/nf-catalog.json` and compare JSON. Restore backups to new isolated databases before changing any running bindings. No deployment command here sends a model request.
