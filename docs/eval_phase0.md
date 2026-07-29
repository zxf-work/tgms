# Phase 0 evaluation: three systems, one registry

Twelve registry queries answered by the TGMS native engine, the TGMS DuckDB
adapter, and a tuned PostgreSQL baseline. Regenerate with:

```bash
uv run python scripts/eval_harness.py --scale 200000 --systems native,duckdb,postgres
```

## Receipts (spec §8.4)

- commit `bcafe95`, 200,000 events, 3 repeats per query, p50 reported
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
| hist.single | 1.1 | 9.7 | **0.5** | postgres |
| hist.asof | 1.1 | 9.1 | **0.5** | postgres |
| snap.hop2 | **23.6** | 46.7 | 41.2 | native |
| diff.global | **72.2** | 108.1 | 109.2 | native |
| reach.window | **18.3** | 23.9 | 699.7 | native |
| paths.k | **8.9** | 16.2 | 21.9 | native |
| series.count | **29.0** | 40.2 | 56.9 | native |
| burst.zscore | **29.8** | 44.8 | 56.8 | native |
| nbr.evolution | 15.1 | 17.0 | **3.6** | postgres |
| coactive.narrow | **37.4** | 62.5 | 54.3 | native |
| resolve.substr | **3.6** | 19.0 | 6.0 | native |
| motif.filtered | **52.6** | 85.6 | 143.2 | native |

Native is fastest on 9, PostgreSQL on 3. Against DuckDB alone the native
engine wins all 12.

## What the baseline actually showed

**PostgreSQL still wins point lookups, but by 1.8× rather than 9×.**
`hist.single` and `hist.asof` are 0.5–0.6 ms against the engine's 1.1 ms.
They were 4.2–4.4 ms until the baseline made the gap impossible to ignore;
see the point-lookup section below. A B-tree lookup on a warm 16 GB buffer
pool is a hard floor, and the residual difference is now small enough to be
mostly fixed per-call cost on both sides rather than a structural defect.
This remains the clearest thing the baseline bought: a floor the engine had
never been measured against.

**`diff.global` was the native engine's worst result and is now its clearest
win** — see the section below. It measured 419 ms against 111 and 104, the
only operator where DuckDB beat native; it now measures 75.6 ms, the fastest
of the three. The baseline is what made the regression visible.

**`nbr.evolution`** also favours PostgreSQL (3.5 vs 15.1 ms): it is a small
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

It is the sixth entry in `engine_lessons.md` §1 — the running table of times
the layer under suspicion was not the one costing the time — and the second
where the suspected layer was genuinely wasteful yet nowhere near dominant.

## Closing the point-lookup floor

PostgreSQL answering a two-row lookup in 0.5 ms against the engine's 4.2 ms
was the baseline's most useful result, because nothing in the TGMS-only
comparison had ever suggested a problem. Two causes, in roughly equal parts,
and neither was the storage layout:

**The index located the rows and the row fetch threw it away.** `locate()`
returns exact `(file, row)` pairs from the identity postings index, and both
`believed_*` paths then rebuilt the *entire segment* to index into it — for
edges, a sha256 and two dictionary lookups per row of a whole segment to reach
two of them. The signature was unmistakable once measured: `believed_edge_
versions` cost **76 ms at 50k versions and 76 ms at 200k**, flat, because one
segment has a fixed maximum size. Materializing only the located rows takes it
to **1.01 ms** — 76×.

**Argument validation cost as much as reading the data.** `jsonschema.validate`
is a convenience wrapper that re-checks the schema and constructs a fresh
validator on every call, re-resolving `$ref`s through `urljoin`. On
`entity_history` that was ~2 ms per call against a ~2 ms Rust lookup. Compiling
the validator once per operator removed it.

| | before | after |
|---|---:|---:|
| `believed_edge_versions` | 76.44 ms | **1.01 ms** |
| `believed_node_versions` | 2.03 ms | **0.92 ms** |
| `entity_history` (end to end) | 4.14 ms | **1.06 ms** |
| load 200k events | 24.1 s | **5.3 s** |

The validator fix is why every other operator in the table also improved:
they all pay the envelope. The load-time change is the read fix showing up in
the write path — each correction has to find the version it corrects.

## The motif operator, where the kernel was never the cost

`motif.filtered` was the slowest query on all three systems. On the engine it
took 369 ms — of which the Rust δ-motif matcher was **11.4 ms**. Everything
else was the call that fetched the events:

- the scan requested no column projection, so props, vid, vt_e, source and
  provenance were materialized for every row in the window — 95 ms;
- the node filter was applied in NumPy *after* the scan, by which point `eid`
  — a sha256 per row — had been derived for all 200,009 window rows, 227 ms,
  and the mask then discarded 93% of them (14,472 survived).

A motif event needs *both* endpoints in the filter and the scan can only push
down or-incidence, but `{both} ⊆ {either}`, so passing the filter as
`touching_ids` is an exact pre-filter and the and-test still decides.
Projecting the five columns actually read and pushing the filter down took the
operator to **52.6 ms**. The matcher still measures 11.1 ms: unchanged, and
now a fifth of the total rather than a thirtieth.

Because the fix is in the shared operator layer and both backends implement
`touching_ids`, **DuckDB improved too, 576.8 → 85.6 ms** — the comparison did
not move in TGMS's favour by handicapping the other side.

## One number that was mine, not PostgreSQL's

`reach.window` first measured **278,810 ms**. The natural SQL for
time-respecting reachability is a `WITH RECURSIVE ... UNION` over
`(node, arrival)` states, and it is correct — but a recursive CTE may not
aggregate over its own working table, so it cannot discard a state dominated
by a better arrival at the same node, and it enumerates the entire reachable
state space. Rewritten as round-by-round relaxation against a temp table
holding one row per node, it runs in **699 ms** — a 399× difference, entirely
in the query.

It is still 38× slower than the engine's 18.3 ms, and that residue is a real
result. But had the first number been published, the table would have said
PostgreSQL is 12,000× slower at reachability, and that would have been a
measurement of my SQL. D-030 makes baseline query quality part of what is
being measured for exactly this reason.

The motif query needed the same correction, twice over. Its events lived in a
CTE rather than an indexed temp table, and its middle join carried no time
bound — `b.t <= a.t + delta` is *implied* by the row ordering, but leaving it
implicit let that join range over every event sharing an endpoint regardless
of when it happened. Indexing the events and stating the bound: **1005.7 →
143.2 ms**. Two of the three slowest PostgreSQL numbers in this evaluation
were my SQL rather than the database.

## Load time

| | native | duckdb | postgres |
|---|---:|---:|---:|
| load 200k events | 4.9 s | 11.0 s | 5.0 s |

Native was **24.1 s** here until the point-lookup fix below. The generated log
ends with ~250 single-op corrections and retractions, and each one performed
an identity lookup that cost 76 ms; that is now 1 ms, and what remains is
mostly per-commit fsync (`engine_lessons.md` §9b). A read-path fix moved the
write path by 4.5×, because the write path reads.

Neither the native nor the PostgreSQL column is a clean ingest measurement —
PostgreSQL's includes building the reference store first — so only the DuckDB
column is close to a straight load number.

## Storage

Recorded without a ratio, because the index sets are not comparable: the
PostgreSQL schema carries eight edge indexes chosen for the whole registry
against two on the TGMS side, and the TGMS index figure is a projection
rather than a measurement. See `eval_semantics.md`.
