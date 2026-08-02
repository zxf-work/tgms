# Phase 0 evaluation: three systems, one registry

Thirteen registry queries answered by the TGMS native engine, the TGMS
DuckDB adapter, and tuned PostgreSQL and ClickHouse baselines (D-030,
D-035, D-044).
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
- **every query returns identical canonical hashes on every system that
  implements it** — 13 of 13 across native, DuckDB, PostgreSQL and
  ClickHouse at 1M and (bar PostgreSQL) 10M

The dataset carries corrections, a second belief epoch, community structure,
and a deliberate burst, so no query is answered over an empty or trivial
result. That was not true of earlier runs of this harness; see
`engine_lessons.md` §9a.

- **Reproducibility bound, added 2026-08-02.** The registry runs its
  queries in one process, in order, so they are not independent: `paths.k`
  builds the TCSR permutation and every scan after it in the same process
  pays about **18%** for that index staying resident (isolated below, and
  fully reversible when it is dropped). Native is the only system in the
  table that builds such an index, so its `series.count`, `burst.zscore`
  and `agg.rel_bucket` cells carry a tax its baselines do not — a real cost
  of the system as measured, now disclosed rather than absorbed. Separately,
  re-measuring the same commits on a later day put `series.count` at 1M
  near 70 ms where the July sweep recorded 59.0; nothing in between touches
  a scan path, so **treat single cells as reproducible to roughly ±20%,
  not to the tenth of a millisecond they are printed with**. Conclusions in
  this file rest on ratios of 2× and up, which survive that band; a handful
  of the narrower ones are flagged where they do not.

## Results

Median milliseconds. Three datasets: the synthetic reference at 200k and 1M
events, and the frozen CollegeMsg replay (59,835 instantaneous events, real
timestamps, no corrections). Every (query, system, dataset) cell agrees on
the canonical hash, except where noted.

### synth, 200k events (four of the six, D-035/D-044)

Refreshed 2026-08-02 at commit `03a678c` in a single six-system run; the
graph engines' columns from the same run are in the Phase 3 table below.

| query | native | duckdb | postgres | clickhouse | fastest |
|---|---:|---:|---:|---:|---|
| hist.single | **0.1** | 9.8 | 0.3 | 10.0 | native |
| hist.asof | **0.1** | 9.1 | 0.2 | 10.0 | native |
| snap.hop2 | **19.6** | 46.2 | 67.0 | 284.3 | native |
| diff.global | **58.9** | 114.7 | 122.0 | 154.4 | native |
| reach.window | **14.7** | 26.0 | 740.3 | 1236.7 | native |
| paths.k | **9.4** | 16.5 | 22.4 | 154.9 | native |
| series.count | 16.3 | 43.7 | 74.4 | 16.7 | tie |
| burst.zscore | 17.7 | 44.3 | 75.0 | 17.1 | tie |
| nbr.evolution | 3.4 | 16.7 | **2.8** | 59.9 | postgres |
| coactive.narrow | **21.5** | 62.6 | 63.0 | 132.0 | native |
| resolve.substr | **3.1** | 19.5 | 5.8 | 11.9 | native |
| agg.rel_bucket | **14.5** | 537.3 | 422.9 | 32.6 | native |
| motif.filtered | **28.7** | 63.5 | 146.4 | 142.4 | native |

All thirteen hash-identical across all six systems (four here, two below).
Two rows changed hands since the July sweep and both are engine work, not
noise: **native's point lookups now beat PostgreSQL's** (0.1 vs 0.3 ms),
and `series.count`/`burst.zscore` are now *ties* with ClickHouse at this
scale where ClickHouse led 2×. Its lead is a scale effect, not a shape
effect — it starts at 200k as a tie, reaches 3.5× at 1M and 4.6× at 10M
(8.7× before D-046).
The new `agg.rel_bucket` runs the other way: **native leads it 2.2× at
200k** and loses it at 1M and 10M, so the crossover for grouped
aggregation sits between 200k and 1M. The baselines divide
the map cleanly: PostgreSQL owns indexed point shapes, ClickHouse owns
whole-window aggregation (the first system to beat native on any scan
shape at this scale), and both pay heavily for iterative traversal —
ClickHouse's reachability rounds cost ~1 ms of HTTP plus a table build
each, which is the honest price of expressing recursion in an engine
that does not natively offer it. Native holds 8 of 12.

### synth, 1M events (four systems)

Refreshed 2026-08-02 at commit `d12b30f`, when the registry gained its
thirteenth query. All four columns re-measured in the same run:

| query | native | duckdb | postgres | clickhouse | fastest |
|---|---:|---:|---:|---:|---|
| hist.single | **0.1** | 25.2 | 0.3 | 11.6 | native |
| hist.asof | **0.2** | 25.2 | **0.2** | 12.1 | tie |
| snap.hop2 | **95.1** | 198.0 | 266.3 | 352.0 | native |
| diff.global | **317.2** | 537.0 | 607.8 | 596.7 | native |
| reach.window | **117.1** | 159.4 | 6323.9 | 4032.6 | native |
| paths.k | **11.6** | 37.4 | 29.2 | 179.8 | native |
| series.count | 81.9 | 143.4 | 219.9 | **20.1** | clickhouse |
| burst.zscore | 83.7 | 146.2 | 219.3 | **20.6** | clickhouse |
| nbr.evolution | 7.7 | 48.8 | **3.1** | 56.8 | postgres |
| coactive.narrow | **105.2** | 111.6 | 194.2 | 106.5 | native† |
| resolve.substr | **6.8** | 87.8 | 19.0 | 17.7 | native |
| agg.rel_bucket | 65.1 | 3031.9 | 2387.5 | **36.6** | clickhouse |
| motif.filtered | **44.4** | 93.1 | 195.4 | 137.9 | native |

†1.3% apart, inside the ±20% reproducibility band above: read
`coactive.narrow` at 1M as a tie with ClickHouse, not a win. The interval
join has now changed hands three times as both sides improved, which is
the honest reading of a query neither system is built for.

Two cells moved for reasons worth naming rather than burying. Native's
**point lookups now beat PostgreSQL's** at this scale (0.1 ms against
0.3), which the 200k table above still shows the other way round.
`nbr.evolution` fell 23.6 → 7.7 ms on the scan-address work (D-039).
And `series.count`/`burst.zscore` read *slower* than the 59.0/60.3 this
table carried in July — not a code regression: §9g of `engine_lessons.md`
traces most of it to the traversal index that `paths.k` leaves resident
earlier in the same process, and the remainder to between-day drift on the
host. The ClickHouse gap on those two queries is real either way.

### synth, 10M events (abridged)

Refreshed 2026-08-02 at commit `cc49795` (native, DuckDB and ClickHouse
re-measured together after the D-046 scan work; the PostgreSQL 10M column,
measured once in July, is retained where no TGMS-side change affects it).
The `was` column is the previous full sweep of the same three systems at
`d12b30f`, earlier the same day:

| query | native | *was* | duckdb | postgres | clickhouse | fastest |
|---|---:|---:|---:|---:|---:|---|
| hist.single | **0.7** | 0.7 | 65.0 | **0.3**‡ | 12.2 | native |
| hist.asof | **0.7** | 0.7 | 65.6 | — | 14.1 | native |
| snap.hop2 | 1017.6 | 1029.6 | 1745.6 | 2506.8‡ | **764.8**† | clickhouse |
| diff.global | **3998.6** | 3983.1 | 5625.9 | 6670.6‡ | 5010.0 | native |
| reach.window | gr | gr | gr | 44400.4‡ | **4052.0** | guardrailed |
| paths.k | **15.5** | 16.2 | 82.9 | 37.2‡ | 240.1 | native |
| series.count | 183.7 | *352.1* | 782.0 | 2176.7‡ | **40.0** | clickhouse |
| burst.zscore | 185.3 | *351.1* | 787.1 | 2156.5‡ | **39.5** | clickhouse |
| nbr.evolution | 68.4 | 62.3 | 107.1 | **2.8**‡ | 88.8 | postgres |
| coactive.narrow | **166.7** | 172.5 | 222.1 | 1781.7‡ | 207.5 | native |
| resolve.substr | **95.4** | 95.3 | 838.0 | 200.5‡ | 122.2 | native |
| agg.rel_bucket | 331.1 | 334.7 | 35798.5 | — | **134.1** | clickhouse |
| motif.filtered | **75.9** | 78.1 | 164.9 | 661.3‡ | 224.2 | native |

‡July measurement, carried forward. `gr` = guardrailed (`E_COST`).

**All 13 queries agree across native, DuckDB and ClickHouse in this run.**
Four of them — `hist.single`, `hist.asof`, `snap.hop2`, `coactive.narrow` —
hash differently from the `d12b30f` run, *identically on all three systems*,
because those are the four whose answers carry `vid`/`tt` and each harness
run builds its own store from a fresh clock (D-023). The other nine hash
byte-identically across both runs and all systems.

**`series.count` 352.1 → 183.7 and `burst.zscore` 351.1 → 185.3** are the
D-046 scan work; no *native* cell moved by more than the ±20%
reproducibility band in either direction (`nbr.evolution` is 10% slower at
code neither change touches, which is what that band is for).

**DuckDB's `series.count` also improved, 981.6 → 782.0, and that one is
not drift.** Half of D-046 landed in the shared operator layer rather than
the engine: `edge_event_count` was asking for four columns and reading one,
and every backend honours a projection. We did not isolate it with an A/B,
so the attribution is by mechanism rather than measurement — but the
direction, the size and the code path all agree, and it is the same
pattern §9e recorded the first time. A fix that removes work improves the
baseline you are compared against, which is the only kind of speed-up that
is unambiguously real.

**`paths.k` at 10M is no longer refused.** The July table recorded it as a
guardrail firing on a query PostgreSQL answered in 37 ms — a cost-model
false positive we logged as known and tracked. Pricing the DFS by its
frontier rather than by the windowed scan (D-039) retired it: native now
answers in **16.2 ms**, the fastest of the four. A guardrail that refuses
work the system could do is a defect, and this is what closing one looks
like. `reach.window` is still refused, and PostgreSQL's 44 s for it is why
that one is not a defect.

†Re-measured after a plumbing fix: the BFS originally inlined the
reached-node id list in query text and blew `max_query_size` at 10M. With
node sets shipped through working tables the query hash-matches and runs
749.3 ms — **beating native**, whose 937 ms materializes the induced
snapshot serially. A cell that began as a defect record ended as a
ClickHouse win; both facts are kept.

The scaling stories the four-system sweep settles: **ClickHouse's
aggregation lead grows with scale** — 2× over native at 200k, 3.5× at 1M,
and **4.6× at 10M** (40.0 vs 183.7; it was 8.7× before the D-046 scan work
and 12× before the refresh before that) — it is simply the right engine for
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
materialization. *(That last inference is retired — see "Where the 10M scan
actually goes" below. The projection changed nothing because it was not
being applied to the fixed-width columns, and materialization was 47% of the
scan the whole time.)* NumPy's mask-and-bincount is 88 ms. What remained was the
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

## Racing the specialist on its own shape (D-044)

ClickHouse's aggregation lead was the clearest thing the baselines found:
8.7× at 10M on `series.count` as measured then, growing with scale. It was also the
capability the independent-question study ranked first. So the fourteenth
operator, `aggregate_events`, was built directly against that column —
count and distinct-endpoint counts over closed dimensions (time bucket,
rel_type, endpoint, label) — and the registry gained `agg.rel_bucket`:
count and distinct-dst by rel_type × time bucket over the full window, 196
groups, verified before timed against **four independent implementations
across six systems** — the operator (with its brute-force oracle behind it),
ClickHouse SQL, PostgreSQL SQL, and one Cypher statement that runs unchanged
on Neo4j and Memgraph — all agreeing on one canonical hash.

| scale | native | clickhouse | postgres | duckdb (portable) | neo4j | memgraph |
|---|---:|---:|---:|---:|---:|---:|
| 200k | **14.5** | 32.6 | 422.9 | 537.3 | 511.2 | 340.5 |
| 1M | 65.1 | **36.6** | 2387.5 | 3031.9 | — | — |
| 10M | 334.7 | **140.8** | — | 34515.9 | — | — |

**We take it at 200k and lose it after.** Native leads 2.2× at 200k;
ClickHouse holds it at 1.8× (1M) and 2.4× (10M), so the crossover sits
between the first two scales. That is the result, and it is a better one than it looks: the same
engine led `series.count` by 8.7× at 10M when this was written, so on the
query family the operator was designed for, the gap closed from roughly
nine-fold to two-fold. (D-046 has since taken `series.count` to 183.7 ms
and that lead to 4.6×, without moving `agg.rel_bucket` — see "Where the 10M
scan actually goes", which explains why the two were never the same cost.) Against the row stores it is not close — 37× faster than tuned
PostgreSQL at 1M, on a query PostgreSQL answers with a plain `GROUP BY`.

The interesting number is not in the table. At 10M, `agg.rel_bucket`
(334.7 ms) costs **less than `series.count` (352.1 ms)** — the same scan,
one bucket dimension, plus a second grouping dimension and an exact
distinct count over 10M endpoint ids, for no measurable extra time. The
two-phase design does what it was copied from ClickHouse to do: per-thread
partial states over the cluster-parallel scan, group keys as fixed-width
codes end to end (bucket index, global rel code, dense endpoint id), merged
deterministically so the answer is byte-identical at any thread count.
Grouping is free; **the residual gap to ClickHouse is entirely the scan
underneath it**, which is exactly where D-043's next item points and is a
far more actionable finding than "we are slower at aggregation."

> **Retracted 2026-08-03 — the paragraph above is wrong, and it is kept
> because the next section is what corrects it.** The two operators cost
> about the same, but not out of the same parts: `aggregate_events` calls
> `select` and stops, so *its* scan is 38.8 ms, not 352. The remaining
> ~270 ms is the aggregation kernel, most of it `count_distinct`. "The
> residual gap is the scan" is true of `series.count` and false of
> `agg.rel_bucket`, and the error was comparing two totals as if the word
> "scan" meant the same work on both sides. See D-046 below.

One cell deserves its own sentence, because it is ours and it is bad. The
**portable fallback — the same operator on the DuckDB backend — takes 34.5
seconds at 10M**, a hundred times the native kernel and fourteen times
PostgreSQL. It is vectorized NumPy, but it groups by `rel_type` as an array
of ten million Python strings, which is the dictionary-coding lesson the
native path was careful to obey and the portable path was not. Anyone using
the DuckDB backend for grouped aggregation at scale is paying for that
today; it is written here rather than left for them to discover.

## Where the 10M scan actually goes (D-046)

The section above ends by naming the scan as the residual gap. This one
measures it, because "the scan" was still a single number subtracted from a
Python wall clock. Six timers now live on the path — `select`,
`cluster_order` and `materialize` inside Rust, segment open, `eid`
derivation and the NumPy conversion at the PyO3 boundary — and every
condition below runs in **its own process** against a pre-built store
(lessons §9g: a resident index taxes any later scan 18%, so conditions
measured together are not independent). Commit `0a7d2ce`, xzgpu, 40 cores,
2026-08-02, median of 5 reps after one warm-up. Numbers on this page from
other days are not comparable with these to better than ±20%.

`series.count` at 10M, default width (16 workers):

| stage | ms | share of the operator |
|---|---:|---:|
| **operator total** | **463.3** | |
| ├ `edges_columnar` (adapter) | 349.4 | 75% |
| │ ├ segment open (warm) | 0.2 | |
| │ ├ **`select`** | **38.8** | 8% |
| │ ├ `cluster_order` | 0.3 | |
| │ ├ **`materialize`** | **220.0** | 47% |
| │ ├ `eid` (not projected) | 0.0 | |
| │ ├ **NumPy conversion** | **72.5** | 16% |
| │ └ residue (PyO3 marshalling) | 17.0 | 4% |
| ├ NumPy above the scan (mask + `bincount`) | 107.5 | 23% |
| └ unaccounted (validation, pagination) | 6.4 | 1% |

**Selection is 8% of it.** Every hypothesis this project has held about the
10M scan — that it is selection-bound, that the belief test or the
valid-time test per row is the cost, that a resident close index makes
visibility expensive — is refuted by that one row. The cost is *moving ten
million rows out of the engine*: materialization, the boundary, and the
NumPy pass above it are 400 of the 463 ms.

Three narrower conditions place it exactly.

**The projection never reached the fixed-width columns.** Same 10M scan,
three different `columns=`:

| projection | select | materialize | convert | wall |
|---|---:|---:|---:|---:|
| `src_id, dst_id, vt_s, vt_e` | 39.0 | 221.3 | 75.5 | 352.2 |
| `vt_s, vt_e` | 42.2 | 227.2 | 75.6 | 367.0 |
| `vt_s` | 42.5 | 229.2 | 74.5 | 360.4 |

Asking for one column costs what asking for four costs, because `copy_run`
honoured the projection for `vid` and the three string columns and copied
`vt_e`, `src_id` and `dst_id` unconditionally — and the boundary widened
both endpoint columns from `u32` to `i64` whatever was asked for. **This
retires a claim published above**: the 2026-08-01 note that "projecting down
to one column changes nothing … so the cost is not column materialization"
measured a projection that was not being applied. The conclusion drawn from
it was wrong in the strongest possible way — it steered the next three
sessions away from materialization, which is where 47% of the time was.

**The per-row predicates are noise.** Removing them one at a time:

| request | select | materialize | wall | rows out |
|---|---:|---:|---:|---:|
| `vt_min` + `vt_max` | 39.0 | 225.5 | 349.6 | 10,000,009 |
| `vt_max` only (no per-row `vt_e` test) | 38.1 | 217.7 | 341.8 | 10,000,009 |
| no window at all | 38.5 | 216.2 | 341.7 | 10,000,009 |
| `rel_types=["R"]` | 39.5 | 149.9 | 242.4 | 6,666,673 |

The `vt_e > vt_min` test that runs for every one of ten million rows costs
**~1 ms**. What `select` is actually doing is writing a `Vec<u32>` of ten
million row ids — the last row confirms it: filtering to two thirds of the
rows leaves `select` unchanged (the loop still visits every row) and cuts
materialize and convert by exactly a third.

**Materialization does not parallelize.** Thread sweep at 10M
(`TGMS_PARALLEL_MIN_ROWS=1`, so the row gate never decides instead of the
width; widths below 4 stay serial by `PARALLEL_SCAN_MIN_THREADS`):

| `TGMS_SCAN_THREADS` | select | cluster | materialize | convert | wall |
|---|---:|---:|---:|---:|---:|
| 1 | 177.2 | 0.2 | 318.9 | 72.4 | 586.9 |
| 2 *(serial)* | 173.0 | 0.2 | 331.9 | 77.7 | 605.8 |
| 4 | 111.3 | 0.4 | 243.0 | 72.9 | 439.1 |
| 8 | 64.8 | 0.4 | 236.8 | 72.6 | 392.1 |
| 16 | 38.7 | 0.3 | 227.1 | 72.6 | 350.5 |
| 32 | 38.0 | 0.3 | 226.9 | 72.7 | 353.1 |
| 40 | 32.7 | 0.3 | 226.8 | 72.6 | 343.8 |

`select` scales **5.4×**; `materialize` scales **1.41×** and stops moving at
8 workers; `convert` is flat by construction. The whole-scan "4.3× at 10M"
recorded in §14.3 of `eval_resources.md` is `select`'s number carrying two
stages that do not scale.

Why materialize does not scale, from the sub-split (CPU summed over workers,
so it exceeds the stage's wall time when the stage fanned out):

| width | materialize wall | k-way merge CPU | run-walk copy CPU | clusters | multi-member |
|---|---:|---:|---:|---:|---:|
| 1 | 321.9 | 33.5 | 143.3 | 371 | 1 |
| 16 | 225.1 | 49.1 | 358.0 | 371 | 1 |

Two things fall out. First, **serial materialization spends 145 of its
322 ms outside both halves** — that is the final `append`, which copies
every column of every cluster a second time into the aggregate. Second,
**the same copy costs 2.5× more CPU in parallel than serial** (358 vs
143 ms), because the stage spawns *one thread per cluster* — 371 of them on
a 40-core host — each allocating its own column buffers. `TGMS_SCAN_THREADS`
never reaches this stage: it gates it and does not size it.

The cluster shape also contradicts the note above it. "A corrected store is
almost-all singletons plus small local clusters" is right at 10M (370 of 371
clusters are singletons) and **wrong at 1M**, where 19 of 38 clusters are
multi-member and the k-way merge is 29 of the 42 ms materialize takes. The
same code has opposite cost profiles at the two scales.

For completeness, the two edges of the picture:

| condition | 10M | 1M |
|---|---:|---:|
| `tgms.open` in a fresh process | 24.7–25.6 s | 2.6–2.7 s |
| first (tiny) scan — opens 459 segments, decodes FOR columns | 2.57 s | — |
| second identical scan | 0.7 ms | — |
| `series.count` operator | 463.3 | 85.2 |
| … of which `select` / `materialize` / `convert` | 38.8 / 220.0 / 72.5 | 16.5 / 46.1 / 2.3 |

The ~25 s store open is what `eval_resources.md` §15 recorded as "a fresh
process pays ~28 s at 10M on its first query, whatever it is"; the split
above attributes it: essentially all of it is `tgms.open`, and 2.6 s more is
the first scan mapping and decoding 459 segments. Neither is on the measured
path of any published number, and neither is touched here.

### One published claim this profile overturns, beyond the projection

D-044 concluded that "grouping is free; the residual gap to ClickHouse is
entirely the scan underneath it", from `agg.rel_bucket` (334.7 ms) costing
less than `series.count` (352.1 ms) at 10M. The two do not share a scan.
`aggregate_events` calls `select` and nothing else — no materialization, no
boundary, no NumPy — so its scan is the **38.8 ms** row above, not 352.
Everything else it costs is the aggregation kernel:

| width | `agg.rel_bucket` | of which `select` | kernel |
|---|---:|---:|---:|
| 1 | 693.2 | ~177 | ~516 |
| 4 | 497.2 | ~111 | ~386 |
| 16 | 313.4 | ~39 | ~274 |
| 40 | 277.0 | ~33 | ~244 |

So on that query the gap to ClickHouse's 140.8 ms is **the kernel, not the
scan** — the opposite attribution to the one published. The claim was not
wrong about the comparison it made (the two operators really do cost about
the same); it was wrong about what the two costs were made of, because
"scan" meant `select` on one side of the comparison and the whole
materialize-and-cross-the-boundary path on the other. The scan is still the
right target for `series.count` and `burst.zscore`, which is where D-046
spends itself; `aggregate_events`' kernel is now a separate, priced item.

### What closing the two named stages bought

Two changes, each aimed at a row of the table above, each re-measured on the
same store the same day in a fresh process per condition.

1. **The projection now reaches the fixed-width columns**, and the boundary
   emits only what was asked for. `edge_event_count` and the event-rate
   burst target were also asking for four columns and reading one.
2. **Materialization is sized by the configured width** — contiguous cluster
   ranges balanced by rows, not one thread per cluster — **and the parts are
   concatenated once**, each into its own disjoint slice, instead of being
   appended into a growing buffer.

| 10M, default width | before | + projection | + materialize |
|---|---:|---:|---:|
| scan, 4 int columns | 352.2 | 360.6 | **219.1** |
| … `select` | 39.0 | 37.7 | 35.0 |
| … `materialize` | 221.3 | 235.7 | **97.1** |
| … NumPy conversion | 75.5 | 77.0 | 77.7 |
| scan, `vt_s` only | 360.4 | **142.5** | **99.9** |
| … `materialize` | 229.2 | 101.7 | 57.3 |
| … NumPy conversion | 74.5 | **0.0** | 0.0 |
| **`series.count` operator** | **463.3** | **283.1** | **214.9** |

**2.16× on the operator, 3.6× on the scan it issues.** The four-column scan
— what every other scan-bound query pays — is 1.6× faster on the
materialization change alone, which is the part that helps queries nobody
aimed at.

Materialization also scales now, where it did not:

| `TGMS_SCAN_THREADS` (10M, 4 int columns) | materialize before | after |
|---|---:|---:|
| 1 | 318.9 | 200.8 |
| 16 | 227.1 | 97.1 |
| 40 | 226.8 | 110.6 |
| speed-up 1 → 16 | 1.41× | **2.07×** |

40 workers is now *worse* than 16 on a 40-core host — the concatenation
spawns a second wave of workers, so asking for the whole machine
oversubscribes it. The shipped default (16) is the measured best, which is
where it already was.

At 1M the same changes are small and positive: `series.count` 85.2 → 66.8,
the one-column scan 65.5 → 48.9. Nothing regressed at the scale where the
parallel gates keep everything serial.

### What is left, priced rather than chased

`series.count` is now **214.9 ms** against ClickHouse's 40.3, and the split
has moved: `select` 35, `materialize` 57, boundary 0, and **106 ms of NumPy
above the engine** — the mask, the divide and the `bincount` that turn ten
million returned timestamps into a hundred bucket counts. Over half the
remaining time is the operator refusing to push its aggregation down.

That structure has a name and now a price. `aggregate_events` already
computes exactly this shape without materializing anything, and on the same
store, the same day:

| query at 10M | ms |
|---|---:|
| count by time bucket, through the aggregation kernel | **93.7** |
| count by rel_type × time bucket | 91.5 |
| count + distinct-dst by rel_type × time bucket (`agg.rel_bucket`) | 323.6 |
| `series.count`, through the scan and NumPy | 214.9 |

So routing `graph_metric_timeseries`' event metrics through the O14 kernel
is worth roughly another **2.3×** on `series.count` and `burst.zscore`, and
would land them at ~94 ms against ClickHouse's 40 — the same order at last.
It is a change to an operator's execution strategy, not to the scan, so it
is named here with its number rather than folded into a scan decision; D-046
records it as the next item.

`select`'s own residue is the same shape one stage earlier. It costs 35 ms
at 16 workers and 178 serial whatever predicates it is given, because what
it is doing is writing a `Vec<u32>` of ten million row ids — 40 MB per call
that materialization then reads back to find the contiguous runs it already
knew about. A run-encoded `Selection` would delete both halves of that; it
is the third item on this list and the only one still inside the scan.

One residue inside materialization is worth naming too, because the split
still reports it: at 10M and 16 workers the stage costs 97 ms while its two
measured halves (the k-way merge, 42 ms of CPU, and the run-walk copy,
124 ms of CPU) account for about 10 ms of wall between them. The difference
is allocation, imbalance, and one copy that survives — each worker still
gathers its clusters into a part before the parts are concatenated. Letting
`materialize_cluster` append straight into the worker's part would remove
that copy; it is a smaller lever than the kernel route above, and it is not
taken here.

The third number in that table is its own result: **`count_distinct` is
232 ms of `agg.rel_bucket`'s 324** (91.5 without it). The gap between
`agg.rel_bucket` and ClickHouse's 140.8 ms is one aggregate — exact distinct
counting over ten million endpoint ids — and not the scan, not grouping, and
not the two-phase design.

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

### Phase 3: the graph baselines (200k, D-036/D-037)

Refreshed 2026-08-02 at commit `03a678c`, in the same six-system run as
the table above:

| query | native | neo4j | memgraph | fastest |
|---|---:|---:|---:|---|
| hist.single | **0.1** | 9.7 | 1.0 | native |
| hist.asof | **0.1** | 7.6 | 1.0 | native |
| snap.hop2 | **19.6** | 666.5 | 619.8 | native |
| diff.global | **58.9** | 1811.7 | 1730.9 | native |
| reach.window | **14.7** | 7265.5 | 3860.9 | native |
| paths.k | **9.4** | 659.2 | 717.2 | native |
| series.count | **16.3** | 291.9 | 171.4 | native |
| burst.zscore | **17.7** | 291.9 | 170.5 | native |
| nbr.evolution | **3.4** | 15.1 | 10.0 | native |
| coactive.narrow | **21.5** | 100.2 | 44.2 | native |
| resolve.substr | **3.1** | 113.4 | 92.5 | native |
| agg.rel_bucket | **14.5** | 511.2 | 340.5 | native |
| motif.filtered | **28.7** | 5514.1 | 2069.8 | native |

All thirteen hash-identical across the three systems — including the
grouped-aggregation query, whose Cypher twin is one statement with no
Python-driven rounds, so this is the graph engines reading at their most
natural rather than handicapped. The result Phase 3
existed to test is unambiguous: **on the traversal family — the graph
engines' home turf — native wins by one to two orders of magnitude**
(reachability 17 ms vs 3.8–7.3 s; the closed-triangle motif 40 ms vs
2.1–5.1 s, despite Cypher expressing that query most naturally of any
system). The bi-temporal predicates are the reason: every hop re-filters
by belief and validity on relationship properties, which no graph index
here accelerates, while the native engine's clustering and kernels were
built for exactly those predicates. **Native now takes all thirteen**:
the two small incidence-shaped queries Memgraph held in July
(`nbr.evolution`, `coactive.narrow`) came back on the scan-address work of
D-039, which is where those two spent their time. Memgraph still runs the
same Cypher ~2× faster than Neo4j throughout. One measurement-quality
note: Neo4j's first run of this table sat at 47 s *per reachability
round* until `Entity.dense_id` got the index the Memgraph DDL already
had — found live via SHOW TRANSACTIONS, fixed, and re-run; the numbers
above are post-fix (D-030: baseline quality is part of what is measured).

### Four systems, one dataset, one accounting

Whole store on disk ÷ 1,000,269 edge rows, all loaded from the same 1M
event log on xzgpu, measured in one run:

| system | bytes | B/row | × native |
|---|---:|---:|---:|
| native (as built, pre-gc) | 48.7 MB | 48.7 | 1.0× |
| native (post-gc, D-034) | — | **25.1** | 0.52× |
| clickhouse (MergeTree, lz4) | 78.4 MB | 78.4 | 1.6× |
| duckdb | 187.7 MB | 187.7 | 3.9× |
| postgres (8 registry indexes) | 549.8 MB | 549.7 | 11.3× |

What each number carries, because the comparison is only honest with the
asymmetries stated: **native** counts segments, manifests, close runs, and
dictionary — but its query indexes (postings, TCSR) live in memory and are
not persisted, so its disk figure buys less query readiness than the
others' (and, per D-045, that residency has a measured latency cost of its
own on later scans); the as-built row still holds ~250 uncollected manifest
generations, the post-gc row is D-034's collected figure. **ClickHouse**
is the only other compressed representation (lz4 MergeTree, no secondary
indexes) and lands within 1.6× of native's as-built bytes — the closest
any baseline comes. **PostgreSQL**'s 11× is mostly deliberate: 366 MB of
its total is the eight covering indexes the registry queries earn their
speed from; heap alone is ~182 B/row. **DuckDB** is a single uncompressed
columnar file. Ratios against post-gc native roughly double everywhere.
