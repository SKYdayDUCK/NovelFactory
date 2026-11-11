# NF Bridge — verification on the public candidate

Source `695668b87f6a76acbe7e27c71fcb259b7b3f0795`, tree `788dc4b190299894509593a23982452f0362f9ef`, PR #11 base `main`. The parent `b0fac90f71903b7349ccd57e643ae145148180d0` includes the signed W2 database upgrade, scoped progress response, expiry at the spend boundary and both live service profiles.

[Bridge JUnit and WAL replay](bridge-695668b.zip) — 23 passed, no skips. PostgreSQL 17.11, both live MCP services, lost receipt, two restore positions, a single NF task and no external model calls. The pinned peer and its actual revision are inside the archive. SHA-256 `5f33263b9b755212d98fe7247a13570ffef7afa37fb117069e9da63d40542c65`.

[W0–W2 compatibility](w2-695668b.zip) — locally 44 original checks passed and one Docker-only test skipped. A separate native Redis 8.0.2 stop/restart passed. SHA-256 `4fc16993a9c2edfd31d4f559454b25628532109ca834ca3dc83c51b3b028eccc`. [Hosted Workshop and bridge job](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43081588087) exercises the original sidecar.

[Caller authority, recovery projections and SQL observation](authority-and-sql.zip) — 37 repository checks and seven independent checks passed. The three projection reads, the resource sample and the queue sample did not enter the new delegated-spend SQL check. The measured ordinary model execution did enter it at reservation and dispatch. These observations describe query reachability, not throughput. The private recovery snapshot retained the six accounting fields and reconciled the original UNKNOWN.

[P1/P2 integrity](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43081588068) · [Physical PITR](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43081588092) · [P2 load](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43081588236) · [Scale](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43081588056) · [P3/P4](https://github.com/SKYdayDUCK/NovelFactory/actions/runs/43081588179)
