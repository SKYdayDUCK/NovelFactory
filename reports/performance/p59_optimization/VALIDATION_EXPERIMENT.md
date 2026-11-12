# Contract validation and ownership experiment

All candidates ran inside the benchmark process. Each uses the same Draft 2020-12 schema, registry, format checker and generic error response as the baseline. FIRST_ERROR traverses until the first error and retains deepcopy. NATIVE_COPY retains the original complete error traversal and skips deepcopy only for an input tree made entirely of exact native JSON scalar/container types and string keys. Nested model DTOs and opaque Python values keep their original normalization and copying.

| Case and sample size | BASELINE ms | FIRST_ERROR ms | NATIVE_COPY ms |
| --- | ---: | ---: | ---: |
| full_compile-1 | 34.148 | 35.391 | 36.432 |
| full_compile-100 | 135.556 | 130.989 | 137.764 |
| valid_small-1 | 0.939 | 0.921 | 0.938 |
| valid_large-100 | 3.872 | 3.777 | 3.769 |
| invalid_early-100 | 3.261 | 0.323 | 3.100 |
| invalid_late-100 | 3.252 | 2.111 | 2.959 |

Service measurements used a 250-item PG17 world and one fixed cut. Nine rotated observations per arm per subject count produced identical facts, role, evidence permissions, token estimate, limitations and world identity. Each compile built and stored a new authorized task-bound context. The microbenchmark measured 15 repeats per arm per case, with only a first or last fact status changed for the invalid fixtures. All 58 valid and 31 invalid checked-in examples had matching results/codes/messages across all three arms.

Removing the final copy unconditionally introduces an alias. The published QueryAnswer schema leaves data unconstrained. A direct Python caller may supply bytearray under a VALUE answer. The baseline returns a detached bytearray, while returning plain(payload) references the caller's bytearray. Native nested dict/list mutation tests and opaque leaf identity tests passed with the guarded implementation.

**Decision.** Defer both changes. First-error validation reduced early and late rejection cost. The current performance target concerns successful authorized work and there is no measured prevailing load of invalid context bundles. On the single-subject successful service path, both candidates were slower, and the native guard was slower at 100 subjects too. The isolated run does not establish a broad successful-path improvement. Both validation paths still traverse the nested schema for valid inputs. See [raw samples](raw/validation-pair.json) and [replay harness](scripts/validation_pair.py).
