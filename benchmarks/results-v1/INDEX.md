# Raw measurement records (results-v1)

JSON records written by `scripts/eval_harness.py` / `eval_writes.py` on
the measurement host; each carries its manifest (commit, host, protocol)
and per-query raw timings. Kept verbatim, including superseded runs —
supersessions are part of the record:

- eval-200k{,-4sys,-5sys,-graphs}.json — 200k as the matrix grew
- eval-1m{,-4sys}.json, eval-1m-gc.json — 1M sweeps
- eval-10m{,-4sys}.json — 10M sweeps (cluster-merge era in -4sys)
- eval-collegemsg{,-costfix}.json — real-dataset runs
- eval-200k-costfix.json — motif cost-model reprice rerun
- eval-writes.json — write-path evaluation (§12)
- eval-10m-d047.json, eval-1m-d047.json — the D-047 refresh: series metrics
  routed through the aggregation kernel and count_distinct on bitsets
  (series.count at 10M 183.7 -> 84.7 ms, agg.rel_bucket 331.1 -> 96.1)
- eval-200k-6sys.json — 200k, all six systems in one run, thirteen queries
  (D-044): the first record covering the whole matrix at once
- eval-1m-agg.json, eval-10m-agg.json — 1M (four systems) and 10M (three)
  with the grouped-aggregation query
- gate-1m-{control,parallel}.json — the parallel-gate A/B at 1M
  (`TGMS_PARALLEL_MIN_ROWS` default vs 250k): a **null result**, kept
  because it is what ruled the gate out as the cause of the 1M scan
  regression (D-045)
- bisect-{47379fc,c56ebbb,e5756a9,468ee32,2601d1a}.json — five 1M
  native-only runs, one per candidate commit, locating that regression at
  the TCSR-persistence commit; the cause turned out to be index residency
  rather than anything in the diff (engine_lessons §9g)
- eval-1m-bitemporal.json — §13 current-vs-bi-temporal overhead, density
  sweep 0–20% (its `agree:false` is the run gate miscounting a one-sided
  guardrail refusal at 20%; zero hash mismatches — see eval_bitemporal.md)
- eval-10m-bitemporal.json — §13 at 10M, 0.1% density (`agree:false` is
  the same gate miscount, there on the two-sided pre-reprice guardrail
  refusals of paths.k and reach.window; zero hash mismatches)
- eval-1m-bitemporal-{prefix5,postfix5,closecache5}.json — 1M/5% A/B
  receipts for the correction-replay fixes (dev M-series host, **not**
  xzgpu): before / after the WP-N4 postings `close_version`, and after
  the per-generation `close_index` cache — see eval_bitemporal.md
- eval-1m-bitemporal-confirm5.json — independent 1M/5% rerun at commit
  96ea135 (same host): replay 41.6 s, hash gate green — reproduces the
  closecache5 receipt within noise
- eval-1m-bitemporal-closecache.json — §13 full density sweep rerun on
  **xzgpu** at 96ea135, both correction-replay fixes in: replay linear
  in corrections (~1.6 ms each), 2,856.8 s → 361.1 s at 20%; hash gates
  green at every density, motif.filtered's one-sided 20% refusal
  recorded as `partial` — see eval_bitemporal.md
- eval-200k-bitemporal-d072.json — 200k density sweep at HEAD `4481997`
  (dev M-series host), re-confirming the correction path is linear:
  0.65 / 0.96 / 0.84 ms per correction at 1/5/20%, hash gates green at
  every density. Run because the **pre-fix** figures were still being
  quoted as current in the report and handoff four days after the fix
  landed (D-072) — the receipt exists to close a record defect, not an
  engine one
- bench-corrections-ci-d073.json — the correction-density matrix at its
  `ci` profile (`scripts/bench_corrections.py`), HEAD `4481997`, dev
  M-series host: density × batch size as a grid, plus versions-per-identity
  and out-of-order distance. The finding is on the axis §13 never swept —
  batch size. One correction per commit costs **~100x** the same work at
  batch 100, and leaves the store **93.9% manifest bytes**. Capped cells
  are flagged `truncated` in the record — see docs/bench_corrections.md

- eval-resources-threads-{1m,10m}.json — §14.3 thread-scaling curves
  (native via TGMS_SCAN_THREADS, DuckDB via SET threads; 1–32 workers;
  result hashes verified identical across every width)
- eval-resources-coldwarm-{1m,10m}.json — §15 warm / process-cold /
  fully-cold states, eviction by user-space posix_fadvise(DONTNEED)
  (no root on the host); 5 single-shot trials per cold cell
- eval-resources-readers-{1m,10m}.json — §14.4 reader concurrency,
  1–16 processes over one store in barrier-aligned windows; the 10M
  n=16 row records 2 readers OOM-killed by the host (rc −9), which is
  the finding, not a harness failure
- eval-resources-memcap-10m.json — §14.2 whole-suite Docker memory caps
  (cgroup-v1 rootful; --memory enforced, swap-limit unsupported):
  8g parity, 4g and 2g OOM-killed against a 5.9 GB suite VmHWM
- eval-resources-memcap-10m-perquery.json — §14.2 one container per
  query, attributing the suite OOMs to individual queries

- eval-resources-threads-recal-{1m,10m}.json — §17 rerun of the thread
  sweep after the row-based parallel-gate recalibration (native only):
  1M flat at serial latency at every width (the 2–16-thread regression
  gone), 10M unchanged where parallel pays (coactive 719 → 164 ms at
  t=16, 133 at t=32); hashes identical across every width
- eval-resources-memcap-budget-10m{,-perquery}.json — §18 capped rerun
  with the byte-budget segment cache (D-041, 768 MB budget) and the
  streamed statistics fold: the whole 10M suite completes under a 2 GB
  Docker cap at 1.76 GB VmHWM (previously OOM at 2 g and 4 g against a
  5.93 GB floor), per-query 2 g/4 g all green; `segment_cache` receipts
  embedded per suite
- eval-resources-memcap-smallbudget-10m.json — §18 the same suite with a
  deliberately under-sized 256 MB budget (below the store's ~794 MB
  decoded size): everything still answers under 2 g — the budget trades
  re-decode latency for memory, priced per query in the record

Plan §26 asks for parquet; JSON was chosen instead: the records are small
(&lt;400 KB total), diffable, and dependency-free to read.
