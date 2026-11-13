# Independent Workshop and NF runs

Frozen product commit `95c71585ca5390cf621976e1b61393384151158c`. The Linux environment used locked Python packages, separate native PostgreSQL 17.11 clusters, separate service logins with no administrative privileges, real MCP SDK stdio and native Redis 8.0.2. CI configuration in `.github/workflows/agent-workshop.yml` lists the corresponding suites and required historical objects. The local execution used native binaries instead of Docker services.

| Result file | Scope | Outcome |
| --- | --- | --- |
| [workshop.xml](workshop.xml) | Complete standalone Workshop suite, including role/organization, assignment, tool, ledger and Redis coverage | 119 passed. One Docker daemon restart case skipped because the environment has no Docker daemon. |
| [bridge.xml](bridge.xml) | Independent NF and Workshop authorities, stdio identities and service database isolation | 3 passed |
| [historical.xml](historical.xml), [legacy-w3.xml](legacy-w3.xml) | Independent W0–W6 releases, including the two archived W6 rehearsal objects fetched by their exact SHA | 21 + 1 passed |
| [dual.xml](dual.xml) | Multi-service runtime skills, candidate/reviewer separation, recovery, receipt reconciliation, lost or repeated delivery, manager resume, PostgreSQL archive replay | 19 passed |

The native Redis replacement exercise rebuilt both notification streams from PG, then compared their logical event identifiers. The paired physical recovery tests compared independent replay endpoints and exercised mismatched restore positions. The provider was a controlled fixture. [Redis replacement](streams.json) · [Workshop offline](offline.json) · [Paired restore](paired.json) · [Independent PITR](pitr.json)
