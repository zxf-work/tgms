# Resource axes: threads, cache state, memory ceiling, readers

What do the published timings assume about the machine? Every table so
far was warm, single-client, uncapped, and free to use 16 scan threads.
This answers the evaluation plan's resource sections with four controlled
sweeps over the same stores: thread scaling (§14.3), cold versus warm
cache (§15), working set versus RAM (§14.2), and reader concurrency
(§14.4).

## Receipts (spec §8.4)

- commit `6fb7cb1` (clean), branch `eval-resources`; the per-query memory
  sweep at `e644503` (same tree plus the `--per-query` harness flag). Raw
  records: `benchmarks/results-v1/eval-resources-{threads,coldwarm,readers}-{1m,10m}.json`,
  `eval-resources-memcap-10m.json`, `eval-resources-memcap-10m-perquery.json`
- host: xzgpu — 40 cores, 93 GB RAM, 8 GB swap, Linux 5.4.0-216-generic
  x86_64; same host as every published table; nothing else running
- protocol: plan §16.3 — 5 warmups, 30 measured reps per sub-second query
  (10 for slower), median reported here, p95 and raw timings in the JSON.
  Per-section deviations are stated where they occur: cold states are
  single-shot by definition (5 trials, median-of-firsts); readers run
  duration-based windows; capped suites use reduced repetitions.
- datasets: the phase-0 generator's reference logs at 1M and 10M events
  (constant average degree, ~0.5% built-in corrections), replayed once
  per backend (D-023); every mode measures the same store bytes. Native
  store: 25 MB at 1M, 268 MB at 10M on disk.
- harness `scripts/eval_resources.py`; sequence `scripts/run_resources.sh`
  (log `runs/resources-20260801.log`, `RUN_FINISHED exit=0`)

## §14.3 Thread scaling

`TGMS_SCAN_THREADS` (added for this measurement) overrides the engine's
scan-stage worker count, default `available_parallelism().min(16)`. Each
point runs in a fresh subprocess with the variable inherited from the
parent, on the scan-heavy registry queries. The DuckDB column is the
same store under `SET threads = N` — its knob is one flag away, so the
comparison is included.

**Result hashes agreed across every thread count on both backends and
both scales** — the engine's claim that parallel scan output is
byte-identical to the serial loop by construction was measured here, not
assumed.

### 10M events (p50 ms, native → duckdb)

| threads | series.count | coactive.narrow | motif.filtered |
|---|---|---|---|
| 1 | 596 → 2,422 | 699 → 2,291 | 205 → 1,087 |
| 2 | 637 → 1,826 | 783 → 1,202 | 240 → 597 |
| 4 | 561 → 1,767 | 490 → 713 | 155 → 406 |
| 8 | 513 → 1,532 | 298 → 428 | 106 → 275 |
| 16 (both defaults) | 472 → 1,332 | 196 → 255 | 74 → 183 |
| 32 | 466 → 1,336 | 162 → 230 | 76 → 171 |

### 1M events (p50 ms, native)

| threads | series.count | coactive.narrow | motif.filtered |
|---|---|---|---|
| 1 | 69 | 107 | 45 |
| 2 | 58 | 151 | 54 |
| 4 | 57 | 149 | 55 |
| 8 | 56 | 153 | 60 |
| 16 | 57 | 145 | 57 |
| 32 | 46 | 98 | 43 |

(DuckDB's 1M columns are in the raw record; its shape is ordinary —
monotone gains to 8 threads, mild regression past that.)

### What the curve says

1. **The parallel scan earns its keep at 10M, not at 1M.** At 10M,
   coactive.narrow is 4.3× faster at 32 threads than serial and
   motif.filtered 2.8×; at 1M the same queries are *slower* at 2–16
   threads than serial. The engine gates the parallel path in units of
   segments ("serial below a few segments"), and the 1M curve shows the
   real break-even sits well above a 1M-event store for two of the three
   scan queries.

2. **series.count barely scales at any width: 596 → 466 ms (1.28×)
   against coactive.narrow's 4.3×.** Its parallelizable share at 10M is
   ~130 of ~600 ms; the rest is the ~740 ms serial residue eval_phase0
   already priced on full-window scans. Thread count is the wrong lever
   for that query; the residue is.

3. **Two threads are worse than one on both scales** (10M: 596→637,
   699→783, 205→240). Halving the segment list pays spawn-and-merge
   overhead while a straggler chunk sets the finish line; the win only
   appears from 4 workers up.

4. **Native needs no thread advantage to beat DuckDB at 10M.**
   Single-threaded native beats 32-thread DuckDB on series.count (596
   vs 1,336 ms) and at the defaults (16 both) native leads on all three
   queries.

5. **t=32 beats t=16 on all three 1M queries and on 10M coactive
   (196→162 ms).** With chunk = ceil(segments/threads), higher counts
   mean finer chunks and less straggler imbalance — a load-balance
   effect, not extra CPU. Suite memory is flat across the sweep (VmHWM
   5.9 GB at every width at 10M): the materialized answers, not the
   workers, own the peak.

## §15 Cold versus warm cache

Three cache states per query, coldest last:

- **warm** — in-process repetition; exactly the published protocol.
- **process-cold** — a fresh process against a page-cache-warm store:
  what every new client pays.
- **cold** — a fresh process *and* the page cache evicted: the first
  query after a reboot, approximately.

**Eviction method (no root available):** the host offers no
`drop_caches`, so eviction is user-space —
`posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)` over every file in the
store directory. DONTNEED drops clean pages, and a read-only store's
pages are clean; the kernel is nevertheless free to keep pages another
process references, so the "cold" column is a lower bound on true
coldness. The process-cold∕cold split then separates the in-process
caches (index warm-up, segment cache) from the page cache.

### Native (p50 ms warm; median first-query ms for the cold states)

| query | 1M warm | 1M proc-cold | 1M cold | 10M warm | 10M proc-cold | 10M cold |
|---|---|---|---|---|---|---|
| hist.single | 0.13 | 2,435 | 2,604 | 0.77 | 27,263 | 28,121 |
| snap.hop2 | 98 | 2,537 | 2,691 | 950 | 28,218 | 29,055 |
| series.count | 54 | 2,536 | 2,648 | 472 | 27,677 | 28,459 |
| coactive.narrow | 143 | 2,638 | 2,742 | 198 | 30,989 | 28,392 |
| diff.global | 334 | 2,809 | 2,934 | 3,925 | 31,382 | 31,923 |
| motif.filtered | 60 | 2,535 | 2,655 | 75 | 27,465 | 28,106 |

Store open: 6 ms (1M) / 29 ms (10M) warm, ~8/35 ms cold. (10M
coactive's proc-cold median sits above its cold median — 5-trial noise
on a 28 s quantity, not a signal.)

### DuckDB, for contrast (same states)

| query | 1M warm | 1M proc-cold | 1M cold | 10M warm | 10M proc-cold | 10M cold |
|---|---|---|---|---|---|---|
| hist.single | 26 | 285 | 295 | 67 | 379 | 448 |
| series.count | 152 | 423 | 462 | 1,169 | 1,702 | 2,004 |
| coactive.narrow | 110 | 367 | 424 | 227 | 552 | 1,198 |
| diff.global | 562 | 836 | 880 | 5,891 | 5,958 | 6,295 |

### What the states say

1. **Native's first query in a fresh process costs ~2.5 s at 1M and
   ~28 s at 10M — whatever the query.** A 0.77 ms point lookup pays the
   same ~28 s as a full-window scan: the first read triggers the
   in-memory warm-up (postings, close index) over every row. The tax
   scales linearly with store size (11× for 10×) and dwarfs everything
   else in this section. It independently reproduces §13's
   time-to-first-query note (2.5 s at 1M) and puts a number on it at
   10M.

2. **The page cache is the minor term.** Evicting the store adds only
   ~0.1–0.16 s at 1M and ~0.6–0.9 s at 10M on top of process-cold —
   about what re-reading 25/268 MB costs. TGMS's cold-cache story is an
   in-process warm-up story, not an I/O story.

3. **`open()` stays milliseconds in every state** (6–35 ms) — open is
   manifest-parse and dict-load, as published; the cost lands on the
   first query instead.

4. **Operationally, short-lived clients never see the warm tables.**
   DuckDB's fresh-process first query at 10M is 0.4–6.3 s against
   native's 27–32 s: the backend that wins every warm comparison loses
   the first-query race by ~10–70×. Long-lived processes amortize the
   warm-up; one-shot scripts pay it in full every time.

## §14.2 Working set versus RAM

The representative suite inside a Docker container with a hard
`--memory` cap against the bind-mounted 10M native store; uncapped
reference under the identical reduced protocol (2 warmups, 5 reps).
Docker on this host is rootful on a cgroup-v1 kernel **without
swap-limit support**: `--memory` bounds residency and the `--memory-swap`
flag is ignored, so overflow may reach the host's 8 GB swap before the
container OOMs. The cgroup cap was verified by reading
`memory.limit_in_bytes` back from inside a probe container (an
allocation canary is the wrong probe under exactly this kernel). The
`RLIMIT_AS` fallback was implemented but not needed; it caps address
space, where mmapped store files count against the limit even when
non-resident — a strictly harsher approximation, noted here because the
harness will use it on hosts without a usable Docker.

| cap | outcome (whole suite, one process) |
|---|---|
| uncapped | completes; suite VmRSS 4.2 GB, **VmHWM 5.9 GB** |
| 8 GB | completes at parity — series.count 479 vs 482 ms, diff.global 4,221 vs 4,081 ms (~+3%) |
| 4 GB | **OOM-killed** (exit 137) |
| 2 GB | **OOM-killed** (exit 137) |

Per query, each in its own capped container (`--per-query`):

| cap | hist.single | snap.hop2 | series.count | coactive.narrow | diff.global | motif.filtered |
|---|---|---|---|---|---|---|
| 8 GB | 0.6 ms | 961 ms | 482 ms | 203 ms | 4,034 ms | 75 ms |
| 4 GB | OOM | OOM | OOM | OOM | OOM | OOM |
| 2 GB | OOM | OOM | OOM | OOM | OOM | OOM |

Every 8 GB row records the **same VmHWM: 5,929 MB — for the point
lookup as much as for the global diff.**

### What the ceiling says

1. **The 10M store's practical memory floor is ~6 GB per process, and
   it is query-independent.** Under 4 GB even `hist.single` — 0.6 ms of
   actual work — is OOM-killed, because the first read triggers the
   in-process warm-up whose peak (5.9 GB) belongs to the store, not to
   the query. This is the same mechanism as §15's 28 s first-query tax:
   one warm-up, priced twice — once in seconds, once in gigabytes.

2. **Below the floor there is no graceful degradation — the process
   dies.** The plan's §14.2 asks where latency degrades and where things
   OOM; the answer at 10M is that there is no degradation region at all
   under a residency cap: 8 GB runs at uncapped parity (within ~3%),
   4 GB is exit 137. The segment cache and warm-up structures are
   unbounded by design (a known roadmap item); this sweep prices that
   decision at a 22× peak-memory blow-up over the 268 MB on-disk store,
   and turns "byte-budget LRU for the segment cache" from a nicety into
   the enabler for small-RAM deployment at 10M+.

3. **Above the floor, caps are free.** The 8 GB rows are
   indistinguishable from uncapped (e.g. series.count 482 vs 482 ms):
   once the working set fits, the cgroup boundary costs nothing.

## §14.4 Reader concurrency

N reader processes over one native store, each looping the same query
mix (point lookup, 2-hop traversal, two full-window scans) inside a
barrier-aligned wall-clock window (30 s at 1M, 60 s at 10M, after a
per-process warm pass). Reported: median per-reader p50 and aggregate
completed queries per second. This is the first concurrent measurement
behind the "lock-free reads off immutable segments" claim.

### 1M events

| readers | agg q/s | hist.single | snap.hop2 | series.count | coactive.narrow |
|---|---|---|---|---|---|
| 1 | 13.2 | 0.54 | 101 | 58 | 143 |
| 2 | 26.4 | 0.46 | 101 | 58 | 143 |
| 4 | 51.5 | 0.47 | 108 | 57 | 143 |
| 8 | 101.4 | 0.43 | 114 | 55 | 140 |
| 16 | 173.1 | 0.47 | 154 | 60 | 149 |

### 10M events

| readers | agg q/s | hist.single | snap.hop2 | series.count | coactive.narrow | note |
|---|---|---|---|---|---|---|
| 1 | 2.33 | 1.25 | 1,012 | 487 | 206 | |
| 2 | 4.28 | 1.29 | 1,096 | 566 | 207 | |
| 4 | 8.14 | 1.27 | 1,108 | 609 | 219 | |
| 8 | 16.0 | 1.29 | 1,134 | 578 | 269 | |
| 16 | 23.8 | 1.42 | 1,232 | 691 | 412 | **2 of 16 readers OOM-killed** |

### What concurrency says

1. **The lock-free claim holds where memory allows.** At 1M, aggregate
   throughput is 13.1× at 16 readers and per-reader medians are flat
   (hist.single 0.5 ms throughout; coactive 143 → 149 ms). At 10M it is
   perfectly linear to 8 readers (2.33 → 16.0 q/s, 6.9×). No
   cross-reader interference consistent with locking appears anywhere.

2. **At 10M the ceiling is memory, not locks: the host OOM killer took
   2 of 16 readers.** Each reader independently warms ~4 GB of process
   memory (unbounded segment cache plus materialization) — 16 readers
   ≈ 64 GB on a 93 GB host, plus the page cache. The survivors kept
   answering (23.8 q/s aggregate); the two SIGKILLs (rc −9, empty
   stderr) are the segment cache's per-process cost surfacing under
   multi-tenancy. The §14.2 sweep prices the same fact from the other
   side.

3. **Past 8 readers, scan latency pays for oversubscription.** Each
   reader's scans themselves fan out 16 threads, so 16 readers ask for
   ~256 workers on 40 cores: coactive.narrow doubles (206 → 412 ms) at
   16 readers while hist.single, which never fans out, moves 0.2 ms.

## Honest limits

- The fadvise eviction is user-space best-effort: it cannot force pages
  out that another process holds, so "cold" is bounded from the warm
  side. With nothing else running on the host the residual warmth is
  small, but unmeasured.
- Docker's cap on this kernel bounds residency only; the 2/4 GB OOMs
  happened with host swap nearly full, so a machine with free swap would
  degrade (thrash) before dying rather than exit 137. Both behaviors are
  "working set exceeds RAM"; the boundary between them is
  swap-availability, which this host could not vary.
- Reader concurrency uses duration-based windows rather than §16.3
  fixed repetitions (concurrency needs overlap, not equal work); medians
  are over ≥10 completions per reader per query at 10M, more at 1M.
- The thread sweep's 1M anomaly (t=32 fastest) was not chased further
  than the load-balance reading above; a reversed-order control run
  would separate it from any residual order effect.
- Single store per scale, ~0.5% correction density (the harness
  baseline). §13 shows correction density moves scan latencies; these
  axes were not crossed with it.
