**P-STORM-HUNT, 57952fa**

| check | verdict | detail |
|---|---|---|
| deterministic final state (verify() clean + replay digest equality) | PASS | verify_healthy=True digest_equal=True |
| bounded metadata growth (manifest bytes slope) | FAIL | manifests 2.828 B/s, segments 2,360.596 B/s |
| no unbounded memory (RSS slope) | FAIL | first-vs-last 115.426 kB/s; within-life median 20.444 kB/s (gate uses the within-life figure) |
| throughput/latency drift (first hour vs. last hour) | PASS | throughput 24.646 -> 29.263 commits/s, p99 170.433 ms -> 84.784 ms |
| compaction stalls (max reader p99 during a compaction window) | info | 0.000 ms |
| errors observed | FLAG | 175 |
| recoveries / reader restarts | info | 0 / 0 |
