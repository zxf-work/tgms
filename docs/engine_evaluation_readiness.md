# Native engine — evaluation readiness

Where the engine stands against the evaluation plan's own adoption criteria,
and what has to exist before a cross-system study can start. Updated
2026-07-29 after the kernel work.

## Is the implementation done?

**Substitutable: yes. Complete: nearly.** The engine passes every correctness
gate and beats DuckDB on every benchmarked operator. All three kernels the
plan benchmarks by name are now settled — two built, one measured and
deliberately not built. What remains is TCSR persistence and three
operational items, none of which blocks the Phase 1 decision.

### Done

- All bi-temporal semantics, behind the unchanged `StorageAdapter` ABC.
- Segment format, generation manifests, close runs, adaptive visibility,
  compaction, verification.
- Kernels: δ-motif join, interval join (`co_active`), identity postings,
  windowed scan with projection pushdown.
- No third-party storage engine in the runtime; DuckDB and Kuzu are extras.
- Default backend for new stores, with on-disk layout detection so existing
  stores keep their own engine.

### Open

| gap | state | matters for |
|---|---|---|
| Time-bucket group aggregation | ~~open~~ **closed, not built** — measured at 9% of its operator (4.9 ms of 53.2 ms at 1M); the scan is 86%. A kernel could not repay itself. | plan §8.4 |
| TCSR traversal | build cost fixed (2803 ms → 665 ms via a stable single-key sort); still rebuilt per process and invalidated by writes, so on-disk persistence remains open | plan §8.5 |
| Name lookup index | **closed** — `name` promoted to a typed column and matching moved into the engine (142 ms → 13.5 ms at 1M) | plan §8.6 |
| Event-log offset / chain in `commit` | stubbed; recovery is full replay, not suffix replay | plan §12, §18 |
| `tgms store verify` CLI | the engine call exists, no subcommand wraps it | plan §23 |
| Compression | deliberately deferred, gated on the uncompressed baseline | plan §20.8 |

Of the three kernels the plan benchmarks by name, two are now closed and the
third turned out not to need building: measurement showed bucket aggregation
is 9% of its operator, so a native kernel could not repay the complexity.
What remains for TCSR is persistence, not speed — the build itself is now
~9 ms, and the cost that made it look urgent was a four-key sort.

The general lesson, which held nine times across this work: measure the layer
before optimizing it. Twice that meant discovering the fix was elsewhere;
once it meant not writing the code at all.

## Against the plan's adoption criteria (§25)

### Correctness — met

- Canonical digest parity: the frozen CollegeMsg log and randomized logs
  replay to byte-identical `store_digest` on both backends (`scripts/ab_digest.py`).
- Randomized oracle suites: the full human-owned suite, including the
  500-case operator oracle and the metamorphic tests, passes unmodified
  under `TGMS_TEST_BACKEND=native`.
- Compaction preserves history: asserted on the full logical listing plus
  historical queries at eight `as_of` points either side of a correction.
- Fault injection: 14 cases, each requiring the previous complete generation
  or a named error — never a silent mixed state.

### High-frequency performance — met

At 1M events on xzgpu, same event log replayed into both backends
(`docs/bench_ops.md`):

- No operator is slower than DuckDB, against a bar of "no more than 20%
  slower without a documented reason".
- Geometric-mean speedup across the eleven benchmarked operators is ≈ 2.7×,
  against a target of 2×.
- `co_active`, the named hotspot, went from ~5.3 s to 32.8 ms.

### Storage — met, with less headroom than previously claimed

Measured at 1M edge versions, |V| = 20,000, both backends built from the same
event log. Per row, counting node versions too:

| | MB | B/row | vs DuckDB |
|---|---:|---:|---:|
| segments | 65.97 | 64.67 | |
| dictionary | 0.35 | 0.34 | |
| manifests | 0.15 | 0.15 | |
| close runs | 0.00 | 0.00 | |
| **on disk today** | **66.47** | **65.17** | **0.350×** |
| + identity postings (persisted) | 20.00 | 19.61 | |
| + TCSR, permutation form | 8.32 | 8.16 | |
| **projected with indexes** | **94.79** | **92.93** | **0.499×** |
| DuckDB, same log | 190.07 | 186.34 | 1.0 |

**An earlier 0.22× figure in this repository was wrong.** It compared a
58 B/row native measurement against a 260 B/row DuckDB number taken from a
different store at a different scale. Like for like, on identical content,
the ratio today is 0.350×.

That still meets the criterion, but the margin matters: once postings and
TCSR are persisted — which is what closing the TCSR gap means — the ratio is
**0.499×**, effectively touching the 0.5× gate. Indexes cost more than the
base rows saved. Compression (blueprint C4) is what would restore headroom,
and this is the measurement that makes it worth doing rather than a
deferred nicety.

**Temporary compaction space: +99%, now reclaimable.** Compaction rewrites
content into fresh segments and deletes nothing itself, so peak usage is
twice the store — 66.47 MB became 132.41 MB while merging 24 segments into
3. Capacity planning still has to budget 2× *during* the pass, but the
explicit `tgms store gc` (D-034) now collects the superseded files
afterwards: on the 1M store, 50.0 MB peak returned to 24.8 MB once gc
removed the 283 superseded segments.

### Operations — met

Prebuilt wheel installs with no Rust toolchain (verified in a clean venv with
neither optional extra); deterministic open and recovery; corruption errors
name the file and a remedy; DuckDB remains available as an extra.

**Conclusion:** the Phase 1 decision — is the native engine ready to be the
default? — is answerable now and the answer is yes, with the storage
measurement outstanding. Phase 0's exit criterion (identical hashes on native
and DuckDB for all oracle plans) is already satisfied.

## What the evaluation needs that does not exist yet

The plan's harness (§6, §11, §17, §18, §23) is almost entirely unbuilt. In
dependency order:

1. **Canonical data layer** (§6) — the logical node/edge version tables, the
   transformation manifest, and the versioned / current-snapshot modes. Every
   other system loads from this, so it comes first.
2. **Query registry and result canonicalizer** (§11, §18.2) — one identifier
   per logical query, with canonical result hashing so systems can be
   compared without trusting each other's formatting. `scripts/ab_digest.py`
   is the two-backend prototype of this idea and can be generalized.
3. **Run manifest and environment capture** (§16, §23) — hardware, versions,
   cache state, repetitions. The `§8.4 receipts` convention already used in
   `docs/bench_ops.md` is the right shape; it needs to be produced by the
   harness rather than written by hand.
4. **Cross-system semantics file** (§18.4) — where each system's semantics
   diverge, recorded rather than smoothed over. This is what keeps the study
   honest when a baseline cannot express a temporal predicate.
5. **Per-system adapters** (§4) — PostgreSQL and Neo4j are a meaningful
   infrastructure lift; ClickHouse, Memgraph, and DuckPGQ more so. None
   should start before 1–3 exist.

## Recommended sequencing

1. **Measure index and compaction storage overhead** — the one outstanding
   §25 criterion, and the only thing standing between the current numbers and
   a defensible storage claim.
2. **Build the Phase 0 harness** against native and DuckDB only. Phase 0's
   exit criterion is already met, so this is about making the result
   reproducible rather than discovering it.
3. **Then** add external systems, starting with PostgreSQL, the cheapest
   baseline with genuinely equivalent semantics.

TCSR persistence and the operational items (suffix replay, the `verify`
subcommand, compression) can proceed in parallel — none of them changes a
measured number, so the harness will not have to be re-run because of them.
That was not true of the kernels, which is why they came first.
