**xzgpu 24h soak, commit 886805f (pre torn-tail-reader fix), 2026-09-14/15**

| check | verdict | detail |
|---|---|---|
| deterministic final state (verify() clean + replay digest equality) | FAIL | verify_healthy=True digest_equal=False |
| bounded metadata growth (manifest bytes slope) | PASS | manifests -3.384 B/s, segments 1,341.111 B/s |
| no unbounded memory (RSS slope) | FAIL | 111.639 kB/s |
| throughput/latency drift (first hour vs. last hour) | PASS | throughput 24.030 -> 16.412 commits/s, p99 485.579 ms -> 3,740.529 ms |
| compaction stalls (max reader p99 during a compaction window) | info | 0.000 ms |
| errors observed | FLAG | 1 |
| recoveries / reader restarts | info | 41 / 2 |
