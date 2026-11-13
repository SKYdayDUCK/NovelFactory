# Independent compact tool and audit regression

Reviewed the audit/outbox and bound-tool changes through 8eb4078011ba34ccc03b10ce9926ec080ddca767. The grant-scoped outer join preserves the same live task/grant filter, task role, actor, frozen input generation, trace and volume as the previous lookup for provisionable bindings. The foreign-task case leaves only the calling grant scope. The compound SQL writes the event and wakeup in one transaction. A preexisting Outbox identity still runs the existing content and idempotency check.

Seven real PostgreSQL tests passed in the core suite, comparing compact versus ordinary audit fields and envelopes, stale generation, foreign scope, a conflicting Outbox identity, MCP/CLI and live revocation. A separate test using two PostgreSQL databases with distinct logins and the real Workshop stdio server restricted a dynamic grant to context reading. The direct task read was denied. Malformed tool arguments on a live authorized binding produced INVALID_ARGUMENT, and Workshop revocation denied both subsequent calls. Every invocation recorded one start and one completion with matching Outbox envelopes.

Both initial attempts used an incomplete local package installation. Sanitized MCP subprocesses could not import agent_workshop.server or novel_factory.interfaces. The original JUnit failures remain in this directory. Installing both checked-out source paths in an isolated Python 3.12 environment restored the subprocess imports. The passing XMLs record the reruns. Use the checkout's NF and Workshop packages installed in the same environment as the real stdio children.

```sh
python -m pytest -q tests/integration/test_hera_compact_role_authority.py \
  tests/integration/test_hera_fused_status.py tests/integration/test_bound_tool_entrypoints.py
python -m pytest -q workshop/integration/test_dynamic_late_delivery.py::test_compact_mcp_reads_recheck_workshop_tool_ceiling_and_revocation
```

Configure NF_TEST_DATABASE_URL and AW_DUAL_TEST_ADMIN_URL to disposable PG17, and AW_NF_SOURCE to the checkout root.
