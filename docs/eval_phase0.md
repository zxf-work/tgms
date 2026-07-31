# Phase 0 evaluation: three systems, one registry

Twelve registry queries answered by the TGMS native engine, the TGMS DuckDB
adapter, and tuned PostgreSQL and ClickHouse baselines (D-030, D-035).
Regenerate with:

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

### synth, 200k events (four systems, D-035)

| query | native | duckdb | postgres | clickhouse | fastest |
|---|---:|---:|---:|---:|---|
| hist.single | 1.0 | 9.3 | **0.3** | 9.6 | postgres |
| hist.asof | 1.0 | 9.1 | **0.2** | 10.0 | postgres |
| snap.hop2 | **23.6** | 46.6 | 67.0 | 62.2 | native |
| diff.global | **71.2** | 114.4 | 121.8 | 149.7 | native |
| reach.window | **17.1** | 26.5 | 734.5 | 1161.4 | native |
| paths.k | **9.1** | 16.8 | 22.6 | 146.1 | native |
| series.count | 29.2 | 44.3 | 73.5 | **14.7** | clickhouse |
| burst.zscore | 29.5 | 44.7 | 74.0 | **17.4** | clickhouse |
| nbr.evolution | 11.5 | 17.0 | **2.8** | 76.7 | postgres |
| coactive.narrow | **54.7** | 61.5 | 61.9 | 120.9 | native |
| resolve.substr | **4.0** | 19.7 | 5.8 | 12.4 | native |
| motif.filtered | **40.8** | 63.2 | 149.6 | 132.4 | native |

All twelve hash-identical across all four systems. The baselines divide
the map cleanly: PostgreSQL owns indexed point shapes, ClickHouse owns
whole-window aggregation (the first system to beat native on any scan
shape at this scale), and both pay heavily for iterative traversal —
ClickHouse's reachability rounds cost ~1 ms of HTTP plus a table build
each, which is the honest price of expressing recursion in an engine
that does not natively offer it. Native holds 8 of 12.

### synth, 1M events (four systems)

| query | native | duckdb | postgres | clickhouse | fastest |
|---|---:|---:|---:|---:|---|
| hist.single | 1.1 | 23.6 | **0.3** | 10.7 | postgres |
| hist.asof | 1.1 | 25.3 | **0.2** | 11.2 | postgres |
| snap.hop2 | **97.2** | 209.8 | 267.8 | 150.1 | native |
| diff.global | **340.4** | 566.7 | 613.1 | 581.0 | native |
| reach.window | **127.4** | 162.6 | 6334.0 | 3731.2 | native |
| paths.k | **11.6** | 39.5 | 28.9 | 178.0 | native |
| series.count | 59.0 | 141.0 | 219.0 | **16.6** | clickhouse |
| burst.zscore | 60.3 | 145.2 | 220.1 | **18.0** | clickhouse |
| nbr.evolution | 23.6 | 48.7 | **3.1** | 48.7 | postgres |
| coactive.narrow | 135.1 | 109.2 | 195.4 | **104.2** | clickhouse |
| resolve.substr | **7.7** | 85.8 | 19.4 | 17.4 | native |
| motif.filtered | **58.6** | 92.6 | 197.1 | 134.0 | native |

(The interval join's 1M story moved twice as the engine improved: DuckDB
took it from native pre-clustering, and ClickHouse now holds it at this
scale; native retakes it at 10M. Point lookups stay flat across scale on
native and PostgreSQL; DuckDB's grow, because its lookup is a scan.)

### synth, 10M events (abridged)

Refreshed at commit `9d38404` (cluster-wise merge in; native and DuckDB —
the PostgreSQL 10M column, measured once pre-refresh, is retained where no
TGMS-side change affects it):

| query | native | duckdb | postgres | clickhouse | fastest |
|---|---:|---:|---:|---:|---|
| hist.single | 1.6 | 66.6 | **0.3** | 12.5 | postgres |
| snap.hop2 | 937.0 | 1669.8 | 2506.8 | **749.3**† | clickhouse |
| diff.global | **3791.4** | 5490.1 | 6670.6 | 5347.9 | native |
| reach.window | n/a | n/a | 44400.4 | **4044.9** | guardrailed |
| paths.k | n/a | n/a | **37.2** | 242.6 | guardrailed |
| series.count | 469.6 | 954.1 | 2176.7 | **38.8** | clickhouse |
| burst.zscore | 465.1 | 959.9 | 2156.5 | **40.2** | clickhouse |
| nbr.evolution | 75.9 | 107.8 | **2.8** | 99.4 | postgres |
| coactive.narrow | **181.4** | 224.1 | 1781.7 | 221.2 | native |
| resolve.substr | **97.3** | 884.3 | 200.5 | 121.9 | native |
| motif.filtered | **73.5** | 168.0 | 661.3 | 230.0 | native |

†Re-measured after a plumbing fix: the BFS originally inlined the
reached-node id list in query text and blew `max_query_size` at 10M. With
node sets shipped through working tables the query hash-matches and runs
749.3 ms — **beating native**, whose 937 ms materializes the induced
snapshot serially. A cell that began as a defect record ended as a
ClickHouse win; both facts are kept.

The scaling stories the four-system sweep settles: **ClickHouse's
aggregation lead grows with scale** — 2× over native at 200k, 3.5× at 1M,
**12× at 10M** (38.8 vs 469.6) — it is simply the right engine for
whole-window aggregation, and the honest comparison says so. Its
iterative relaxation also **beats PostgreSQL's by 11×** on the
reachability query TGMS guardrails (4.0 s vs 44.4 s), so the baselines
now bracket that guardrail from both sides. Meanwhile the interval join
flipped back: ClickHouse took `coactive.narrow` at 1M (104.2 vs 135.1)
and native retook it at 10M (181.4 vs 221.2) on the cluster-wise merge.
Native holds every other selective and traversal shape it answers;
ClickHouse took the 2-hop snapshot at 10M once its query was fixed — the
first traversal-family loss, worth watching as scale grows.

10M initially inverted part of the picture: DuckDB won series, burst, the
interval join (3.2×), and the motif fetch — every full-window scan — on a
40-core host where the native scan ran single-threaded. Parallelizing the
per-segment selection (scoped threads, byte-identical results by
construction) settled the hypothesis **half-right**: `coactive.narrow`
724 → **151 ms** and `motif.filtered` 227 → **77 ms**, both back ahead of
DuckDB — those scans are selection-bound (incidence and node filters per
row). `series.count`/`burst.zscore` did not move (~1.26 s vs DuckDB's ~0.96) —
profiled: the scan is 1167 ms of the operator's 1274, and projecting down
to one column changes nothing (1170 ms), so the cost is not column
materialization. NumPy's mask-and-bincount is 88 ms. What remained was the
serial post-selection machinery. Fixing the merge — keys were resolved by
column name and built into an `Id96` per popped row (lesson §2 again), and
valid-time clustering means non-interleaving segments can concatenate
instead of heap-merging at all — took the scan 1167 → 811 ms and the
operator to **929 ms, just under DuckDB's ~958**. The two paths are
byte-identical (the disjointness check uses the selected rows' own
composite keys), all 12 queries still agree. Parallelizing materialization (disjoint selections on scoped threads, no
order list at all) first moved **nothing** — 811 → 819 ms — and a stage-
timing probe explained why: the code was not irrelevant, it was
**unreachable on this store**. Per call: `select` 33 ms, NumPy boundary +
eid 73 ms, core total 733 ms — with the disjoint fast path never firing,
because corrections write superseding versions into segments whose key
ranges overlap the originals, and one overlap anywhere failed the then
all-or-nothing disjointness check. A corrected store is the *normal*
store. The fix is **cluster-wise merging**: selections group into
key-range overlap clusters; clusters concatenate and materialize in
parallel; only rows within a cluster heap-merge. That took the scan
817 → 330 ms and `series.count` to **434–439 ms, 2.1× ahead of DuckDB**
— and made the "irrelevant" parallel materialization the thing doing the
work. The full arc (parallel select → null result → probe → clustering)
is lesson material: the 819 ms non-result was inventory, not waste.

The guardrail gates both traversal queries on TGMS at this scale.
PostgreSQL's answers split the verdict: reachability genuinely explodes
(44 s), so that refusal is the guardrail working; but `paths.k` runs in
**38 ms** — the k-shortest search is cheap at any scale because the frontier
is bounded — so that refusal is the same cost-model false positive the
CollegeMsg motif row exposed, second instance.

### CollegeMsg (59,835 events, real timestamps; abridged)

| query | native | duckdb | postgres | fastest |
|---|---:|---:|---:|---|
| hist.single | **0.1** | 7.3 | 0.3 | native |
| snap.hop2 | **2.3** | 14.1 | 8.6 | native |
| diff.global | **4.6** | 21.4 | 8.5 | native |
| reach.window | **1.6** | 9.6 | 39.2 | native |
| paths.k | **137.6** | 143.2 | 307.6 | native |
| series.count | **1.4** | 19.6 | 20.2 | native |
| coactive.narrow | **1.2** | 13.9 | 37.4 | native |
| resolve.substr | **2.8** | 16.8 | 5.2 | native |
| motif.filtered | **3.6** | 21.9 | 27.4 | native |

One coherent run at the current tip (parallel scan, merge fast path, gc,
and the cost-model reprice all in): `eval-collegemsg.json`. The reprice's
own rerun record is `eval-collegemsg-costfix.json`; the pre-reprice motif
refusals survive only in this file's history. Instant snapshots and interval joins
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

With column compression (D-032) and string-heap packing (D-033), the
native store's segments measure **24.6 B/row at 1M rows — 0.132×** the
DuckDB representation's 186.3; uncompressed they were 65.3, so 2.65× total.
Per row: 12.0 B of vid (sha256-derived, incompressible — 49% of segment
bytes, the standing price of derived identity), 4.4 B of packed string heap,
3.5 B of endpoints, 3.0 B of timestamps, ~1.7 B of everything else. Heap
packing cost no measurable query latency: medians are within noise of the
D-032 run, since both decode once per process behind the segment cache.

One honest caveat stands. Query latency *improved* alongside (co_active
37.3 → 23.5 ms, nbr.evolution 15.0 → 5.6, diff 60.4), but compression
landed together with a store-level segment cache that stops every operator
call re-opening and re-parsing every segment — the gain belongs to the
pair, and no ablation separates them.

The other caveat this section used to carry — manifest retention as the
dominant overhead — is closed. Every commit still writes a full manifest
naming every segment, and at 1M rows the 283 commits had accumulated
23.6 MB of manifests (23.4 B/row) against 24.8 MB of segments. Generation
collection (`tgms store gc`, D-034) now removes generations outside a
retention window — the last K plus any pinned by a live reader — and the
same pass collects segment and close-run files no retained generation
references. Measured on the same 1M build (xzgpu): whole store 48.2 →
**25.1 B/row** after gc alone (manifests 23.41 → 0.33 B/row, 283 files →
2), and **24.6 B/row** after compaction merges the 283 segments into 3 and
gc collects the superseded files — reclaiming compaction's 2× transient
(50.0 MB peak → 24.8 MB). What a retained manifest costs stays O(segments):
165 KB while it names 283 segments, 1 KB naming the 3 post-compaction ones.

The PostgreSQL comparison stays unratioed: its schema carries eight edge
indexes chosen for the whole registry against two on the TGMS side. See
`eval_semantics.md`.
