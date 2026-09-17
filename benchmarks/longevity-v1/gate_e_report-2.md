**P-SOAK2 post-fix, eed91c0**

| check | verdict | detail |
|---|---|---|
| deterministic final state (verify() clean + replay digest equality) | PASS | verify_healthy=True digest_equal=True |
| bounded metadata growth (manifest bytes slope) | FAIL | manifests 3.141 B/s, segments 2,396.455 B/s |
| no unbounded memory (RSS slope) | FAIL | first-vs-last 56.275 kB/s; within-life median 27.965 kB/s (gate uses the within-life figure) |
| throughput/latency drift (first hour vs. last hour) | FAIL | throughput 39.910 -> 19.117 commits/s, p99 60.420 ms -> 82.075 ms |
| compaction stalls (max reader p99 during a compaction window) | info | 0.000 ms |
| errors observed | FLAG | 150477128 |
| recoveries / reader restarts | info | 3 / 0 |
