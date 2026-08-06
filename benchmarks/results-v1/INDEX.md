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
- bench-corrections-full-d073.json — the same matrix at release scale on
  **xzgpu** at `4481997` (100.8 min, 20k entities over a 1M-event base,
  replay timed). Resolves the two axes the ci profile could not: the
  batch-size curve is **U-shaped** with an optimum at 100–1,000 and a 5–9x
  regression at 10,000 (p50 commit latency 24.7 s there), spread 45–123x
  across a density row; and per-identity history depth costs **18.8x** from
  depth 1 to 1,000, linear past a knee at ~100. Manifests are 99.6% of a
  batch-1 store at every density. 8 capped cells flagged `truncated`; the
  manifest records `dirty: true` for the one uncommitted file, the harness
  itself — see docs/bench_corrections.md
- eval-1m-bitemporal-d081.json — the §13 sweep re-run on **xzgpu** at
  `f1498e4`, after the D-076…D-081 write-path arc. Replay **reproduces**
  the closecache numbers within 0.4–5% at every density (362.6 vs 361.1 s
  at 20%) — a control across four engine changes, and the run that split a
  review's staleness verdict in half (D-082): the matrix's 2.2× replay gain
  (bench-corrections-full-d079) is a different, many-small-commits log
  shape. What did change: cold-process time-to-first-query 198 → **0.48 s**
  at 20% density (0.29 s at density 0) and probe peak memory **−70%**,
  which staled the published warm-up facts instead of the replay ones.
  Hash gates green at every density.
- eval-durability-injection.json — injected crashes at ten write-path
  boundaries (D-086): real aborts mid-commit via TGMS_CRASH_POINT plus
  harness-side WAL tears, 3 trials each, four machine-checked questions
  per trial. First run: 9/10 clean and one real finding at the forecast
  boundary — a torn final WAL record made the store refuse to open.
  After EventLog.trim_torn_tail (tests-first): **30/30 trials clean**.
- eval-xtdb-footprints-1m.json — the XTDB cells for normalized resource
  reporting (D-085): container RSS after a warm query pass **3.78 GB**,
  after a cold boot + one query 935 MB; stop/start cold boot **12.66 s to
  pgwire-ready** + 85 ms first query; store 750.8 MB (reproduces the
  record receipt); replay 393 s (within 4.4% of the record run). Feeds
  eval_resources.md §19's three-footprint table alongside the D-082
  coldwarm cells for native and DuckDB.
- eval-xtdb-1m-{5,20}-final.json — the first semantic competitor
  (D-083/D-084): XTDB 2 via pgwire, **op-level** replay of the reference
  log on **xzgpu** at `a340d6e`, 1M events at 5% and 20% correction
  density. **400 believed-state probes, zero disagreements** — XTDB's own
  SQL:2011 supersession reproduces our belief semantics at every probe.
  Point ops 28–389× native, ingest 3.9–4.7×, diff 2.4×, storage 23–27×;
  both systems flat in density. Two earlier receipts
  (xtdb-1m-{5,20}.json, kept on the host) carried a non-idiomatic native
  S4 and are superseded. Forecast scored per cell in docs/eval_xtdb.md:
  2 of 8 confirmed, and every miss but one erred against our own engine.
- eval-resources-coldwarm-{1m,10m}-d082.json — the §15 cold/warm sweep
  re-run on **xzgpu** at `f1498e4`, the protocol-matched refresh for the
  published warm-up facts. Native first-query in a fresh process: 2.4–2.9 →
  **0.29–0.62 s** at 1M, 27–32 → **3.1–8.0 s** at 10M (5–9× per query,
  both cache states). At 1M native cold start is now at **parity with
  DuckDB** (0.8–1.6×); at 10M the gap is 1.2–8.8×, down from the published
  "5–70×". Native/duckdb hashes agree within the run at both scales;
  cross-run hashes differ by construction (independent builds, D-023).
- eval-10m-bitemporal-d081.json — §13 at 10M, 0.1% density, **xzgpu** at
  `f1498e4`. Against the `8136ecb` receipt (which predates even the D-072
  close cache, so this delta spans D-041 + D-072 + the D-076…D-081 arc):
  suite VmHWM **6,108 → 2,428 MB (−60%)**, first query 42.6 → **3.3 s**,
  replay 290 → 261 s, and the old run's `agree:false` is now `agree:true`.
  The floor number feeding the 100M costing was corrected from this receipt
  plus eval_resources §18 (D-082) — and `open_rows`' addition (the D-076
  index) is invisible inside the reduction, answering the review's memory
  question.
- bench-corrections-full-d079.json — the same matrix on **xzgpu** at
  `d5620bf`, after the open-version index (D-076), the segment-name cache
  (D-077) and the close-index fold (D-079). 89.2 min. Per-identity depth is
  now **flat — 1.18x across depths 1 to 1,000** with seed batches held fixed,
  the axis that opened the session at a claimed 18.8x. Best per-correction
  cost 0.355 -> 0.230 ms at 20% density, and the optimum batch size moves to
  100 at every density. Replay at 20% density 296.81 -> 137.50 s (2.2x).
  Batch 10,000 regressed 8-10%, which is commit-latency overhead from the
  index's per-row maintenance and is the honest cost of the trade — see
  docs/bench_corrections.md

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
