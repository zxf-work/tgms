# TGMS operator micro-benchmarks

Regenerate with `tgms bench ops --store <store> --out docs/bench_ops.md`.
The comparison below was assembled from two such runs, one per backend.

This replaces an earlier 100k-event run; the M3 acceptance targets are
stated at 1M, so the benchmark now uses that scale.

## Receipts (spec §8.4)

- commit `94d439d` — native engine, WP-N4 scan work
- machine: xzgpu, 40 cores / 93 GB — the host the M3 floor was measured on
- Python 3.12.13, `rustc 1.97.1`, 7 repeats per case
- store: `stores/synth-1m` — |V| = 20,000, 1,000,000 edge versions
- **both backends replayed from the same event log**, and both report store
  digest `682f1194f6ca335f` — one dataset measured twice, not two datasets
  compared

Replay rather than re-ingest is required here: a fresh `tgms ingest` stamps
transaction times from the clock, so two independently built stores of the
same data legitimately differ (D-023).

## Results

p50 milliseconds. "floor" is the M3 acceptance target from
`TECHNICAL_REPORT.md` §8.1 where one was set.

| operator | case | floor | duckdb | native | native vs duckdb |
|---|---|---:|---:|---:|---:|
| entity_history | base | 51 | 53.0 | **19.5** | 2.7× |
| snapshot_subgraph | hop2 | 99 | 98.4 | **26.8** | 3.7× |
| diff_snapshots | global | 163 | 164.6 | **59.4** | 2.8× |
| temporal_reachability | w10 | — | 61.5 | **18.3** | 3.4× |
| temporal_reachability | w50 | — | 243.4 | **177.3** | 1.4× |
| temporal_paths | w10 | — | 32.3 | **3.6** | 9.0× |
| graph_metric_timeseries | events-100b | 155 | 142.4 | **99.7** | 1.4× |
| burst_detection | zscore | — | 153.3 | **103.5** | 1.5× |
| neighborhood_evolution | base | — | 52.0 | **9.3** | 5.6× |
| co_active | src-narrow | — | 118.7 | **32.8** | 3.6× |
| resolve_entities | substr | — | 185.3 | **142.0** | 1.3× |

`count_temporal_motifs` (tri-w10) is `E_COST`-gated on both backends at this
scale without a `node_filter` — the guardrail behaving as designed, not a
failure. Motif matching itself runs natively; see `crates/tgms-engine-core/src/motif.rs`.

Every floor is met, and the native engine is faster on every operator
benchmarked. `co_active` is the largest change in absolute terms: it was the
known ~5.3 s hotspot at 1M events before the interval-join kernel landed.

## What moved, and what the numbers taught

- **`co_active`** — the candidate-range walk moved into a native kernel. The
  loop turned out *not* to be the dominant cost; batching per-row `uids_for`
  calls and pushing endpoint specs down into the scan mattered more.
- **`graph_metric_timeseries`, `burst_detection`, `temporal_reachability`
  w50** — all three regressed against DuckDB on the first native run
  (316.6 / 371.7 / 313.9 ms). The cause was `vid`: two integer columns in the
  store, but a 24-character hex string at the boundary, built for every row
  even when the caller had projected it away. Gating it took a 1M-row
  integer-column scan from 172.8 ms to 43.6 ms — the shape all three use.
- **`entity_history`** — served by the identity postings index rather than by
  materializing every version and filtering.

## Caveat worth knowing

The on-disk `stores/synth-1m` predates D-011, which added the `source` and
`provenance_ref` columns. Benching DuckDB directly against it fails
`entity_history` and `resolve_entities` with a `BinderException` — a stale
store, not a code fault. The numbers above come from a store replayed onto
the current schema, which is also what makes the digests match.

## Storage

The same content occupies ~58 bytes per edge version in the native format,
uncompressed and with no codecs, against roughly 260 bytes per row for
DuckDB (≈ 0.22×; 578 MB vs 2.6 GB at 1e7 events). Compression is deliberately
deferred until the uncompressed baseline is established (blueprint C4).
