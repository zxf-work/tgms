# Write-path evaluation (plan §12)

Regenerate: `uv run python scripts/eval_writes.py --json out.json`

## Receipts (spec §8.4)

- commit `caa5246`, xzgpu (40 cores, 93 GB, Linux 5.4), best of 3 for loads,
  p50/p95 over sustained commits for appends; raw record `eval-writes.json`
- native store carries compression (D-032/D-033) and the segment cache
- PostgreSQL absent by design: it never implements the write semantics (D-030)

## Bulk load (best of 3)

| events | native | duckdb |
|---|---:|---:|
| 200k | 4.58 s (43.7k ev/s) | 8.20 s (24.4k ev/s) |
| 1M | 23.56 s (42.4k ev/s) | 45.97 s (21.8k ev/s) |

Native holds ~43k ev/s flat across 5× the data; both scale linearly.

## Sustained appends, by batch size (2,000 events total)

| batch | native p50 / p95 (ms) | native ev/s | duckdb p50 / p95 (ms) | duckdb ev/s |
|---|---:|---:|---:|---:|
| 1 | 9.83 / 17.71 | 96 | **4.11** / 5.37 | 230 |
| 10 | **4.06** / 6.30 | 1,846 | 12.97 / 19.50 | 724 |
| 100 | **5.28** / 6.13 | 18,703 | 88.07 / 95.08 | 1,120 |
| 1000 | **21.76** / 21.43 | 45,958 | 814.02 / 809.04 | 1,228 |

Two structures, visible at a glance. Native's per-commit cost is nearly flat
— the durable-generation fsync floor (`engine_lessons.md` §7) — so
throughput scales with batch size, 37× from b=1 to b=1000. DuckDB's
per-commit cost grows with batch size instead, capping at ~1.2k ev/s.

**Native loses only at batch=1, and not to fsync.** Its singleton commits are
9.8 ms p50 with a p95 of 17.7 *and climbing through the run*: every commit
writes a full manifest naming every segment, so 2,000 singleton commits make
each commit progressively more expensive. That is the manifest-retention
problem measured from the write side.

## Corrections (200 single-op, against a 100k-row store)

| | native | duckdb |
|---|---:|---:|
| p50 / p95 (ms) | **4.84** / 6.98 | 9.64 / 13.86 |
| store growth per correction | 63,671 B | 812 B |

The latency headline: 4.84 ms, down from the ~45 ms recorded in
`engine_lessons.md` §9b — the point-read fix and segment cache reached the
write path, as predicted there ("writes read").

The space headline is the same finding a third way: **64 KB per correction,
of which the corrected data is well under 1 KB** — the rest is a fresh full
manifest per commit. DuckDB, with no manifest-per-generation design, pays
812 B.

## Compaction (100k-row store, 250 corrections + retractions folded)

- **0.41 s**; 253 segments → 2; 250 close runs folded
- **digest preserved** — the bi-temporal answer set is untouched, by check
  rather than by assumption
- bytes on disk grew 28.8 → 31.2 MB: compaction writes new segments and
  deletes nothing; `tgms store gc` (D-034, landed after this run) collects
  the superseded files once they leave the retention window

## What §12 established

1. The batched write path is healthy: flat commit floor, 46k ev/s appends,
   2× DuckDB on bulk load, corrections at 4.8 ms.
2. Every write-side pathology measured here — singleton-append drift,
   64 KB corrections, compaction that grows the store — is the *same*
   root cause: full manifests per commit, never collected. Generation
   collection (D-034) has since landed against exactly these targets:
   at 1M rows, whole-store 48.2 → 25.1 B/row after gc alone, 24.6 after
   compaction plus gc (`eval-1m-gc.json` on xzgpu).
