# TGMS Native Engine — Architecture Blueprint & Component Roadmap (v3)

*v3, 2026-07-28 — revised after review round 2 (`TGMS_NATIVE_ENGINE_V2_REVIEW.md`);
round 1 = `TGMS_NATIVE_ENGINE_REVIEW.md`. Status: **design phase — no
implementation yet.** Priority per PI: effective, elegant implementation;
publication is a possible outcome, not a design constraint.*

**Changes from v2, in one paragraph.** Round 2 endorsed the v2 pushbacks
(no optimizer/IR, no query-mix adaptivity, no comparative baselines,
derived IDs) and landed five corrections, all adopted: (1) composite-key
tie groups must never split across segment boundaries, and hash derivation
gets one canonical implementation in the engine core with an ingest parity
assertion; (2) per-segment TCSR uses an adaptive sparse active-vertex form
— dense `offsets[|V|+1]` per segment is O(|V|·segments) and indefensible;
(3) event-lane membership is defined by actual partition crossings (K = 2),
not by a width comparison that adaptive partitions invalidate; lane
assignment is physical; (4) generation GC is conservative-explicit (none in
the first version); (5) the compressed-size estimate is corrected to
14–24 B/row (~1.4–2.4 GB @1e8; v2's 6–10 B ignored 8 incompressible
vid64 bytes). Additional adoptions: u32 entity ids (16 B hot row),
group-commit durability, monotone *prefix-max* staircase (precise
definition), E3 semantic-completeness vs E4 indexed-performance gate
split, a first-usable-engine scope cut, and — the big one, **decided
2026-07-28** — a **Rust-first persistent core** (thin PyO3/maturin layer)
replacing the Python-first-then-port staging: the persistent format is
implemented once, in Rust; Python keeps the entire semantics layer, CLI,
adapters, and tests; NumPy is for prototyping and benchmark oracles, never
the authoritative store.

---

## 1. The design contract: minimal, closed, fast

The engine is **not** a database. It is a storage kernel for exactly one
schema (bi-temporal version rows) and one query surface (the ~12
`StorageAdapter` primitives + 5 fixed computation kernels). The surface is
**closed**: no SQL, no ad-hoc predicates, no general joins, no
multi-writer. Every code path is specialized to its access pattern.

Implementation values (round-2 framing, adopted): fewer physical
representations; fewer configuration parameters; deterministic and
inspectable files; predictable failure and recovery behavior; prebuilt
wheels (`pip install tgms` stays one command); a narrow stable API;
excellent diagnostics on failure.

Measured call surface (call-sites: ops_snapshot 19, ops_series 9,
ops_paths 8, ops_motifs 3):

| Primitive | Access pattern |
|---|---|
| `edges_columnar(as_of_tt, vt_min, vt_max, rel_types, columns, touching_ids)` | windowed columnar scan, sorted (vt_s, vid) — the workhorse |
| `nodes_columnar(...)` | same, small |
| `believed_{node,edge}_versions(identity)` | point lookup + belief filter (hot in write-path overlap checks, O1) |
| `nodes_with_believed_versions(uids)` | batched existence probe (bulk ingest) |
| `props_for_vids(vids)` | lazy point fetch |
| `dense_ids / uids_for / num_entities` | dictionary |
| `insert_* / close_*_versions` | append + rare tt_e close |
| `stats()` | maintained incrementally |
| `store_digest()` | full logical scan (replay verification only) |
| kernels | motif join, interval join, TCSR traversal, time-bucket group-agg, name lookup |

Non-goals, enforced by absence: predicates beyond the scan signature,
string predicates other than name lookup, mutation other than tt_e
closing, concurrent writers, cross-store transactions, an internal
optimizer/IR, automatic GC, adaptive I/O selection.

**Crash-safety objective.** The JSONL event log is the write-ahead source
of truth; `tgms replay` rebuilds byte-identical stores (D-023). The native
store is a deterministic materialization. The objective is therefore:

> Never expose an undetected inconsistent generation. On any doubt, roll
> back to the last valid generation and replay the event-log suffix.

Cheaper than ARIES-class recovery — but it requires checksums,
complete-markers, and generation discipline from format v0.

---

## 2. Workload profile

- Edge versions dominate (CollegeMsg 60k, email-Eu 332k, synth 1e6–1e7
  committed, 1e8 stretch; |V| ≤ millions). Node versions ≈ |V|, small.
  DuckDB 1e7 store today: 2.6 GB (~260 B/row).
- Writes: append-only batches at strictly monotone `tt`; the only mutation
  is closing `tt_e` on a small set (carves, retract/correct). Bulk event
  ingest is roughly vt-ordered, instantaneous (`vt_e = vt_s+1`),
  `disc = "#batchoffset"`, props mostly `{}` or one small key.
- Reads: >95% of scans at `as_of_tt = OPEN_END` with a vt window;
  historical reads are the differentiator but rare. Column projection
  matters.
- Ordering: scans return (vt_s, vid)-sorted rows; digest sorts by
  (identity, tt_s, vt_s, vid). Determinism is contractual.
- Derivability: `eid = sha256(canon[src,dst,rel_type,disc])[:24]`,
  `vid = sha256(f"{identity}:{tt_s}:{vt_s}")[:24]` — pure functions of
  other fields.

**Size expectation (corrected, honest):** base event row ≈ vt_s delta +
src/dst u32 + rel code + props ref + vid64 (8 incompressible bytes) +
block/visibility overhead ⇒ **14–24 B/row compressed; ~1.4–2.4 GB @1e8
before indexes** — ~10–18× smaller than DuckDB today. Recorded
measurement-driven future option: after full compaction into a single
globally ordered run, vid64 can move to a droppable merge sidecar.

---

## 3. Architecture overview

```
 PYTHON (unchanged surface)
   ops_* operators · agent layer · eval          StorageAdapter ABC
   base.py semantics (apply_ops, carving)        CLI · dataset cards
   EventLog (JSONL WAL, source of truth)         DuckDB reference adapter
   oracle / replay / differential tests          NativeAdapter (thin)
 ────────────────────────────┬───────────────────────────────────────────
                     PyO3 boundary (coarse: open/snapshot/commit/scan/
                     lookup/compact — one crossing per batch, GIL released)
 ────────────────────────────┴───────────────────────────────────────────
 RUST  tgms-engine-core (no Python dependency)
   C6 kernels ◄── chunked scan cursor (never the ABC)
   C3 indexes: postings · zone maps · prefix-max staircase · TCSR-perm · name
   C2 visibility: generation-scoped close runs → segment sidecars
   C1 layout: partitions ▸ byte-sized segments ▸ runs ▸ blocks (two lanes)
   C4 compression (format v2, gated; codec IDs reserved from v0)
   C5 I/O + persistence: reader policies · compaction · conservative GC
   C0 substrate: dictionary · generations · commit protocol   ← the spine
```

---

## 4. Components

### C0 — Identity & generation substrate (the spine; build first)

**Store generations.** Every logical commit produces an immutable
generation described by a manifest:

```
MANIFEST.G
├── parent_generation, format_version, column widths, checksums
├── event_log_commit_offset (+ incremental/checkpointed prefix hash)
├── dictionary_visible_length
├── edge/node segment list (lane, full low/high keys, zone maps)
├── close-patch run list
├── postings / name-index / TCSR file refs + visible extents
└── statistics snapshot
```

**Group-commit durability (round 2 §2.7).** One durability mode. A logical
commit = one or more op batches → **one** event-log fsync → seal
materialization files → publish one manifest. Interactive use: one batch
per generation; bulk ingest: many batches per generation. Invariants: a
published generation always references a durable log prefix; the manifest
offset points immediately after a complete newline-terminated JSONL
record; the prefix hash is chained/checkpointed so opening a store never
re-hashes the full log.

**Commit protocol:** append batches → fsync log → write segment/close-run/
index files → fsync files → write `MANIFEST.G.tmp` → fsync → atomic
rename → fsync directory. Crash at any step leaves the previous manifest
valid; orphans are ignored. Readers hold a **snapshot handle** pinning one
generation.

**Dense-ID dictionary** — first-class, with invariants: append-only
(uid, label) file, dense id = ordinal, manifest records visible length;
**u32 ids with an explicit capacity check** (>4B entities is out of scope;
widths declared in the format header, not adaptive); replay-stable
assignment (deterministic op order — tested); never reused or reordered;
uid→id via hash64 probe + full-string verify; casefolding versioned.

**Canonical derivation lives in the engine core (round 2 §1.4).** One Rust
implementation serves row ordering, collision verification, digest
construction, and public ID materialization. The Python semantics layer
still computes ids in `apply_ops` (unchanged); at commit the engine
re-derives and **asserts equality per row** (input-construction drift is
the real risk, not SHA-256 itself; assertion cost is measured at E2 and
may drop to per-block spot checks).

**Node store is identity-clustered**, not vt-partitioned: nodes are few
and identity-addressed; `nodes_columnar` scans the small node store with
vt filtering.

### C1 — Layout: partitions ▸ segments ▸ runs ▸ blocks, two lanes

```
Logical partition   contiguous valid-time range (dataset-card driven)
Physical segment    disjoint composite-key range; target 64–256 MB
Run                 one immutable flush within a partition
Block               codec/pruning unit, 16–64k rows; the cache tile
```

Physical boundaries on `(vt_s, vid64)` with the **tie-group rule (round 2
§1.4): a group of rows equal on the composite key is never split across a
segment boundary**, and manifests record full 96-bit derived low/high
boundary keys — so ordering never rests on the 64-bit prefix alone.

**Two lanes, routed by partition crossings (round 2 §2.2):** a row whose
interval intersects **≤ K = 2 adjacent partitions** goes to the event lane
(vt_e delta-coded or elided when uniformly `vt_s+1`); more than K, the
interval lane (explicit vt_e, prefix-max staircase). **Lane assignment is
physical, not logical identity** — compaction or partition-map changes may
reroute rows; digests are unaffected. Event-lane zone maps stay tight by
construction regardless of partition-width policy. Interval lane expected
tiny; per-partition carry-over directories remain the recorded upgrade
path.

**Physical edge schema.** Hot: `vt_s i64`, `src_id u32`, `dst_id u32`,
`rel_code u8/u16` → **16-byte hot row** (round 2 §2.6). Warm: `vid64 u64`,
`vt_e` (lane-dependent), `props_ref u32` (0 = empty), `disc_ref`, `tt_s`
(RLE, per-run constant), `source/provenance_ref` (dict refs). `tt_e` is
not a column (C2). Segment-local row ids u32; global offsets u64.

**Derivability invariant (unchanged from v2):** full eid/vid derived,
never stored; disc always recoverable (dictionary-coded, or elided only
under a header-declared reversible `"#" + offset` encoding); 64-bit
prefixes are accelerators; ties compare fully derived ids. Stored 96-bit
IDs remain the recorded format fallback.

**Cache-locality rationale:** the workhorse scan streams 16 B/row
block-tiles that live in L2 during fused filtering; 1M-row window ≈ 16 MB
⇒ bandwidth-bound. All variable-width data sits out-of-line behind u32
refs; hot columns stay fixed-width and SIMD-friendly.

### C2 — Visibility: generation-scoped close runs → sidecars

- Each commit writes an immutable **close-patch run** `(segment, row,
  tt_e)` (usually empty/tiny); manifests list visible runs — old readers
  never see newer closes.
- Compaction folds runs into per-segment sidecars. **First version
  implements only** `all_current` (header flag; zero visibility work) and
  **sparse** (bitmap + sorted (row, close_tt)); the dense-`tt_e` sidecar
  tag is reserved in the format but unimplemented until a real workload
  crosses a measured threshold (round 2 §1.2).
- Historical read (`as_of_tt = a`): `tt_s ≤ a` via RLE/zone maps;
  closed ⇒ `close_tt > a` via sidecar arrays.
- **Compaction never deletes closed rows** — it changes representation,
  not content; its unit test is digest + historical-sample equivalence.
- The current/historical split (AeonG economics) falls out of the sidecar;
  D-009's deferred cache is subsumed.

### C3 — Indexes (generation-scoped, rebuildable, never authoritative)

1. **Identity postings**: `identity64 → [(segment, row), …]`, append-only
   runs + manifest extents, full-id verify on hit. *(E4 — E3 uses linear
   fallbacks, see roadmap.)*
2. **Zone maps** (manifest): per segment min/max vt_s/vt_e/tt_s, rel-code
   bitset, n_rows, n_closed, lane, full boundary keys. All pruning happens
   before I/O; `partitions_pruned` feeds the existing trace slot.
3. **Interval staircase** (interval lane), precise definition (round 2
   §2.3): `block_prefix_max_vt_e[i] = max(vt_e over blocks 0..i)` — a
   **monotone prefix maximum**, binary-searchable for the first block with
   prefix-max > w_min; per-block independent maxima are not monotone and
   are wrong for this purpose.
4. **TCSR, adaptive-sparse (round 2 §2.1)**: per segment and direction,
   `active_vertex_ids[m] + offsets[m+1] + row_perm[n]`; dense
   `offsets[|V|+1]` only when active-vertex density ≥ threshold. Lookup =
   binary search / segment-local hash on active ids. Traversal gathers
   base columns through `row_perm`; multi-segment traversal iterates
   per-vertex over segment slices (offsets never concatenate). Window-CSR
   materialization stays a measured option.
5. **Name index**: current-canonical names only; sorted (casefolded name,
   dense_id) + binary search. Trigram substring postings deferred past the
   first usable engine.

### C4 — Compression (format v2, gated; codec IDs reserved from v0)

Adoption order once uncompressed baselines exist (byte-aligned,
decode-speed-first, decode into L2 tiles): vt_s delta+FoR bit-packing
(FastPFoR/streamvbyte; Gorilla d-o-d alternative) → src/dst bit-pack →
rel RLE → per-segment props payload dictionary → optional zstd for cold
props blobs only. Gate: ≤10% scan regression per codec, else rejected.
Size expectation per §2 (14–24 B/row — the honest number).

### C5 — I/O, persistence, compaction, GC

- **Segment I/O is a swappable policy**: mmap / buffered / in-memory
  readers behind one Rust trait; benchmarked on local NVMe and cluster
  project storage, cold and warm. Rust ownership makes the mapped-lifetime
  rules enforceable (a Python-visible buffer keeps its snapshot owner
  alive; no use-after-unmap by construction).
- **Format hygiene from v0**: header+footer checksums, per-block
  checksums, endianness declaration, codec IDs, declared column widths,
  generation + schema version, complete-marker. Corruption is detected
  before results; recovery = last valid generation + suffix replay.
- **Compaction** (early, E3): merge runs, fold close runs, rebuild
  postings/TCSR extents; explicit CLI; equivalence test = digest +
  historical-query sample byte-identical.
- **GC is conservative and explicit (round 2 §2.4): the first version
  performs no physical deletion.** Compaction publishes new generations
  and leaves old files. Later: `tgms store gc` removes unreachable
  generations only when no reader marker (pid, generation, ctime) is
  live; in-process handles are refcounted. No lease protocol.

### C6 — Kernels over the chunked cursor; the language boundary

**Internal interface** (Rust): `scan(ScanRequest) → iterator[ColumnarBatch]`
— request carries lanes, key range, as_of_tt, vt window, rel codes,
incidence set, projection, `needs_global_order`; batches carry zero-copy
column views, selection bitmap, key range, (segment, row) ids, visibility
already applied. The cursor owns the shared mechanics (pruning, block
decode, belief/vt/rel filters, projection, row tracking); each kernel owns
only its access pattern. Fusion is hand-written; no optimizer.

Kernels (Rust-resident, incremental — start with the measured hotspot):
1. **Windowed scan + run merge** — k-way on (vt_s, vid64), full-id compare
   on prefix ties; merge only when `needs_global_order`.
2. **δ-motif join** — searchsorted candidates over δ-bounded slices,
   radix/hash-grouped on dense ids; same E_COST guardrails.
3. **Interval overlap join** (`co_active`) — plane sweep; Allen variants
   as predicates.
4. **Time-bucket group-aggregate** — segmented count/sum/min/max/distinct;
   substrate for the future aggregation operators (+30-question coverage).
5. **Traversal** — TCSR-perm slices feeding existing O4/O5 relaxation.

**PyO3 boundary (round 2 §7–9):** coarse calls only —
`NativeStore.open/snapshot/commit(batch_descriptor)/compact`,
`Snapshot.scan(request)/lookup_identities(ids)`. One crossing per result
batch, never per row; GIL released during Rust work. Materialization to
Python = NumPy arrays via the buffer protocol backed by an owner object
holding (snapshot handle, mapping, extent) — Arrow stays optional interop.
Version-specific wheels first; **no abi3 until the API stabilizes**.

**Gates are output-sensitive and staged**: raw predicate throughput ≥ 1e8
rows/s/core; end-to-end `edges_columnar` (1M-row current-belief window,
SoA materialized) ≤ 10 ms; ABC conversion accounted separately; join gates
name complete-result materialization.

### C7 — Adapter integration, parity, fault injection

`tgms/storage/native/adapter.py` (thin) implements the ABC over the PyO3
API; `store.py` gains `backend="native"`. DuckDB adapter kept untouched as
the reference: the A/B harness applies every event-log batch to both and
asserts digest equality. It answers the only baseline question that
matters for this phase: *does the native engine improve TGMS?* After the
switch it becomes an optional extra (b6 keeps an eval-only pin).

**Fault-injection matrix**: truncated segment, corrupt footer, partial
close run, log ahead of manifest, manifest ahead of segments, crash
before/after rename, interrupted compaction, old reader across commit.
Pass: previous generation served intact, or detected + suffix-replayed —
never a silent mixed state. Rust core additionally gets normal `cargo
test` units + fuzzed file parsers (no-Python crate makes both natural).

### C8 — Observability & benchmark discipline

Scan counters (`segments_pruned / runs_merged / blocks_touched /
rows_decoded / bytes_touched`) flow into the existing trace slot. `tgms
bench ops --backend {duckdb,native}` on identical replayed stores. Layout
constants (partition policy, segment bytes, R, block size, density
thresholds, K) are measured, recorded in manifest/dataset card, never
hard-coded. Synthetic generator gains correction-rate / correction-age /
interval-fraction / selectivity axes as a tuning instrument.

---

## 5. Repository & packaging shape

```
TGMS/
├── tgms/                    # existing Python package, layout unchanged
│   └── storage/native/adapter.py
├── crates/
│   ├── tgms-engine-core/    # pure Rust: manifest/segment/visibility/
│   │                        #   index/scan/kernels — no Python dependency
│   └── tgms-engine-py/      # PyO3 bindings only
├── pyproject.toml           # maturin mixed build → single `tgms` wheel
└── Cargo.toml               # workspace
```

Single mixed wheel via maturin + cibuildwheel in the existing
trusted-publishing workflow (mac/linux × supported CPythons). sdist builds
need a Rust toolchain — acceptable because wheels cover the supported
matrix; **the E0 packaging probe exists precisely to verify a plain
`pip install tgms` user never sees Rust.** `tgms-engine-core` having no
Python dependency enables cargo unit tests, parser fuzzing, and a future
standalone inspect/repair CLI.

---

## 6. Roadmap (language-adjusted, round 2 §12; supersedes v2 staging)

| Stage | Builds | Exit gate |
|---|---|---|
| **E0 — Freeze + skeleton + packaging probe** | semantics/decisions recorded (D-028); mixed Python/Rust package skeleton; CI wheels build on linux/mac; module imports and returns one test NumPy array through the full boundary | wheel installs clean on both platforms with no toolchain; operator projection/selectivity trace on real stores recorded |
| **E1 — C0 in Rust** | manifest, generations, group-commit, dictionary + invariants, checksums, snapshot handles, suffix-replay hook; thin PyO3 wrapper | crash-step unit tests (fault-matrix subset); replay-stable dense ids proven; derivation parity assertion live |
| **E2 — Segments + scan + performance probe** | uncompressed event-lane segments, blocks, chunked cursor, multi-run scan, buffered+mmap readers; NumPy prototypes serve as benchmark oracles; probe fixes which kernels go native first | round-trip digest identity; scan gates met on 1e6; probe report ranks kernel porting order |
| **E3 — Semantic completeness** | close runs, sparse sidecars, historical visibility, interval-lane slow path, minimal compaction, **linear-scan fallbacks** for believed_*/probes/props | full ABC green on `make test-full` (native); vaulted CollegeMsg replay reproduces D-018/D-023 SHAs; compaction equivalence; **gate = semantics, not speed** |
| **E4 — Indexed performance** | postings, staircase, adaptive TCSR, name index; kernels incrementally (measured hotspot first: co_active, then motifs) | ops_* DuckDB-free; oracle suites green; co_active ≤ 100 ms @1M; `tgms bench` ≥ M3 floor everywhere; **gate = indexed performance** |
| **E5 — Soak + switch** | A/B dual-apply harness, full fault matrix, recovery tooling, diagnostics, docs; native = default backend; wheels shipped | A/B digest equality on all canonical stores + synth 1e6/1e7; fault matrix green |
| **E6+ — Measured luxuries** | compression, dense sidecars, kernel parallelism, window-CSR, trigram names, GC reclamation, richer interval indexing | each item individually justified by measurement; none scheduled |

Usability workstream (PHASE3_SPEC §7) interleaves; independent of engine.

---

## 7. Borrowed-solutions summary

| Problem | Borrowed from | What we take |
|---|---|---|
| Generation commits & snapshots | Iceberg/Delta, LMDB | immutable manifests, atomic swap, reader handles |
| Group commit | classic WAL practice | one fsync per logical commit |
| Columnar time layout | kdb+ splayed tables, MonetDB | SoA columns, out-of-line variable data |
| Segment pruning | Parquet/ClickHouse | manifest zone maps, block marks |
| Interval search | SAP HANA Timeline Index | prefix-max staircase (simplified) |
| Current/historical economics | AeonG | visibility sidecar, not dual tables |
| Mutation-as-data | LSM tombstones, PG visibility maps | close runs → sidecars → compaction folds |
| Integer compression | FastPFoR/streamvbyte, Gorilla | delta+FoR, decode-in-tiles (gated) |
| Join kernels | radix-join literature, Paranjape'17 | dense-id grouping over δ-bounded slices |
| Interval join | classic plane sweep | co_active kernel |
| Vectorized execution | DuckDB/Velox concepts | chunked cursor, selection vectors, late materialization |
| Safe zero-copy + lifetimes | Rust ownership, PyO3 buffer protocol | snapshot-owned buffers, no use-after-unmap |
| Crash model | our D-023 | detect-or-rollback + suffix replay |

---

## 8. Decisions locked at E0 (for D-028; resolved items dated)

1. All logical state is manifest-generation scoped; readers hold snapshot
   handles; close patches are immutable commit artifacts.
2. **Identity is derived under the derivability invariant**; 64-bit
   prefixes are accelerators; ties compare full derived ids; stored 96-bit
   IDs are the recorded fallback. *(2026-07-28)*
3. One canonical derivation implementation in the engine core; ingest-time
   parity assertion against the Python semantics layer.
4. Composite-key `(vt_s, vid64)` tie groups never split across segment
   boundaries; manifests record full 96-bit boundary keys.
5. Partitions ≠ segments; segments byte-targeted; blocks are the codec/
   pruning unit.
6. Lane membership = intersects ≤ K(=2) adjacent partitions; lane
   assignment is physical, not logical identity.
7. Visibility v0 = `all_current` + sparse sidecars; dense tag reserved,
   unimplemented.
8. Compaction preserves closed rows; equivalence test = digest +
   historical sample.
9. **No physical GC in the first version**; later GC is explicit
   (`tgms store gc`) and marker-guarded.
10. Group-commit durability, one mode; manifest offset lands on a record
    boundary; chained/checkpointed log prefix hash.
11. u32 entity ids and row ids with capacity checks; widths declared in
    the format header, fixed (not adaptive) in v0.
12. The internal API is the chunked cursor; the ABC materializes only at
    the public boundary; the PyO3 boundary is coarse (per batch, GIL
    released).
13. **Rust-first persistent core** (tgms-engine-core, no Python
    dependency) with thin PyO3/maturin layer; Python keeps semantics, CLI,
    adapters, tests; NumPy prototypes are never authoritative.
    *(2026-07-28, supersedes v2's Python-first staging)*
14. Version-specific wheels first; no abi3 until the API stabilizes.
15. Node store is identity-clustered; name lookup is current-canonical
    only.
16. Format v0 carries checksums, codec IDs, widths, versions,
    complete-markers.
17. Gates: E3 = semantic completeness (linear fallbacks legal); E4 =
    indexed performance; scan/join gates are output-sensitive.

---

## 9. Remaining open questions (none blocking E0)

1. Partition policy default (weekly vs row-mass-balanced) — decided with
   E2 numbers; K = 2 rule makes the choice non-semantic.
2. Derivation parity assertion granularity (per row vs per block) —
   measure at E2.
3. Kernel porting order after co_active — E2 probe decides.
4. props typed-column promotion — format slot reserved; build with the
   aggregation operators, not before.

## 10. Research hooks (recorded, not designed for)

Belief-aware bi-temporal segment storage; duration-aware lanes;
compressed-native temporal kernels; verifiable derived-state engines.
None changes what we build. The practical justification stands on its own
(round 2 §13): TGMS has unusually constrained, well-defined storage
semantics; a small native engine makes them faster, easier to deploy,
easier to verify, and independent of a general SQL engine — without
turning TGMS into a general database system.
