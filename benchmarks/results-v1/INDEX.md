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

Plan §26 asks for parquet; JSON was chosen instead: the records are small
(&lt;400 KB total), diffable, and dependency-free to read.
