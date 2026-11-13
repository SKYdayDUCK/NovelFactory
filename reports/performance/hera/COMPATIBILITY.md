# Compatibility and rollback

| Surface | Verification and applicable boundary |
|---|---|
| Original role-bound stdio | The compact-role option and old argument path both ran on a real child process. Live grant revocation, different roles and forged capabilities retain their errors. [Ported tests](raw/port.xml) |
| Remote SDK | The project lock pins Python mcp 1.29.0. The real clients negotiate 2025-11-25. Responses use ordinary tool contracts. Optional host-private rosters control exposed bindings. |
| REST task create | The existing route returns its previous HTTP status and raw receipt. HERA submit returns HTTP 202 after commit; old and new receipt readers refer to the same TaskService command/ID. |
| Raw tasks / preauthorized model jobs | Ordinary TaskSpec is eligible for an external authorized worker. The installation delegation contract atomically installs a supervisor job. A repeated, authorized submission across protocols does not add a second job or attempt. |
| Project status/resume | Both protocols require the existing narrow branch-bound resume grant. An optional terminal receipt scope is checked through the same application method on REST, stdio and remote MCP. |
| Existing workflow | Original dependencies, AND/OR predicates, generation fencing, scoped task/results access, lease recovery, provider debt and version authority remain the underlying state. There is no new independent world or task store. |
| Workshop | Original cold stdio bindings and optional reuse are covered. Opt-in group notification delivery revalidates the database envelope and Inbox. [Recovery results](FAILURE_RECOVERY.md) |
| Rollback | Disable optional API mounts, SQL read optimization, branch counters, grouping, class limits and dynamic sizing per their design docs. Historic receipts and results remain readable. Follow the migration's outstanding-work checks before attempting a schema downgrade. |

Run `python -m pytest tests/service/test_hera_mixed_ingress.py tests/integration/test_hera_durable_admission.py tests/integration/test_hera_progress.py tests/integration/test_hera_supervisor.py tests/integration/test_http_fenced_task_status.py -q` with the locked environment and isolated PostgreSQL 17. The existing native CI workflow includes HERA tests on Windows PostgreSQL. See [final acceptance](FINAL_ACCEPTANCE.md) for actual results and CI status.
