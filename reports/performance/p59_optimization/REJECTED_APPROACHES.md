# Rejected and deferred approaches

| Proposal | Observation that informs the decision |
| --- | --- |
| Attribute 429 to PostgreSQL and enlarge its connection pool | 716 authentication-queue rejections, 80 ingress rejections and 46 business rejections plus seven timeouts in the 200-overload run. The pool reached its eight-connection allowance. A pool-only increase changes the resource budget and leaves upstream queue/offer limits. |
| Claim capacity after one successful soak or domain-recovery gate | Historic 500-soak shows both successes and failures. A recovered command can follow several failed HTTP attempts. These gates retain independent status. |
| Raise or recalibrate the 950/s gate | The original P2 call-start gate and P3 wire-start gate remain part of Issue #13. Transport-only calibration identifies a driver/test-host limit and cannot approve production capacity. |
| Reuse Scion world caches across tenants/epochs | Immutable data may be shared only when current grants, pinned version, storage content witness and delivery dependencies are maintained. No candidate here passes such a new proof. Original PR #4 remains independent. |
| Share a Provider transport process across origins, or retry UNKNOWN on a different Provider | Synthetic fault trials show the lost reply can leave a billable UNKNOWN attempt; successful tasks on the healthy provider continue. Required isolation benefits outweigh an unmeasured connection reuse opportunity here. |
| Replace durable fairness with in-memory balancing | Four independently authorized project queues advanced evenly in the controlled single-slot experiment. No measured defect justifies losing durable ordering on restart. |
| Speed up contract checks by eliding authorization or output schemas | The observed CPU share is real. A valid replacement must prove equivalent output and mutation ownership for all supported inputs. Profiling alone establishes neither. |

The [measured validation alternatives](VALIDATION_EXPERIMENT.md) retain all schema checks. First-error evaluation helped rejected packets and varied by successful input size. Guarding native JSON to avoid a second copy slowed both whole-compile sizes. Unconditional elision broke mutable-leaf ownership for a valid Python caller. Both candidates were left out of the product.
