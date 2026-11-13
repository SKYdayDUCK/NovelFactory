# Durable backlog size and admission cost

The [preregistered driver](ADMISSION_DEPTH_PLAN.md) ran from clean, detached commit `e9c930ab1d05` on Linux, PostgreSQL 17.11 (Debian 17.11-0+deb13u1), in two independently migrated databases. The experiment used the ordinary authorized kernel task-create entry point, two works, sixteen owners, four worker threads and four PostgreSQL connections per arm. Only one arm filled at a time. Every bulk create used its own short committed transaction. Each depth check verified the authoritative count and eight new create/commit probes per arm in interleaved A/B/B/A groups. Probe cancellation restored the live depth. [All per-task timings, plans, byte sizes and RSS snapshots](raw/admission-depth.json.gz) are retained.

| Live backlog | Policy disabled median commit ms | Enabled median commit ms | Occupancy SQL EXPLAIN execution ms | Shared-hit blocks | Fill time from previous depth s, disabled / enabled |
|---:|---:|---:|---:|---:|---:|
| 0 | 19.72 | 21.30 | 0.295 | 3 | 0.00 / 0.00 |
| 50 | 20.12 | 24.90 | 0.907 | 8 | 0.80 / 1.07 |
| 200 | 17.48 | 19.74 | 0.596 | 14 | 2.11 / 2.64 |
| 500 | 25.86 | 24.25 | 1.574 | 21 | 5.01 / 5.93 |
| 1000 | 14.20 | 24.95 | 1.455 | 36 | 8.37 / 10.01 |
| 2000 | 22.13 | 26.70 | 3.187 | 70 | 15.14 / 23.28 |
| 5000 | 27.83 | 36.80 | 11.476 | 164 | 49.89 / 70.01 |

**5,000 READY tasks in each arm.** Probes left 56 terminal task records per database, including the eight already present at the depth-zero snapshot. Total database file growth from the zero-depth snapshot was 12,853,248 bytes (12.26 MiB) with the policy disabled and 14,737,408 bytes (14.05 MiB) enabled. The measured difference was 1,884,160 bytes (1.80 MiB), approximately 377 incremental bytes per live task for this TaskSpec and mix. Database size includes catalogs and default allocated pages; PostgreSQL WAL and external object storage are outside this measure. Live row counts also include pre-existing installation metadata.

| Changed relation (indexes and TOAST included) | File growth bytes, disabled | File growth bytes, enabled |
|---|---:|---:|
| nf_task_inputs | 5,488,640 | 5,513,216 |
| nf_task_commands | 2,154,496 | 3,096,576 |
| nf_outbox | 2,465,792 | 2,498,560 |
| nf_tasks | 1,638,400 | 1,605,632 |
| nf_bus_publications | 901,120 | 901,120 |
| nf_durable_admissions | 0 | 761,856 |
| nf_durable_admission_policy | 0 | 73,728 |

The bulk driver held both result sets in a single process and retained raw observations. Its live RSS at the depth-zero checkpoints was 86.4 / 86.4 MiB and at 5,000 was 96.9 / 96.9 MiB. At 5,000, individual PostgreSQL connection backends ranged from 39.8–40.5 MiB disabled and 40.5–42.7 MiB enabled. Each backend's RSS includes shared pages. Per-backend values cannot be summed into unique queue memory. The separate mixed-transport 5,000-submission benchmark records its combined client/server RSS with 20 logical client lanes.

The scan counts occupancy in one set-based aggregate. The growing buffer-hit count shows that the enabled per-admission cost rises with backlog. A partial index on live state would remove little work when nearly every row is live. Replacing the scan with shared counters requires tracking terminal transitions from all writers and restoring or rebuilding counts after restore, activation, and role-resource changes. The experiment does not validate that replacement or a production write speedup. The policy stays opt-in. Turning it off for new work preserves receipts and active first-start deadline checks on existing admissions.

Reproduce with a disposable PostgreSQL 17 administration database:

```sh
python -m benchmarks.hera_admission_depth --output /tmp/admission-depth.json.gz
```
