# Original capacity gate executions

The 200 overload offered-call target remains 1000/s and its original minimum 950/s. 1000 open uses the actual header-start and requires all responses validated. P2 status counts include retries and other workflow phases. Each raw asset retains the original configuration, offered traffic, latency distributions, correctness, resource metrics and exit status. Read soak groups are separate repetitions.

| Arm | Scenario | Capacity | Business recovery | Wire starts/s | HTTP/transport outcomes |
| --- | --- | --- | --- | --- | --- |
| at30 | 10-mixed-1 | PASS | PASS | — | {'200': 58} |
| at30 | 1000-open | NOT_MET | — | 41.95 | {'429': 3627, '200': 1319, 'transport_error': 54} |
| at30 | 200-overload-1 | NOT_MET | PASS | 40.08 | {'200': 2114, '409': 19, '429': 849} |
| at30 | 50-mixed-1 | PASS | PASS | — | {'200': 282, '409': 4} |
| at30 | 50-overload-1 | NOT_MET | PASS | 58.37 | {'200': 532, '409': 4, '429': 48} |
| at30 | 500-mixed-1 | PASS | PASS | — | {'200': 2802, '409': 49} |
| at30-candidate | 1000-open | NOT_MET | — | 38.26 | {'200': 1434, '429': 3534, 'transport_error': 32} |
| at30-candidate | 200-overload-1 | NOT_MET | PASS | 27.80 | {'200': 2092, '409': 19, '429': 840} |
| at30-candidate | 50-overload-1 | NOT_MET | PASS | 77.52 | {'200': 532, '409': 4, '429': 137} |
