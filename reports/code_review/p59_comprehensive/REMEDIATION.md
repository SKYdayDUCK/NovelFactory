# Repair and rollback ledger

| Finding | Development commit on GitHub | Focused regression |
|---|---|---|
| CORE-01 | 2a26c686; abd89ea4 | Real PG17, temporal axes, fixed plan, BELIEF provenance, single/batch query and event-select counts |
| RT-01 | 4c15001a | Mixed valid/invalid operator catalog rejects both invalid ports with stable config error |
| PIPE-01 | c67db7f0 | Exported current and historical IDs checked against the seven authoritative fixture calls |
| RT-02 | 9ad1c18b; f890b141 | Selector ordering, override, invalid revised rules, paired comparison over real checked-in rules |
| DB-01 | 2cee68f8 | Same-transaction map, rollback, simultaneous reversed input, guarded accept and idempotent replay |
| VALID-01 | 5d777526 | Explicit output reaches selected path; archive remains intact |
| CI-01 | 4bce206b | Independent fetch of both historical SHAs and rerun of two previously failing upgrades |

Each product change can be reverted independently; all use the existing schemas. Revert the timeline event-read optimization before reverting the axis contract fix. Reverting the database batching returns to ordered row updates in the original transaction. Returning to the original SQL path needs no data rewrite. The performance dossier retains the adverse and unchanged results.
