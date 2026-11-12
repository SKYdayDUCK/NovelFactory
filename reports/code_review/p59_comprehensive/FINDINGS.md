# Findings after repair

| ID | Level | Frozen source | Impact |
|---|---|---|---|
| P59-CORE-01 | P1 | `src/novel_factory/application/queries.py:348` | Incorrect historical and future query results consumed by planning and dependency collection. No unauthorized role read or world write observed. |
| P59-RT-01 | P2 | `agent_runtime/providers.py:72` | Bad configuration can abort construction of the entire adapter registry outside the stable ProfileError/DomainError contract. No remote call. |
| P59-PIPE-01 | P2 | `src/novel_factory/application/production_pipeline.py:225` | Incomplete operator-facing accounting trace; authoritative reservations and billing rows retained. |
| P59-RT-02 | P2 | `agent_runtime/routing.py:37` | CPU cost isolated to pure policy resolution; end-to-end throughput attribution remains unestablished. |
| P59-DB-01 | P2 | `src/novel_factory/infrastructure/repository.py:73` | Database round trips in the serialized portion of a world commit. |
| P59-VALID-01 | P3 | `validation/validate_package.py:380` | A developer can accidentally commit a regenerated historical report and misread the scope of validation. |
| P59-CI-01 | P2 | `.github/workflows/agent-workshop.yml:232` | The clean historical upgrade job cannot reconstruct two real old releases. |

All seven passed their focused verification. Historical AT-30 and long-history pagination are separate pre-existing items. The retained rejected stale-search hypothesis has no finding ID. See [structured records](FINDINGS.json).
