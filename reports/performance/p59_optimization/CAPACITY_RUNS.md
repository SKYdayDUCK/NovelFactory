# Original capacity gate executions

The 200 overload offered-call target remains 1000/s and its original minimum 950/s. 1000 open uses the actual header-start and requires all responses validated. P2 status counts include retries and other workflow phases. Each raw asset retains the original configuration, offered traffic, latency distributions, correctness, resource metrics and exit status. Read soak groups are separate repetitions.

| Arm | Scenario | Capacity | Business recovery | Wire starts/s | HTTP/transport outcomes |
| --- | --- | --- | --- | --- | --- |
| at30 | 10-mixed-1 | PASS | PASS | — | {'200': 58} |
| at30 | 1000-open | NOT_MET | — | 41.95 | {'429': 3627, '200': 1319, 'transport_error': 54} |
| at30 | 200-mixed-1 | PASS | PASS | — | {'200': 1122, '409': 19} |
| at30 | 200-mixed-2 | PASS | PASS | — | {'200': 1122, '409': 19} |
| at30 | 200-overload-1 | NOT_MET | PASS | 40.08 | {'200': 2114, '409': 19, '429': 849} |
| at30 | 200-overload-2 | NOT_MET | PASS | 38.25 | {'200': 2119, '409': 19, '429': 845} |
| at30 | 50-mixed-1 | PASS | PASS | — | {'200': 282, '409': 4} |
| at30 | 50-mixed-2 | PASS | PASS | — | {'200': 282, '409': 4} |
| at30 | 50-overload-1 | NOT_MET | PASS | 58.37 | {'200': 532, '409': 4, '429': 48} |
| at30 | 50-overload-2 | NOT_MET | PASS | 74.44 | {'200': 532, '409': 4, '429': 45} |
| at30 | 500-mixed-1 | PASS | PASS | — | {'200': 2802, '409': 49} |
| at30 | 500-soak | PASS | — | — | {'200': 12000} |
| at30-candidate | 1000-open | NOT_MET | — | 38.26 | {'200': 1434, '429': 3534, 'transport_error': 32} |
| at30-candidate | 200-overload-1 | NOT_MET | PASS | 27.80 | {'200': 2092, '409': 19, '429': 840} |
| at30-candidate | 50-overload-1 | NOT_MET | PASS | 77.52 | {'200': 532, '409': 4, '429': 137} |
| at30-independent | 500-soak | PASS | — | — | {'200': 12000} |
| at30-replica | 200-mixed-1 | PASS | PASS | — | {'200': 1122, '409': 19} |
| at30-replica | 200-mixed-2 | PASS | PASS | — | {'200': 1122, '409': 19} |
| at30-replica | 200-overload-2 | NOT_MET | PASS | 34.31 | {'200': 2113, '409': 19, '429': 861} |
| at30-replica | 50-mixed-2 | PASS | PASS | — | {'200': 282, '409': 4} |
| at30-replica | 50-overload-2 | NOT_MET | PASS | 49.44 | {'200': 532, '409': 4, '429': 103} |

## Latency, useful work and resource use

P2 uses all HTTP responses across phases; P3 uses all attempted responses, including transport failures. P2 useful throughput excludes successful duplicate receipts. The read-only P3 value is validated query throughput. RSS is the sum of per-worker high-water marks. The database column counts application cursor executions. P2 contention retries are counted from actual HTTP completions minus distinct completed logical operations.

| Arm / scenario | P50/P95/P99 ms | Useful/s | Commits | Retries | SQL | Peak pool | CPU s | Peak RSS KiB |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| at30/10-mixed-1 | 73.4/138.6/165.1 | 9.40 | 2 | 0 | 1519 | 2 | 1.6 | 83048 |
| at30/1000-open | 2885.5/8812.4/11336.0 | 10.88 | 0 | 0 | 24704 | 8 | 110.3 | 297236 |
| at30/200-mixed-1 | 76.5/221.4/416.9 | 7.91 | 21 | 0 | 29658 | 7 | 47.0 | 118640 |
| at30/200-mixed-2 | 72.0/209.7/361.6 | 7.91 | 21 | 0 | 29658 | 5 | 46.9 | 114868 |
| at30/200-overload-1 | 1121.8/6690.1/10680.8 | 12.04 | 21 | 841 | 46575 | 8 | 72.1 | 136784 |
| at30/200-overload-2 | 1152.9/12624.4/18126.5 | 12.29 | 21 | 842 | 46706 | 8 | 70.1 | 136108 |
| at30/50-mixed-1 | 59.8/133.9/202.7 | 8.12 | 6 | 0 | 7443 | 3 | 8.4 | 90096 |
| at30/50-mixed-2 | 41.2/111.9/168.3 | 8.14 | 6 | 0 | 7443 | 2 | 7.3 | 89384 |
| at30/50-overload-1 | 182.4/4433.8/5432.5 | 13.15 | 6 | 48 | 11693 | 8 | 16.2 | 110060 |
| at30/50-overload-2 | 96.0/3125.3/3798.4 | 13.62 | 6 | 45 | 11693 | 8 | 11.7 | 108592 |
| at30/500-mixed-1 | 179.8/846.4/1164.4 | 7.85 | 51 | 0 | 74088 | 8 | 213.2 | 291648 |
| at30/500-soak | 130.1/649.2/996.2 | 20.00 | 0 | 0 | 205800 | 8 | 606.0 | 228732 |
| at30-candidate/1000-open | 2920.2/8224.6/11531.5 | 10.66 | 0 | 0 | 26896 | 8 | 128.3 | 299368 |
| at30-candidate/200-overload-1 | 1629.5/12047.6/18888.9 | 11.35 | 21 | 810 | 46164 | 8 | 83.9 | 139220 |
| at30-candidate/50-overload-1 | 210.9/2475.5/3128.7 | 13.35 | 6 | 137 | 11690 | 8 | 15.8 | 112580 |
| at30-independent/500-soak | 199.7/1071.4/1519.1 | 20.00 | 0 | 0 | 205800 | 8 | 628.6 | 230292 |
| at30-replica/200-mixed-1 | 81.5/385.7/671.9 | 7.91 | 21 | 0 | 29658 | 8 | 54.1 | 129460 |
| at30-replica/200-mixed-2 | 64.7/291.8/777.6 | 7.89 | 21 | 0 | 29658 | 5 | 49.8 | 115260 |
| at30-replica/200-overload-2 | 1136.4/6849.0/11632.3 | 12.03 | 21 | 852 | 46544 | 8 | 76.2 | 138740 |
| at30-replica/50-mixed-2 | 83.7/695.3/861.8 | 8.06 | 6 | 0 | 7443 | 8 | 14.5 | 106208 |
| at30-replica/50-overload-2 | 172.3/3305.0/4257.4 | 13.13 | 6 | 103 | 11693 | 8 | 14.3 | 110720 |
