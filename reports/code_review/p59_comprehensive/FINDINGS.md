# Findings at the frozen baseline

| ID | Level | Source | State and consequence |
|---|---|---|---|
| P59-CORE-01 | P1 | `src/novel_factory/application/queries.py:348` | REPRODUCED. Incorrect historical and future query results consumed by planning and dependency collection. No unauthorized role read or world write observed. |
| P59-RT-01 | P2 | `agent_runtime/providers.py:72` | REPRODUCED. Bad configuration can abort construction of the entire adapter registry outside the stable ProfileError/DomainError contract. No remote call. |
| P59-PIPE-01 | P2 | `src/novel_factory/application/production_pipeline.py:225` | REPRODUCED. Incomplete operator-facing accounting trace; authoritative reservations and billing rows retained. |
| P59-RT-02 | P2 | `agent_runtime/routing.py:37` | PROFILED. CPU cost isolated to pure policy resolution; end-to-end throughput attribution remains unestablished. |

[Frozen observations and repair criteria](FINDINGS.json).
