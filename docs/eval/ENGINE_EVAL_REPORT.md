# TGMS native engine: evaluation report

Six systems, one thirteen-query registry, three synthetic scales plus one
real dataset, every answered cell verified by canonical hash before it was
timed. Numbers: `docs/eval_phase0.md` (tables with receipts) and
`benchmarks/results-v1/` (raw records, timings included). Semantics:
`docs/eval_semantics.md`. This report interprets; it does not re-tabulate.

## The four headline results

**1. Correctness portability.** All thirteen registry queries are
`equivalent` on all six systems — nothing was `unsupported` anywhere. The
bi-temporal semantics that motivated a custom engine are *expressible* in
SQL and Cypher; what they are not is *fast* (result 3). The hash oracle
caught every porting error before it could become a wrong number, and it
caught two real defects inside TGMS itself (the resolve kernel/oracle
divergence, D-031; the motif cost-model false positive).

**2. The native engine is the best generalist.** At every scale it is
fastest on the majority of queries and never catastrophically slow on any.
Its losses are specific and explained, and two of them have since closed:
whole-window aggregation to ClickHouse (8.7× at 10M — the right engine for
that shape) and the 10M 2-hop snapshot to ClickHouse post-fix remain;
indexed point lookups no longer go to PostgreSQL (0.1 vs 0.3 ms at 200k
and 1M), and the two small incidence queries came back from Memgraph on
the D-039 scan-address work, so the 200k graph table is now a native
sweep. The one new query since — grouped aggregation, D-044 — splits by
scale: native leads 2.2× at 200k, ClickHouse leads 1.8–2.4× above it.

**3. Specialists win their shape; nobody else's.** ClickHouse's
aggregation lead grows with scale but it pays 100–400× on traversal
against native. The graph engines lose their own home turf by one to two
orders of magnitude — per-hop bi-temporal predicates on relationship
properties defeat their indexing, while native's valid-time clustering was
built for exactly those predicates. Cypher expressed the motif most
elegantly and ran it 50–125× slower than the native kernel: query
elegance and execution speed are independent axes.

**4. Storage.** Segments at 24.6 B/row (D-032/33) — 0.132× DuckDB, and
1.6× *smaller* than ClickHouse's lz4 MergeTree, the only other compressed
representation. Whole-store 25.1 B/row after generation collection
(D-034). PostgreSQL's 549.7 B/row is two-thirds deliberate index spend —
that is what its 0.3 ms lookups cost in bytes.

## The write path (only native and duckdb implement it)

Batched commits are healthy (flat fsync floor, 46k ev/s at batch=1000,
2× DuckDB bulk load; corrections 4.8 ms). The singleton-append and
correction-bloat pathologies all traced to manifest retention and were
fixed by generation collection. See `docs/eval_writes.md`.

## Performance envelope (for TGMS users)

- Point history: ~1 ms warm, flat to 10M+.
- Selective scans, joins, motifs, diffs: tens of ms at 1M, hundreds at
  10M; parallel scan and cluster merge keep them ahead of every baseline.
- Whole-window aggregation: native is fine (0.5 s at 10M) but ClickHouse
  is the tool if that is the workload (40 ms at 10M).
- Traversal: native or nothing at scale; guardrails refuse what would
  exhaust the machine, and the baselines' 4–44 s answers at 10M are the
  calibration for that refusal.
- Writes: batch. Singleton commits pay the durability floor by design.

## Limitations, honestly

- One host; one synthetic family plus one real dataset; no concurrency,
  cold-cache, or memory-pressure axes (plan §14–15 remain).
- Baseline query quality is ours, and twice it was the bottleneck
  (PostgreSQL recursion, Neo4j's missing index) — found and fixed under
  D-030, but a vendor expert might do better still.
- The two guardrail refusals at 10M include one known cost-model false
  positive (paths.k).
- No automated regression dashboard yet; the substitute is that the
  harness exits nonzero on any hash mismatch, so CI can run it.
- Measurement protocol is §16-conformant for repetitions and statistics,
  but earlier published tables (pre-correction) were min-of-3 mislabeled
  as p50 — corrected in place with a note, no conclusion flipped.

## Decision posture (plan §25)

Every adoption criterion is met: 100% digest parity, floors beaten,
geometric-mean speedup over DuckDB well past 2×, storage materially
smaller with overheads documented, wheel installable without a Rust
toolchain, fault-injection matrix green, DuckDB fallback intact. The
native engine has been the right default since M3; this evaluation
retires the remaining doubt with receipts.
