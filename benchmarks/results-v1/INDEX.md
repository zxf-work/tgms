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

Plan §26 asks for parquet; JSON was chosen instead: the records are small
(&lt;400 KB total), diffable, and dependency-free to read.
