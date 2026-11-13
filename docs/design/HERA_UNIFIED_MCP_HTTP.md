# HERA task operations over stdio, remote MCP and REST

The existing Kernel and TaskService own task identity, world/branch checks, generation, grant, deadline, idempotency receipts and provider accounting. The service takes a trusted Principal and an optional operator-provisioned work/branch ceiling. The remote SDK adapter authenticates every POST, including initialization, and maps the resulting principal/grant pair to its installed binding. One stateless manager serves these bindings on the same Uvicorn application, SQLAlchemy pool and admission controllers as REST. An SDK request receives the runtime attached to its own ASGI request. The shared catalog contains static schemas only. Tool discovery and invocation independently evaluate the current binding and grant. Transport cancellation does not undo a committed submission.

| Ordinary MCP tool | REST endpoint | Result |
|---|---|---|
| nf_task_submit | POST /api/v1/hera/tasks | Durable task receipt after commit. REST returns 202 and MCP returns ordinary CallToolResult. Same TaskSpec and idempotency key replay the same receipt. |
| nf_task_status | GET /api/v1/tasks/{id} | Current authorized task metadata. |
| nf_task_cancel | POST /api/v1/tasks/cancel | Existing TaskService generation-checked cancellation receipt. |
| nf_task_events | GET /api/v1/tasks/{id}/events | Page of task.changed outbox events with a task-scoped cursor, 1–100 records. Only state, generation and identifiers are exposed. |
| nf_project_status | GET /api/v1/works/{work}/branches/{branch}/workflow | Branch state and status counts under a live, unexpired PROJECT_RESUME grant. |
| nf_resume_project | POST /api/v1/works/{work}/branches/{branch}/workflow/resume | Recheck existing bounded dependency and recovery work under the same branch authorization. |

The existing POST /api/v1/tasks retains its original receipt and 200 response. Role-bound stdio tools and role knowledge cuts keep their existing ceilings. Task operations are a separately installed binding. Remote role, project and controller bindings can also be installed. New task creation does not claim a worker or bypass a workflow dependency.

## Installation and runtime

Run the existing ASGI factory with its configured database and object store. To add remote MCP, set NF_MCP_BINDINGS_FILE to an owner-private JSON file. Each array entry is an installation record derived from a grant issued by the operator. Use the IDs returned when provisioning a grant. No client request chooses its own binding. Example file shape:

```json
[
  {
    "principal_id": "<issued principal UUID>",
    "grant_id": "<issued grant UUID>",
    "binding": {
      "mode": "task_operations",
      "work_id": "<work UUID>",
      "branch_id": "<branch UUID>"
    }
  }
]
```

Configure NF_MCP_ALLOWED_HOSTS and NF_ALLOWED_ORIGINS for the deployment. The MCP default accepts loopback hosts on explicit ports. Retain the usual TLS boundary for bearer tokens on remote networks. An installation requires the normal issued NF bearer credential when opening the SDK client. Restart the app after changing its roster. Live grant revocation takes effect in PostgreSQL. Use ASGI lifespan (Uvicorn auto/on). Without a roster, the server exposes its regular REST API. Existing stdio can select the same binding object inside its private configuration.

For bounded ingress bursts, tune NF_HTTP_MAX_INFLIGHT, NF_HTTP_MAX_QUEUED_INGRESS, NF_HTTP_MAX_QUEUED_OPERATIONS, NF_HTTP_MAX_QUEUED_PER_PRINCIPAL and NF_HTTP_QUEUE_TIMEOUT consistently with the global connection budget. Authentication has its own bounded lanes. MCP calls and REST requests consume the same rate limiter and operation slots. A full ingress budget yields a retryable error. Submission responses include a status URL, tool, poll interval and task wall deadline. A disconnect during acknowledgement can be retried with exactly the same idempotency key.

The lock pins Python mcp 1.29.0. Real clients negotiated 2025-11-25 and received ordinary SDK tool results. The official 2026-07-28 Tasks extension requires per-request client capability advertisement and supports tasks/get, tasks/update and tasks/cancel. The official Python SDK migration guide documents that its 2.x runtime also lacks this extension. Use the tool APIs above until the locked SDK and peer have negotiated a supported implementation. [Extension specification](https://tasks.extensions.modelcontextprotocol.io/specification/2026-07-28/tasks) · [Python SDK migration](https://py.sdk.modelcontextprotocol.io/migration/#experimental-tasks-support-removed)

## Automatically executed model tasks

Set NF_HERA_DELEGATIONS=1 to expose an installation's existing delegation service at /mcp/installations/{installation_uuid} and the matching REST endpoints /api/v1/hera/installations/{installation_uuid}/delegations/submit and /status. Their tools are nf_delegation_submit and nf_delegation_get. Use the installation credential and the exact previously provisioned operation/slot scope. On a successful submission, one transaction creates the task, installs its authorized Supervisor job and records the delegation receipt. A worker can then claim it after connection loss. Retry through either adapter with the same operation and content. The live installation/role grant, available budget, per-attempt accounting and Supervisor transport still apply. The ordinary TaskSpec submission serves callers that manage their own worker and claim lifecycle.
