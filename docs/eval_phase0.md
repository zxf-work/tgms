# Phase 0 evaluation: three systems, one registry

Twelve registry queries answered by the TGMS native engine, the TGMS DuckDB
adapter, and a tuned PostgreSQL baseline. Regenerate with:

```bash
uv run python scripts/eval_harness.py --scale 200000 --systems native,duckdb,postgres
```

## Receipts (spec §8.4)

- commit `7606fd5` — measured under the plan's §16 protocol: 5 warmups,
  30 measured repetitions per sub-second query (10 for slower), true median
  reported here, p95 and raw timings in the JSON records.
  **Tables in earlier revisions of this file were min-of-3 labeled as p50**;
  the harness computed a best case and the field name hid it. Medians turn
  out close to those minimums, so no conclusion flips, but the labels were
  wrong and this note is the correction.
- the CollegeMsg `motif.filtered` row was remeasured on the same host and
  protocol after the cost-model reprice ("motifs: price the filtered query
  by delta-pairs, not max-degree squared"); the full rerun agreed on all 12
  hashes, and the other rows kept their `7606fd5`-sweep values.
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

Median milliseconds. Three datasets: the synthetic reference at 200k and 1M
events, and the frozen CollegeMsg replay (59,835 instantaneous events, real
timestamps, no corrections). Every (query, system, dataset) cell agrees on
the canonical hash, except where noted.

### synth, 200k events

| query | native | duckdb | postgres | fastest |
|---|---:|---:|---:|---|
| hist.single | 1.1 | 9.0 | **0.3** | postgres |
| hist.asof | 1.1 | 8.9 | **0.2** | postgres |
| snap.hop2 | **23.7** | 45.1 | 61.4 | native |
| diff.global | **70.4** | 111.2 | 111.7 | native |
| reach.window | **18.6** | 24.7 | 703.9 | native |
| paths.k | **9.5** | 16.4 | 21.4 | native |
| series.count | **27.9** | 42.6 | 66.9 | native |
| burst.zscore | **29.2** | 43.3 | 67.8 | native |
| nbr.evolution | 15.0 | 16.3 | **2.8** | postgres |
| coactive.narrow | **37.3** | 62.6 | 56.7 | native |
| resolve.substr | **3.6** | 19.3 | 5.7 | native |
| motif.filtered | **32.2** | 63.3 | 144.0 | native |

### synth, 1M events

| query | native | duckdb | postgres | fastest |
|---|---:|---:|---:|---|
| hist.single | 1.1 | 23.7 | **0.3** | postgres |
| hist.asof | 1.1 | 24.8 | **0.2** | postgres |
| snap.hop2 | **96.6** | 214.8 | 244.4 | native |
| diff.global | **325.2** | 575.2 | 576.7 | native |
| reach.window | **122.8** | 155.8 | 6115.8 | native |
| paths.k | **11.1** | 36.8 | 27.4 | native |
| series.count | **123.2** | 131.8 | 210.3 | native |
| burst.zscore | **124.6** | 134.0 | 210.2 | native |
| nbr.evolution | 20.1 | 42.9 | **3.0** | postgres |
| coactive.narrow | 125.8 | **109.9** | 178.2 | duckdb |
| resolve.substr | **4.3** | 85.5 | 18.5 | native |
| motif.filtered | **52.6** | 91.2 | 186.4 | native |

Two honest notes on 1M. **`coactive.narrow` is DuckDB's first win**: 109.9
against native's 125.8 — the interval join's advantage at 200k does not
survive 5× the data, which makes it the next thing worth profiling, not
hiding. And native's point lookup holds flat at 1.1 ms across scale while
PostgreSQL holds at 0.3; DuckDB's 23.7 grows, because it is a scan.

### synth, 10M events (abridged)

| query | native | duckdb | postgres | fastest |
|---|---:|---:|---:|---|
| hist.single | **1.7** | 66.1 | 0.3 | postgres |
| snap.hop2 | **1001.4** | 1716.3 | 2538.3 | native |
| diff.global | **3918.9** | 5611.7 | 6686.9 | native |
| reach.window | n/a | n/a | **43987.2** | guardrailed |
| paths.k | n/a | n/a | **38.0** | guardrailed |
| series.count | 1207.9 | **956.6** | 2160.0 | duckdb |
| burst.zscore | 1209.4 | **957.2** | 2127.7 | duckdb |
| nbr.evolution | 70.5 | 105.7 | **2.7** | postgres |
| coactive.narrow | 724.1 | **224.9** | 1722.4 | duckdb |
| resolve.substr | **34.8** | 775.4 | 201.0 | native |
| motif.filtered | 227.1 | **167.0** | 644.1 | duckdb |

10M inverts part of the picture, and the pattern of what flips is the
finding. **Every query DuckDB now wins is a full-window scan** — series,
burst, the interval join (3.2× faster), the motif event fetch — while native
keeps everything index-served or selective (point lookup flat at 1.7 ms,
resolve 22× faster, snapshots and diff still ahead). The obvious hypothesis
is parallelism: DuckDB scans on all 40 cores, the native scan is
single-threaded. That is a hypothesis, not a measurement — nothing here
profiles it — but it is the first structural argument for parallel scan in
the native engine, and it puts a number on what it would buy.

The guardrail gates both traversal queries on TGMS at this scale.
PostgreSQL's answers split the verdict: reachability genuinely explodes
(44 s), so that refusal is the guardrail working; but `paths.k` runs in
**38 ms** — the k-shortest search is cheap at any scale because the frontier
is bounded — so that refusal is the same cost-model false positive the
CollegeMsg motif row exposed, second instance.

### CollegeMsg (59,835 events, real timestamps; abridged)

| query | native | duckdb | postgres | fastest |
|---|---:|---:|---:|---|
| hist.single | **0.1** | 7.2 | 0.3 | native |
| snap.hop2 | **2.5** | 14.2 | 8.4 | native |
| diff.global | **5.2** | 23.6 | 8.3 | native |
| reach.window | **2.1** | 9.5 | 38.8 | native |
| paths.k | **137.0** | 141.7 | 306.7 | native |
| coactive.narrow | **1.4** | 14.4 | 37.5 | native |
| resolve.substr | **2.6** | 17.0 | 5.2 | native |
| motif.filtered | **3.8** | 22.0 | 27.7 | native |

Full table in `eval-collegemsg-costfix.json` (the pre-reprice record, with
the motif refusals, is `eval-collegemsg.json`). Instant snapshots and interval joins
are legitimately thin here — the events are instantaneous, so microsecond
intervals cannot strictly overlap — and the belief probe works without
corrections because it pins mid-ingestion state.

**The motif row was a guardrail false positive, found by the baseline.**
Both TGMS backends refused with `E_COST` while PostgreSQL answered in 27.3
ms — count 7. The cost model scaled with max out-degree, and CollegeMsg's
skew (one user with out-degree 1091) put the estimate at 65M expansions
when the filtered window actually holds 1,102 events. The two TGMS backends
agreeing on the refusal is consistency, not correctness: the ceiling was
mispriced for skewed degree distributions.

Repriced: the estimate now charges the filter's event mass at *mean* out
degree and expands it only against events within `delta` — half of
`e_f² · delta/span` — instead of `min(e_w, k·max_deg) · max_deg`
(`_motif_cost` in `tgms/temporal/ops_motifs.py`). On the rerun the row
answers on all three systems with agreeing hashes (table above). The
refusals that were correct stay refusals: the synthetic 200k log at
`node_filter = |V|/5` estimates 14.5M expansions against 18.7M measured
delta-pairs and keeps its `E_COST`, and the unfiltered full-window query
stays gated at every scale in the sweep.

On synth 200k native is fastest on 9 of 12 and wins all 12 vs DuckDB.

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

A footnote from finishing the job: projecting the diff's point-state scans to
the five columns it reads bought only 72.2 → 69.8 ms. The measurement says
why: `('eid',)` alone costs 15.3 of the projected scan's 17.6 ms. The diff is
*defined* over derived identity, so a sha256 per row at each instant is the
price of D-028's identity-is-derived decision, not a call-site defect — and
the earlier `eid_hi`/`eid_lo` experiment already showed integer identity does
not pay for itself end to end. The projection's real yield was a latent bug:
the scan omitted a *requested* `vid` column whenever zero rows matched, which
no unprojected caller could ever hit. It is fixed with a regression test, and
`diff_snapshots` no longer fails on a window that sees an empty instant.

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

A motif event needs *both* endpoints in the filter, and the scan only offered
or-incidence. `{both} ⊆ {either}`, so passing the filter as `touching_ids` was
already an exact pre-filter with the and-test above it — that plus projecting
the five columns actually read took the operator to 52.6 ms. Teaching the scan
the and-form outright (`touching_both`) took it to **40.5 ms**: the weaker
pushdown still derived `eid` for every or-match, 25k rows to keep 14.5k.

| | ms |
|---|---:|
| original | 368.9 |
| + column projection, or-pushdown | 52.6 |
| + both-endpoints pushdown | 40.5 |
| + NumPy buffers at the PyO3 boundary | **32.0** |
| — of which `_match` (boundary + kernel) | 5.5 |

The matching algorithm was never touched. `_match` fell from 11.0 to 5.5 ms
only because the three int64 columns stopped being converted twice — once by
a Python list comprehension, once by PyO3 walking the list — and now cross as
borrowed NumPy buffers.

Splitting what remains, with a probe that extracts the same arguments and then
returns:

| | ms |
|---|---:|
| full `motif_match` | 5.31 |
| argument extraction | 0.94 |
| — same probe with `eid=[]` | 0.00 |
| **matching** | **4.37** |

So the boundary is now genuinely cheap: passing three int64 columns as NumPy
buffers costs nothing measurable, and the whole 0.94 ms is building
`Vec<String>` for `eid`. Python-side conversion is 0.07 ms.

That settles the obvious next optimization as **not worth doing**. `eid` could
avoid the string entirely — the scan holds it as a 96-bit id and formats it to
hex on the way out, and hex order equals `Id96` order so the tiebreak would
survive — but it would buy at most 0.94 ms of a 32 ms operator, 3%, in
exchange for changing the scan's output contract and the kernel's comparison
logic. The measurement is the reason not to.

What remains in the operator is close to the scan floor: reading the window's
three integer columns costs 25.4 ms on its own, and `_events` costs 25.6.

Because the work happened in the shared operator layer, the scan ABC, and a
shared kernel binding, both backends benefited: **DuckDB 576.8 → 60.6 ms**.
The comparison did not move in TGMS's favour by handicapping the other side.

## One number that was mine, not PostgreSQL's

`reach.window` first measured **278,810 ms**. The natural SQL for
time-respecting reachability is a `WITH RECURSIVE ... UNION` over
`(node, arrival)` states, and it is correct — but a recursive CTE may not
aggregate over its own working table, so it cannot discard a state dominated
by a better arrival at the same node, and it enumerates the entire reachable
state space. Rewritten as round-by-round relaxation against a temp table
holding one row per node, it runs in **705 ms** — a ~400× difference, entirely
in the query.

It is still 37× slower than the engine's 18.9 ms, and that residue is a real
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
| load 200k events | 4.6 s | 11.1 s | 4.8 s |

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
