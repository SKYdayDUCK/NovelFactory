# World state, time and acceptance

`Kernel.prepare` (kernel.py:705) reads current version, immutable inputs and approvals. `_accept_transaction` (:836) resamples scope tokens and the qualified task before the authoritative append. Fork, compare and promote (:936 onward) operate on explicit branch identities. `Kernel.revision` (:85) checks the row's actual projection bytes even after a witness hit, then verifies scheduled state. The snapshot hash is independent of a mutable projection pointer.

`WorldQueryService._sample` (:159) freezes world, branch, plane and grant in one read transaction. It checks BELIEF actor and acquisition time and only loads event bodies for fact timelines. The delivery transaction rechecks the grant and task. `get_artifact` (kernel.py:355) gates delivery of referenced artifacts. The temporal answer still has a distinct correctness requirement: an entry can pass authorization while occurring on the wrong axis. P59-CORE-01 reproduces this through the public application query on real PG17.

Historical revision selection and concurrent promotion are covered by tests/queries/test_history_queries.py and the kernel integration suites. In cross-volume work a changed premise invalidates its descendants; a provisional plan still needs formal adoption. No new world corruption was observed in the axis probe.
