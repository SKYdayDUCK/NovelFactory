# Persistence and database access

NF and AW have independent migration histories and transaction boundaries. NF revisions, events, artifacts and receipts have immutable identities. The repository binds a revision read to work, branch and revision together. KernelRepository.ancestor uses a scoped recursive query. Query packets leave the repeatable-read transaction before answer formatting; a later eligibility check protects delivery. Object bytes and relational references have separate persistence and recovery checks.

KernelRepository.bump loops over sorted unique scope keys, issuing an INSERT … ON CONFLICT per key before reading scope_map. This is a measurement target inside the authoritative transaction. Replacing it requires preserving unique-key increments, stable lock ordering, the caller transaction, the full returned version map and rollback semantics. No schema migration is proposed from static inspection.

The authoritative pipeline ledger contains seven synthetic model Attempts in raw/pipeline-ledger.json. A status export exposed six; the discrepancy is in the application projection. WAL restore and paired restore evidence are recorded separately from load results.
