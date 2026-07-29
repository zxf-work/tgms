# Phase 0 evaluation: three systems, one registry

Twelve registry queries answered by the TGMS native engine, the TGMS DuckDB
adapter, and a tuned PostgreSQL baseline. Regenerate with:

```bash
uv run python scripts/eval_harness.py --scale 200000 --systems native,duckdb,postgres
```

## Receipts (spec §8.4)

- commit `d72244f`, 200,000 events, 3 repeats per query, p50 reported
- **xzgpu** — 40 cores, 93 GB, Linux 5.4. Every number here comes from that
  one host; `eval_harness.py` warns when run anywhere else, because a laptop
  differs by 5× in cores and 6× in RAM and pins `effective_io_concurrency`
  to 0 for want of `posix_fadvise`.
- PostgreSQL 16.14, source build, `--locale=C --without-icu`,
  `shared_buffers` 16 GB, `effective_cache_size` 64 GB, `work_mem` 256 MB
- all three systems load **the same replayed event log**, so transaction
  times — and every id derived from them — are identical (D-023)
- **all 12 queries return identical canonical hashes on all 3 systems**

The dataset carries corrections, a second belief epoch, community structure,
and a deliberate burst, so no query is answered over an empty or trivial
result. That was not true of earlier runs of this harness; see
`engine_lessons.md` §9a.

## Results

p50 milliseconds.

| query | native | duckdb | postgres | fastest |
|---|---:|---:|---:|---|
| hist.single | 4.4 | 11.9 | **0.5** | postgres |
| hist.asof | 4.2 | 11.5 | **0.5** | postgres |
| snap.hop2 | **26.9** | 49.9 | 43.5 | native |
| diff.global | **75.6** | 113.8 | 108.1 | native |
| reach.window | **22.6** | 28.5 | 699.1 | native |
| paths.k | **11.9** | 19.7 | 21.8 | native |
| series.count | **31.8** | 49.2 | 57.1 | native |
| burst.zscore | **37.1** | 49.2 | 56.5 | native |
| nbr.evolution | 17.4 | 18.9 | **3.5** | postgres |
| coactive.narrow | **43.0** | 56.8 | 55.1 | native |
| resolve.substr | **5.7** | 21.5 | 5.8 | tie |
| motif.filtered | **356.2** | 584.0 | 1003.8 | native |

Native is fastest on 8, PostgreSQL on 3, one tie. Against DuckDB alone the
native engine now wins all 12.

## What the baseline actually showed

**PostgreSQL wins point lookups by roughly 9×.** `hist.single` and
`hist.asof` are 0.5 ms against the engine's 4.2–4.4 ms. A B-tree lookup is
hard to beat, and the engine has a fixed per-lookup cost — the same ~13 ms
flat identity lookup that shows up in the write path (`engine_lessons.md`
§9b) — that does not shrink with the answer. This is the clearest thing the
baseline bought: it is a floor the engine was not previously measured against.

**`diff.global` was the native engine's worst result and is now its clearest
win** — see the section below. It measured 419 ms against 111 and 104, the
only operator where DuckDB beat native; it now measures 75.6 ms, the fastest
of the three. The baseline is what made the regression visible.

**`nbr.evolution`** also favours PostgreSQL (3.5 vs 17.4 ms): it is a small
indexed neighbourhood lookup plus a bucketed count, which is exactly what
partial indexes are good at.

**The engine wins where scanning and ordering dominate** — snapshots,
traversal, series, interval join, motifs — which is the shape it was designed
for, and it wins them on a fair baseline rather than an untuned one.

## The one regression the baseline caught, and its cause

`diff_snapshots` at 419 ms looked like a scan problem. The obvious suspect was
`edges_at`, which requests no column projection and so materializes `eid` —
a sha256 per row — for every edge valid at each of the two instants.

That is real and costs 19 ms. The two point-state scans together are 54 ms of
the 419. **The remaining 365 ms was `props_for_vids`**, called twice to fetch
props for the handful of candidate identities whose version differs between
the instants. It routed through `all_*_versions`, rebuilding every row in the
store — two dictionary lookups, several string allocations, and a sha256 for
`eid` — to pick out sixteen vids. Measured, it cost the same for one vid as
for 256, and scaled with the store: 86 ms at 50k versions, 353 ms at 200k.

It now sweeps three integer columns per segment and reads a string only once
a vid matches: **353 ms → 6.9 ms**, and `diff.global` 419 → 75.6 ms. It is
still O(rows) — vids are hashes, so segments have no order to search them by
and there is no vid index — but the constant was the problem, not the
complexity.

This is the tenth time in this project that the layer under suspicion was not
the one costing the time, and the second where the suspected layer was
genuinely wasteful but nowhere near the bottleneck.

## One number that was mine, not PostgreSQL's

`reach.window` first measured **278,810 ms**. The natural SQL for
time-respecting reachability is a `WITH RECURSIVE ... UNION` over
`(node, arrival)` states, and it is correct — but a recursive CTE may not
aggregate over its own working table, so it cannot discard a state dominated
by a better arrival at the same node, and it enumerates the entire reachable
state space. Rewritten as round-by-round relaxation against a temp table
holding one row per node, it runs in **699 ms** — a 399× difference, entirely
in the query.

It is still 31× slower than the engine's 22.6 ms, and that residue is a real
result. But had the first number been published, the table would have said
PostgreSQL is 12,000× slower at reachability, and that would have been a
measurement of my SQL. D-030 makes baseline query quality part of what is
being measured for exactly this reason.

## Load time

| | native | duckdb | postgres |
|---|---:|---:|---:|
| load 200k events | 24.1 s | 11.0 s | 24.9 s |

Native's figure is not bulk-ingest throughput. The generated log ends with
~250 single-op corrections and retractions, and each one costs ~45 ms — half
fsynced commit, half identity lookup (`engine_lessons.md` §9b). Bulk ingest
alone is 0.27 s per 20k events. PostgreSQL's number includes building the
reference store first, so it is not comparable either; only the DuckDB column
is close to a straight load measurement.

## Storage

Recorded without a ratio, because the index sets are not comparable: the
PostgreSQL schema carries eight edge indexes chosen for the whole registry
against two on the TGMS side, and the TGMS index figure is a projection
rather than a measurement. See `eval_semantics.md`.
