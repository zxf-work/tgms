# TGMS Native Engine — Implementation Specification (hand-off to coding agent)

**Version 1.0 — 2026-07-28. Status: awaiting PI sign-off (D-028/D-029).**
**Intended reader:** an autonomous coding agent implementing the native
storage engine. Architecture and rationale live in `ENGINE_BLUEPRINT.md`
(v3); this document is the *binding implementation contract*. Where the two
disagree on an implementation detail, this document wins. Where a design
decision was made, it is recorded here or in the blueprint §8 so the
implementer does not re-litigate it.

Required reading, in order: this file → `ENGINE_BLUEPRINT.md` →
`tgms/storage/base.py` (the ABC + semantics you must serve) →
`tgms/storage/duckdb_adapter.py` (the reference implementation) →
`benchmarks/frozen-v1/README.md` (replay reproduction procedure).

---

## 0. Mission and definition of done

Implement `backend="native"`: a Rust storage core (`crates/tgms-engine-core`)
with a thin PyO3 layer (`crates/tgms-engine-py`) and a thin Python adapter
(`tgms/storage/native/adapter.py`) implementing the existing
`StorageAdapter` ABC — such that:

1. `make test-full` passes with the native backend (500-case oracle,
   metamorphic, invariants, replay suites — none of which you may edit;
   see §1).
2. `tgms replay` of `benchmarks/frozen-v1/collegemsg.eventlog.jsonl` into a
   native store yields **exactly** the same `store_digest()` as the DuckDB
   backend (the D-023 procedure).
3. An A/B harness applying every event-log batch to both backends asserts
   digest equality after every batch on all canonical stores + synthetic
   1e6/1e7.
4. `tgms bench ops --backend native` meets every gate in §10.
5. `pip install tgms` (wheel) works on macOS/Linux with no Rust toolchain.

TGMS must function fully with DuckDB absent from the environment.

---

## 1. Process rules (binding; violations fail CI or review)

1. **Test ownership (spec §8.1).** `tests/` and `tgms/temporal/oracle.py`
   are human-owned. Never modify them in the same commit as implementation
   code. `scripts/check_commit_hygiene.py` (run by `make ci`) enforces
   this. If a test must change (e.g., adding a backend parameter to
   fixtures — WP-N3 needs this), open a **separate commit labeled
   `[tests]`** with written justification and **pause that work package
   until a human approves**.
2. **DECISIONS discipline.** Before the first engine commit, add to
   `docs/DECISIONS.md`: **D-028** (engine architecture = `ENGINE_BLUEPRINT.md`
   §8 decision list, copied verbatim, dated) and **D-029** (new
   dependencies + licenses, listed in §12 here). New dependencies beyond
   §12 require their own DECISIONS entry before use.
3. **Frozen artifacts are sacred.** Never regenerate or re-ingest anything
   under `benchmarks/frozen-v1/`. Frozen-split reruns need
   `TGMS_FORCE="reason"`. The canonical CollegeMsg store is rebuilt ONLY
   via `tgms replay` (never `tgms ingest`).
4. **Determinism receipts.** Every benchmark table you produce embeds: git
   SHA, store digest, config, machine, and (for Rust) `rustc --version`.
5. **No silent semantics changes.** The ABC signatures, operator
   semantics, digest definition, and event-log format are fixed. Anything
   that would change a digest is a bug in your code, never a proposal.
6. **CI budget.** `make ci` must stay under 15 minutes. Cache cargo builds
   in CI; Rust unit tests run in `cargo test` (fast profile) within that
   budget.
7. **Commits.** Small, single-purpose, present-tense messages, as in the
   existing history. Do not commit generated stores, wheels, or target/.

---

## 2. Fixed semantic ground truth (reproduce exactly; never reinterpret)

### 2.1 Constants and types

| Name | Value |
|---|---|
| `OPEN_END` | `2**62` = 4611686018427387904 |
| Timestamps | int64 epoch **microseconds**, UTC; intervals half-open `[s, e)`, valid iff `s < e` |
| `clamp_tt(a)` | `min(a, OPEN_END - 1)`; belief predicate is `tt_s <= clamp_tt(a) < tt_e` |
| Entity ids | dense `u32` in the engine (capacity-check > 4.29e9 → hard error); presented to Python as int64 (the ABC contract: `dense_ids` returns `np.int64`) |
| Endianness | little-endian throughout on-disk |

### 2.2 Identity derivation (one Rust implementation; §6.4)

```
sha256_hex(text)  = lowercase hex of SHA-256 of UTF-8 bytes
eid  = sha256_hex(canonical_json([src, dst, rel_type, disc]))[:24]   # 24 hex chars = 96 bits
vid  = sha256_hex(identity + ":" + str(tt_s) + ":" + str(vt_s))[:24] # identity = uid (nodes) | eid (edges)
```

`canonical_json` = Python `json.dumps(obj, sort_keys=True,
separators=(",", ":"), ensure_ascii=False)`. The engine needs it **only
for arrays of strings** (the eid input). Rust must byte-match Python for
that domain: `["a","b",""]` style output; escapes limited to `"` `\`
and control chars (`\n` `\t` … as `\uXXXX` for <0x20 except the standard
short escapes); non-ASCII passes through as UTF-8. **Ship test vectors**
(≥ 30 cases incl. unicode, quotes, backslashes, empty strings) asserted
against Python in the parity tests.

Prefix keys: `vid64` / `eid64` / `identity64` = first 16 hex chars parsed
as big-endian u64. Full-96-bit compare = u64 prefix, then remaining 8 hex
chars as u32. Hex-string lexicographic order ≡ this numeric order — that
equivalence is what makes prefix sorting legal.

### 2.3 Digest (stays in Python — do not reimplement)

`StorageAdapter.store_digest()` in `base.py` computes the digest from
`all_node_versions()` / `all_edge_versions()`. The native adapter
implements those two iterators (full materialization, offline path);
Python does the sorting and hashing. **Consequence:** the engine must
return props as the exact canonical-JSON string it was given at ingest
(store the bytes; never re-serialize).

### 2.4 Write-batch protocol (from `store.py::_write` — fixed)

One public mutation = one batch = one event-log record = one engine
generation:

```
tt = clock.tick()                    # strictly monotone
eventlog.append(tt, ops)             # WRITE-AHEAD (Python, unchanged)
adapter.begin()                      # open engine staging
adapter.apply_ops(ops, tt)           # base.py semantics — calls the
                                     #   primitives below, interleaved
adapter.commit()  /  adapter.rollback()  on TgmsError
```

During `apply_ops`, base.py calls (possibly repeatedly, interleaved):
`ensure_entities`, `believed_node_versions`, `believed_edge_versions`,
`nodes_with_believed_versions`, `insert_node_versions`,
`insert_edge_versions`, `close_node_versions`, `close_edge_versions`.

**Read-your-own-writes is mandatory:** reads inside an open batch must see
rows staged earlier in the same batch (e.g., two asserts on one identity
in one batch; carve inserts fragments then closes originals). Closes may
target committed rows (→ close-run entry) **or** staged rows (→ set tt_e
in staging directly). A rolled-back batch leaves zero trace in the store
(the event log keeps the record; replay re-fails it identically — D-004).

Bulk ingest arrives as `ingest_events` chunks of ≤ 50,000 events
(`store.py::INGEST_CHUNK`); each chunk is its own batch/generation.
Generation-per-batch is acceptable; compaction merges runs (§5.6).

---

## 3. Repository layout and packaging

```
TGMS/
├── tgms/                          # existing package — layout unchanged
│   └── storage/native/
│       ├── __init__.py
│       └── adapter.py             # NativeAdapter(StorageAdapter) — thin
├── crates/
│   ├── tgms-engine-core/          # pure Rust, NO pyo3 dependency
│   │   └── src/{manifest,dict,segment,visibility,index,scan,kernel,derive}/
│   └── tgms-engine-py/            # pyo3 bindings only ("tgms._engine" module)
├── Cargo.toml                     # workspace
└── pyproject.toml                 # maturin mixed build → single tgms wheel
```

- Build backend switches to **maturin** (mixed Rust/Python project,
  `python-source` pointing at the repo root package). One wheel named
  `tgms` containing the compiled `tgms._engine` extension.
- Wheels: CPython 3.11/3.12/3.13 × {manylinux_x86_64, macosx_arm64} via
  cibuildwheel in the existing trusted-publishing workflow. **No abi3.**
- `tgms/storage/native/adapter.py` imports `tgms._engine`; a missing
  extension raises a clear error naming the wheel requirement (source
  installs need a Rust toolchain — documented, acceptable).
- `store.py::_make_adapter` gains `backend="native"` →
  `NativeAdapter(path / "native")`. DuckDB remains the default backend
  until WP-N5 flips it; DuckDB then moves to an optional extra
  (`tgms[duckdb]`), imported lazily only when requested.
- `tgms-engine-core` compiles and tests with `cargo test` with no Python
  present. Rust toolchain: stable, MSRV = the version in CI, recorded in
  `Cargo.toml`.

---

## 4. On-disk formats (normative)

Store directory (created by `NativeStore.open`, lives at
`<store_path>/native/` beside `eventlog.jsonl`):

```
native/
├── CURRENT                        # 1 line: "<generation> <manifest_sha256_16>\n"; written tmp+rename
├── manifests/<G>.json             # immutable manifest, G = zero-padded u64
├── dict.log                       # append-only dictionary
├── seg/<seg_id>.tgs               # immutable segment files (u64 ids, never reused)
├── close/<G>.tgc                  # close-run for generation G (absent if empty)
└── idx/…                          # postings / name / tcsr files (WP-N4; manifest-referenced)
```

**Never delete or rewrite any file in place** (except `CURRENT` via
atomic rename). No GC in this project phase (blueprint §8.9): compaction
adds files and publishes a new manifest; old files remain.

### 4.1 MANIFEST.<G>.json (UTF-8 JSON, inspectable by design)

Required fields:

```jsonc
{
  "format": 1, "generation": G, "parent": G-1,          // 0 has parent null
  "created_tt": <i64>,                                   // batch tt (or last tt of group)
  "event_log": {"offset": <u64 bytes>, "chain": "<sha256_16>"},
      // offset points just PAST the newline of the last applied JSONL record.
      // chain = sha256(prev_chain || record_bytes) truncated 16 hex; chain of
      // generation 0 = sha256("") — verifiable without rehashing whole log.
  "dict": {"records": <u64>, "bytes": <u64>},
  "widths": {"entity_id": 32, "row_id": 32, "rel_code": 16},
  "node_store": { … same shape as edge lanes … },
  "edge_lanes": {
    "event":    [SegmentEntry, …],
    "interval": [SegmentEntry, …]
  },
  "close_runs": [{"file": "close/<G>.tgc", "entries": <u32>, "sha": "…"}, …],
  "indexes": { … WP-N4: file + visible-extent per index … },
  "stats": { "n_node_versions": …, "n_edge_versions": …, "vt_min": …,
             "vt_max": …, "rel_type_counts": {…}, "max_out_degree": … },
  "manifest_sha": "<sha256 of this file with this field blanked>"
}

SegmentEntry = {"file": "seg/<id>.tgs", "rows": <u32>, "runs_in_partition": …,
  "key_lo": ["<vt_s>", "<vid 24-hex>"], "key_hi": [ … ],   // FULL 96-bit keys
  "vt_min": …, "vt_max": …, "vt_e_max": …, "tt_s_min": …, "tt_s_max": …,
  "rel_codes": [ … ], "n_closed_folded": <u32>, "all_current": bool,
  "sha": "<segment footer sha>"}
```

`stats()` (ABC) is served from the manifest stats block — maintained
incrementally at commit, never by scanning.

### 4.2 dict.log (append-only)

Record: `u32 uid_len | uid utf-8 | u32 label_len | label utf-8`. Dense id
= record ordinal (u32). The manifest's `dict.records/bytes` bound
visibility; readers never read past `bytes`. Replay-stability: ids are
assigned in first-registration order of `ensure_entities` calls — this is
deterministic given the event log (test WP-N1-T5).

### 4.3 Segment file `.tgs`

```
[magic "TGSG" | u32 format=1]
[u32 header_len | header JSON]      // schema below
[column extents, each 64-byte aligned]
[blob region]                        // props + disc payload dictionaries
[footer: u32 per-extent crc32c[] | file sha256_16 | u64 row_count
 | magic "TGSE"]                     // footer presence = complete-marker
```

Header JSON: `lane`, `rows`, `key_lo/key_hi` (full), zone-map fields (as
in SegmentEntry), `block_rows` (default 32768), per-column table
`{name, dtype, codec_id (0 = raw), offset, bytes}`, flags:
`vt_e_elided` (event lane, uniform vt_s+1), `tt_s_rle`
(list of `(run_start_row, tt_s)` pairs), `disc_encoding`
(`"dict"` | `"derived_offset:<base>"` — the derivability invariant:
`derived_offset` is legal **only** when every row's disc equals
`"#" + str(base + row_ordinal_in_batch)`; the writer verifies before
electing it).

Edge columns (event lane): `vt_s i64`, `src_id u32`, `dst_id u32`,
`rel_code u16`, `vid64 u64`, `vid_lo32 u32`, `props_ref u32` (0 = `{}`),
`disc_ref u32` (absent if derived), `source_ref u8`, `prov_ref u32`
(0 = null). Interval lane adds `vt_e i64` and the prefix-max staircase
(u64 per block, **monotone prefix maximum of vt_e over blocks 0..i**).
Node segments: `uid_id u32`, `vt_s`, `vt_e`, `vid64`, `vid_lo32`,
`label_ref`, `props_ref`, `source_ref`, `prov_ref`, `tt_s_rle`.

`vid_lo32` (remaining 8 hex chars of vid) is stored so full-96-bit
ordering/verification never requires re-derivation in the hot path; full
*hex strings* are still derived only at the API boundary. eid is fully
derived (from src/dst/rel/disc via §2.2) — never stored.

Rel-code table and `source` table live in the header (tiny dictionaries).
Rows within a segment are sorted by `(vt_s, vid64, vid_lo32)`; a
composite-key tie group never spans a segment boundary.

### 4.4 Close-run file `.tgc`

`[magic "TGCR" | u32 format | u32 n]` then n records of
`u8 kind (0=node,1=edge) | u64 seg_id | u32 row | i64 tt_e`, then
`crc32c | magic`. Rows are addressed by their **committed** location;
compaction that moves rows rewrites affected close info into the folded
sidecar within the new segment (header field `closed_rows`:
sorted `(row u32, tt_e i64)` pairs + bitmap) and drops the run from the
new manifest's `close_runs` list.

### 4.5 Defaults (constants; header-recorded, tunable only via measurement)

| Constant | Default |
|---|---|
| block_rows | 32,768 |
| segment target size | 128 MiB uncompressed |
| runs-per-partition compaction trigger R | 4 |
| lane rule K (max partition crossings for event lane) | 2 |
| partition policy v0 | fixed width from dataset card (default 7 days); revisit at WP-N2 with measurements |
| TCSR dense-offsets threshold | active_vertices / |V| ≥ 0.5 |
| checksums | crc32c per column extent; sha256_16 per file |

---

## 5. Engine behavior specification

### 5.1 Snapshots and visibility
`Snapshot` = one manifest generation: segment list + visible close runs +
dict length. Row visible at `as_of_tt = a` iff `tt_s ≤ clamp_tt(a)` and
(not closed, or `close_tt > clamp_tt(a)`) — closes come from folded
sidecars + visible close runs. `all_current` segments skip all visibility
work at `a = OPEN_END`. `WorkingView` = Snapshot ⊕ staging overlay
(read-your-own-writes, §2.4); only the writer ever holds one.

### 5.2 Staging (begin → commit/rollback)
Staging buffers are in-memory columnar builders (nodes, edges, closes)
plus a dict-append list. `commit(tt)`: sort staged rows by
`(vt_s, vid64, vid_lo32)`, route rows to lanes (K-rule against the
current partition map), split into segments at the size target (never
inside a tie group), write files per §4.3/§4.4, fsync files, append dict
records + fsync, write manifest + fsync, rename CURRENT + fsync dir.
`rollback()`: drop buffers (dict entries staged but unpublished are
simply not covered by the manifest; the file is truncated to the
manifest's byte count on next open if a torn append is detected).
**Derivation parity assertion:** at commit, re-derive each staged row's
eid/vid via §2.2 and assert equality with the Python-provided strings
(hard error = bug). Start per-row; may drop to per-block sampling only if
WP-N2 measurement shows > 5% ingest overhead.

### 5.3 Scans
`ScanRequest {kind: node|edge, lanes, as_of_tt, vt_min?, vt_max?,
rel_codes?, touching_ids?, columns, needs_global_order, limit?}` →
iterator of `ColumnarBatch {columns (NumPy via buffer protocol,
zero-copy where layout permits), selection mask applied, sorted}`.
Pruning order: manifest zone maps → per-segment binary search on vt_s →
staircase (interval lane) → block decode → fused predicate. k-way merge
across runs only when `needs_global_order`. The public
`edges_columnar`/`nodes_columnar` materialize the merged SoA dict exactly
as `duckdb_adapter.py` does today (same keys, same dtypes, same
`(vt_s, vid)` order; string columns built only when projected).

### 5.4 Point reads
`believed_versions(kind, identity_str)`: derive identity64, consult
postings (WP-N4) or linear scan fallback (WP-N3), verify candidates by
full derived id, apply belief filter, return rows sorted by vt_s.
`nodes_with_believed_versions`: batched form of the same.

### 5.5 Iterators for digest
`all_versions(kind)`: stream every row (all generations' rows are in the
newest manifest's segments — closed rows included with their folded/run
tt_e applied; a row's tt_e = its close tt if closed, else OPEN_END).
Order irrelevant (base.py sorts).

### 5.6 Compaction (`tgms store compact`, explicit CLI)
Triggers (advisory; command always runs to completion): runs-per-partition
> R, or unfolded close entries > 20% of a segment's rows. Actions: merge
runs partition-wise, fold close runs into sidecars, re-split at size
targets, rebuild index extents, publish new manifest. **Never drops a
row.** Acceptance: `store_digest` and a 1,000-query historical sample
(random `as_of_tt` ∈ observed tt range) byte-identical before/after.

### 5.7 Errors and diagnostics
Rust errors carry: category (corrupt | not_found | capacity | io |
invariant), file, offset/row, expected-vs-found. PyO3 maps them to the
existing `tgms.core.errors` taxonomy (`StateError` for invariant,
`NotFoundError` for unknown identity/uid — matching the messages the ABC
contract implies, e.g. `dense_ids` raises `NotFoundError(f"unknown uid: …")`).
Corruption is always detected (checksums) before results are returned;
the remedy message names the file and says: restore generation or
`tgms replay`.

---

## 6. PyO3 API (module `tgms._engine`; coarse boundary, GIL released in scans)

```python
class NativeStore:
    @staticmethod
    def open(path: str) -> "NativeStore"                  # creates layout if absent
    def snapshot(self) -> "Snapshot"                      # committed generation
    def begin(self, tt: int) -> None
    def stage_nodes(self, cols: dict[str, list | np.ndarray]) -> None
        # keys: vid, uid, label, vt_s, vt_e, tt_s, tt_e, props(canonical str),
        #        source, provenance_ref  — one call per insert_node_versions
    def stage_edges(self, cols: dict) -> None             # + eid, src, dst, rel_type, disc
    def stage_closes(self, kind: str, vids: list[str], tt_e: int) -> None
    def working(self) -> "Snapshot"                       # committed ⊕ staging
    def commit(self) -> int                               # returns generation
    def rollback(self) -> None
    def compact(self) -> int
    def stats(self) -> dict
    def close(self) -> None

class Snapshot:
    def scan_edges(self, req: dict) -> dict[str, np.ndarray]      # materialized SoA
    def scan_nodes(self, req: dict) -> dict[str, np.ndarray]
    def scan_edges_batches(self, req: dict) -> Iterator["Batch"]  # kernels path
    def believed(self, kind: str, identity: str, as_of_tt: int) -> list[dict]
    def believed_any(self, uids: list[str], as_of_tt: int) -> list[bool]
    def props_for_vids(self, kind: str, vids: list[str]) -> dict[str, str]
    def dense_ids(self, uids: list[str]) -> np.ndarray            # int64 out
    def uids_for(self, ids: list[int]) -> list[str]
    def num_entities(self) -> int
    def all_versions(self, kind: str) -> Iterator[dict]           # digest path
```

Ownership rule: every returned array/batch holds a reference to its
Snapshot; the Snapshot pins its manifest generation and mappings. Dropping
the store while arrays are alive is safe (Rust side keeps files open).
Never expose a pointer whose backing can vanish.

`NativeAdapter` maps ABC → this API 1:1 (`begin/commit/rollback` direct;
reads go to `working()` when a batch is open, else `snapshot()`). It
contains **no logic** beyond dict-key translation and error mapping —
if you find yourself writing algorithms in adapter.py, stop; they belong
in Rust.

Kernels (WP-N4) live in Rust behind dedicated calls
(`Snapshot.motif_count/motif_instances/interval_join/bucket_agg/
tcsr_slices`) whose signatures mirror the current `ops_motifs.py` /
`ops_paths.py` inputs; `ops_*.py` switch to them behind
`isinstance(adapter, NativeAdapter)` checks with the existing NumPy paths
retained for other backends.

---

## 7. Work packages

### WP-N0 — Skeleton, packaging probe, decisions  *(gate: install)*
- D-028 + D-029 entries in `docs/DECISIONS.md` (awaiting sign-off marker).
- Cargo workspace + maturin build; `tgms._engine.ping()` returns a NumPy
  array through the boundary; CI job builds wheels (linux x86_64, macOS
  arm64; CPython 3.11–3.13) and runs `pip install dist/*.whl && python -c
  "import tgms._engine"`.
- **Acceptance:** fresh venv wheel install works on both platforms, no
  Rust toolchain; `make ci` still < 15 min.

### WP-N1 — C0 substrate  *(gate: crash-safe generations)*
- Manifest read/write + CURRENT protocol; dict.log; commit protocol incl.
  fsync order (§5.2); snapshot handles; event-log offset/chain fields;
  §2.2 derivation module + Python test vectors; checksums.
- `cargo test` units: commit protocol step-kill matrix (kill after each
  fsync/rename step — reopen must serve the previous generation or detect
  torn state), dict replay-stability, derivation vectors.
- **Acceptance commands:** `cargo test -p tgms-engine-core`;
  `uv run pytest tests/ -q` still green (nothing wired yet);
  parity vectors test in `crates/tgms-engine-core/tests/derive.rs`.

### WP-N2 — Segments, staging, scan, performance probe  *(gate: scan)*
- §4.3 writer/reader (event lane + interval lane), staging with
  read-your-own-writes, lane routing (K-rule), tie-group splitting,
  mmap + buffered readers behind one trait, `scan_edges/scan_nodes`
  materialization matching duckdb_adapter output byte-for-byte on dtype
  and order.
- **Performance probe report** (`docs/engine_probe.md`): scan throughput
  Rust vs a NumPy reference on 1e6/1e7 synthetic; ranks kernel porting
  order; measures the parity-assertion ingest overhead (§5.2).
- **Acceptance:** round-trip property test (random op batches → native vs
  in-memory reference → identical `all_versions`); scan gate §10.1 at 1e6.

### WP-N3 — Semantic completeness  *(gate: the full ABC, correctness only)*
- Close runs + staged-row closes; historical visibility; linear-scan
  `believed_*` / `believed_any` / `props_for_vids`; `all_versions`;
  `stats` from manifest; minimal compaction (§5.6); `NativeAdapter`
  complete; `store.py` gains `backend="native"`.
- `[tests]` commit (separate, justified, awaits approval): conftest
  gains a `TGMS_TEST_BACKEND` env var so the whole suite can run against
  native. **Do not proceed past this WP until approved.**
- **Acceptance commands:**
  `TGMS_TEST_BACKEND=native make test-full` green;
  replay parity: rebuild from `benchmarks/frozen-v1/collegemsg.eventlog.jsonl`
  per its README with `--backend native`, assert digest equals the DuckDB
  rebuild's; compaction equivalence test (§5.6); fault-injection pytest
  module for the §4 formats (truncated segment, corrupt footer, partial
  close run, CURRENT/manifest mismatches, torn dict append).

### WP-N4 — Indexes and kernels  *(gate: performance)*
- Identity postings (+ believed_* switch-over), name index (current-
  canonical; wire to `ops_snapshot.py:377` name lookup), adaptive TCSR
  (sparse active-vertex form; dense per threshold), prefix-max staircase
  already in WP-N2 format — now used by interval-lane scans.
- Kernels in probe-ranked order — expected: interval join (`co_active`),
  motif join (retire the in-memory DuckDB path in `ops_motifs.py`),
  bucket group-agg, traversal slices. Oracle suites stay green after each
  switch-over (they are the arbiter, per §1.1 never edited).
- **Acceptance:** `TGMS_TEST_BACKEND=native make test-full` green with
  DuckDB uninstalled from the venv; gates §10.2–10.4.

### WP-N5 — Soak, switch, ship  *(gate: default backend)*
- A/B harness `scripts/ab_digest.py`: replays any event log into both
  backends, asserts digest equality after **every batch**; run on all
  frozen logs + synthetic 1e6/1e7.
- Full fault-injection matrix (§15 list in review round 1) as pytest;
  recovery tooling (`tgms store verify` — checksum walk + chain check);
  diagnostics polish; docs (README backend section, format doc).
- Flip default backend to native; DuckDB → `tgms[duckdb]` extra; wheel
  release dry-run through the trusted-publishing workflow.
- **Acceptance:** A/B green everywhere; `make reproduce` (through
  suite-gen) green on native; fresh-machine wheel install runs the
  5-minute quickstart.

---

## 8. Testing obligations (beyond existing suites)

| Layer | New tests (all yours to write; none replace human-owned suites) |
|---|---|
| Rust unit | format round-trips, checksum failures, commit step-kill, tie-group splitting, lane routing, staircase monotonicity, visibility truth-table (staged/committed × open/closed × as_of) |
| Rust fuzz | segment + close-run + manifest parsers (`cargo fuzz`, corpus checked in, run in nightly CI not `make ci`) |
| Parity | derivation vectors vs Python; scan output vs duckdb_adapter on random stores (property test); A/B digest harness |
| Fault injection | §4 torn/corrupt cases through the Python API — assert clean error + previous-generation service, never wrong data |
| Perf | `tgms bench ops --backend {duckdb,native}` + `docs/engine_probe.md` receipts |

---

## 9. Things you must NOT do

- Edit `tests/`, `tgms/temporal/oracle.py` outside an approved `[tests]`
  commit. Edit frozen benchmarks. Re-ingest canonical stores.
- Store full eid strings, re-serialize props, coalesce adjacent versions,
  reorder digest fields, "fix" the vid formula, auto-GC files, add a
  second durability mode, implement dense visibility sidecars /
  compression / trigram index / parallel kernels (all E6+ — reserved
  format tags exist; leave them unimplemented).
- Add dependencies beyond §12 without a DECISIONS entry.
- Put algorithms in `adapter.py` or per-row calls across the PyO3
  boundary.

---

## 10. Performance gates (measure on the 40-core bench host; receipts per §1.4)

| # | Metric | Gate |
|---|---|---|
| 10.1 | `edges_columnar`, 1M-row current-belief window, full SoA incl. string columns | ≤ 10 ms (raw predicate pass and materialization reported separately) |
| 10.2 | `co_active` @ 1M events | ≤ 100 ms (today: ~5.3 s) |
| 10.3 | every `tgms bench ops` row @ 1e6 and 1e7 | ≥ M3 floor (`docs/TECHNICAL_REPORT.md` §8.1: entity_history 51 ms, series 155 ms, snapshot 2-hop 99/272 ms, diff 163/485 ms) |
| 10.4 | point `believed_*` lookup, warm, indexed | ≤ 10 µs |
| 10.5 | bulk ingest 1e7 events | ≥ DuckDB backend wall-clock; report bytes/row (expect 30–60 B raw v0) |
| 10.6 | store size @ 1e7, uncompressed v0 | ≤ 0.5× DuckDB's 2.6 GB |

Failing a gate is a finding to report with profiles, not something to
paper over by weakening the measurement.

---

## 11. Risks and prescribed fallbacks

- **R-N1 Python json.dumps mismatch in Rust** → the §2.2 vector suite
  catches it; if a corner is unmatchable, escalate (do not approximate) —
  eid inputs are plain strings, so none is expected.
- **R-N2 parity assertion too slow on bulk ingest** → measured switch to
  per-block sampling (§5.2), never removal.
- **R-N3 mmap pathologies on cluster storage** → `BufferedSegmentReader`
  is the fallback policy; selection is a store-open option, benchmarked
  in WP-N2.
- **R-N4 wheel matrix trouble** → wheels are the release blocker, not
  sdist; keep DuckDB default until WP-N5 acceptance passes on both
  platforms.
- **R-N5 `[tests]` approval latency** → WP-N3 backend wiring can be
  developed against a local branch; nothing merges until approval (§1.1).

## 12. Dependencies for D-029 (licenses verified before use)

Rust: `pyo3` (Apache-2.0/MIT), `serde`+`serde_json` (MIT/Apache-2.0),
`sha2` (MIT/Apache-2.0), `crc32c` or `crc` (Apache-2.0/MIT), `memmap2`
(MIT/Apache-2.0), `thiserror` (MIT/Apache-2.0), `numpy` crate for pyo3
(BSD-2). Build: `maturin` (MIT/Apache-2.0), `cibuildwheel` (BSD-2, CI
only). Later (E6+, separate entry): `rayon`, compression codecs.
