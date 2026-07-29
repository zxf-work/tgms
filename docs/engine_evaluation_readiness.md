# Native engine — evaluation readiness

Where the engine stands against the evaluation plan's own adoption criteria,
and what has to exist before a cross-system study can start. Written
2026-07-29 at commit `cc1abd8`.

## Is the implementation done?

**Substitutable: yes. Complete: no.** The engine passes every correctness
gate and beats DuckDB on every benchmarked operator, but three planned
kernels and two operational items are still open, and two of the three are
things the evaluation explicitly measures.

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

### Storage — partly met

- Per-row size is ≈ 0.22× DuckDB's, uncompressed and with no codecs.
- **Not yet measured:** index and sidecar overhead, and temporary compaction
  space. Postings and TCSR are currently in-memory, so today's on-disk figure
  understates a persisted design. Both are required by §25 and should be
  measured before the storage claim is made.

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

1. **Close the three kernel gaps first** — name index, TCSR persistence,
   bucket aggregation. They are days of work, they are the parts the plan
   benchmarks by name, and measuring NumPy stand-ins would produce numbers
   that have to be thrown away and re-run.
2. **Measure index and compaction storage overhead**, closing the one
   outstanding §25 criterion.
3. **Build the Phase 0 harness** (items 1–4 above) against native and DuckDB
   only. Phase 0's exit criterion is already met, so this is about making the
   result reproducible rather than discovering it.
4. **Then** add external systems, starting with PostgreSQL, which is the
   cheapest baseline with genuinely equivalent semantics.

Doing 3 before 1 is the tempting order and the wrong one: the harness would
measure a configuration that is about to change.
