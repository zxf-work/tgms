# ivm-dd — incremental view maintenance of TGMS artifacts with differential dataflow

**What it consumes.** One exported storm-v2 cell, as written by
`scripts/export_storm_workload.py`: the epoch-0 version table
(`versions-epoch0.jsonl`), the registered artifacts (`artifacts.jsonl`,
`{name, op, args}`), one line of corrections per burst (`deltas.jsonl`:
closed and inserted versions), TGMS's own result digest for every artifact
at every epoch (`oracle.jsonl`) and the manifest (`export-manifest.json`).
`src/export.rs` documents the exact bundle shape.

**What it emits.** For each burst k: `refresh_ms` (push the burst's
retractions and insertions, then maintain every view until the probe has
passed epoch k), `publish_ms` (hash the changed payloads), the artifacts
whose output changed, `retracted_vt_span`, and each changed artifact's
payload digest — the canonical-JSON sha256 that TGMS's `result_digest`
computes, so every value is checked against the oracle. `ivm-dd run`
writes this as `result.json` + `run.log` (`src/record.rs`).

**What it is for.** It is the incremental-view-maintenance configuration
in TGMS's external comparison: the same event log consumed as a
changelog, corrections applied as retraction plus re-insertion at their
valid time, every artifact family a maintained dataflow, "fresh" meaning
the view has absorbed the burst. Its per-burst numbers are what the
campaign's pre-registered predictions for this comparison are scored on;
which families are maintained incrementally, and what each update
re-evaluates, is stated below so that the scope of the "genuine IVM" claim
can be read off this file ("Family → dataflow" and "What is recomputed per
update").

The crate lives at `external/ivm-dd/` (the campaign design placed it at
`benchmarks/external-v1/ivm-dd/`; the result records name the path used).

## Versions

- Rust toolchain: **1.97.1** (`rust-toolchain.toml`, pinned)
- `timely` **0.31.0**, `differential-dataflow` **0.25.1** — the latest
  crates.io releases at build time (2026-10-02), pinned exactly in
  `Cargo.toml` and recorded (with their full transitive tree) in the
  committed `Cargo.lock`
- `serde`/`serde_json` 1.x, `sha2` 0.10, `clap` 4.x

## Build (on xzgpu)

```sh
cd external/ivm-dd
cargo build --release
```

The crate declares its own empty `[workspace]` so `cargo` never pulls it
into the repository's engine workspace (`crates/tgms-engine-core`,
`crates/tgms-engine-py`).

## CLI

```sh
ivm-dd run <cell-dir> --out <out-dir>        # full per-burst run; writes result.json + run.log
ivm-dd shape-test <cell-dir>                 # epoch-0 digest vs oracle.jsonl, every artifact
ivm-dd withheld <cell-dir> --out <out-dir>   # the withheld-correction check (needs >= 11 bursts)
```

## Architecture

- `src/model.rs` — the bitemporal `VersionRow` (mirrors
  `tgms.core.model.NodeVersion`/`EdgeVersion`).
- `src/digest.rs` — canonical JSON + sha256, reimplemented to match
  `tgms.core.model.canonical_json`/`digest` and
  `tgms.temporal.algebra._canonicalize_floats` byte for byte (pinned in
  `tests/digest_tests.rs` against values computed by the Python code).
- `src/changelog.rs` — the changelog encoding: a version is inserted (+1)
  at the epoch of its `tt_s` and retracted (−1) at the epoch whose
  transaction closed it. A closed version stays in the collection as a
  historical row (the open row is retracted and re-inserted with its
  finite `tt_e`), which is what `version_history`/`entity_history` report.
  Every operator that reads "the current graph" therefore filters
  `believed_at(as_of)` itself.
- `src/views.rs` — **the maintained dataflows**, one per family, with the
  artifacts as data (a static `params` collection inserted at epoch 0),
  and the valid-time bucketing.
- `src/dataflow.rs` — the driver: one timely worker (the scored
  configuration), the input sessions, per-burst push / advance / step until
  the probe passes the epoch, `inspect`-captured output changes, timing.
  Also `compute_payload_pub`, each family's whole-store reference.
- `src/families/*.rs` — the 13 operator families' payload logic
  (`tgms/temporal/ops_*.py` reimplemented): each family's `compute` is the
  whole-store reference, and the payload assembly helpers
  (`payload_from_counts`, `payload_from_items`, `payload_from_paths`, …)
  are shared by the reference and the maintained dataflow, so the two
  cannot drift apart in formatting.
- `src/withheld.rs` — the withheld-correction check (F-epoch and
  F-watermark feeders).
- `src/record.rs` — `result.json`/`run.log` writer.

## Valid-time routing

B = 256 equal buckets over the store's epoch-0 valid-time extent
`[lo, hi)` (smallest `vt_s` to the largest finite end), plus one overflow
bucket for everything at or past `hi` — open-ended intervals and
corrections placed past the extent land there. The extent is recorded in
`result.json` (`config.routing.valid_time_extent`). Bucketing is only an
index: every bucket join is followed by the exact predicate.

- **Event families** (F6, F7, F9, F10): an edge event is routed to the
  bucket of its `vt_s` (one copy); an artifact registers in every bucket
  its window `[t_a, t_b)` covers. A changed event reaches only the
  artifacts whose windows cover its bucket.
- **Interval family** (F2): a version is arranged by its start bucket
  (`iv_start`) and by every further bucket it covers (`iv_cont`); the
  artifact's window by every bucket it covers and by its start bucket. The
  band join is two joins — version start bucket inside the window's
  buckets, or window start bucket strictly inside the version's later
  buckets — so each overlapping (version, artifact) pair is emitted
  exactly once, at the later of the two start buckets. Only a compact
  reference (vid, valid and transaction times) is routed; the full row is
  fetched by `vid` for matched pairs only.
- **Instant family** (F4): the artifact registers in the bucket of `t1`
  and of `t2`; a version meets it through `iv_start ∪ iv_cont`, once per
  instant it is valid at.
- **Uid families** (F1, F3, F5, F8) join on uid with `nodes_by_uid` and the
  uid's incident edges (`edges_by_src ∪ edges_by_dst`, self-loops once).
- **Traversal families** (F11, F12) follow `edges_by_src` from the
  artifact's source; the window and belief predicates are applied per hop.

**Fan-out.** Registered windows are 5, 10, 25 or 50 % of the extent, so an
event-family artifact sits in about 13 to 129 buckets and an event in
exactly one; a changed event reaches the artifacts whose window bucket
range contains its bucket — on average about the window fraction of that
family's artifacts, not all of them. Interval routing copies a version
into every bucket it covers: collegemsg edges are instants (`vt_e = vt_s +
1`, one bucket each) and its 1,899 node versions are open-ended (up to 257
compact references each, about 0.5 M in total); for synth-iv-60k (60k
edges, intervals 0.5–50 % of the extent) the expectation is ≈ 64 buckets
per edge, ≈ 4 M compact references — only built when F2 or F4 artifacts are
registered, and only for the kinds they read. A retraction of a long-lived
version is exactly where this fan-out bites, which is the mechanism the
deep-age prediction is about.

## Family → dataflow, as implemented

"Maintained": every operator upstream of the final per-artifact `reduce` is
incremental (joins, `count_total`, `distinct`, `iterate`), and that final
`reduce` only formats the artifact's maintained input group (next
section). F9/F10 are maintained at endpoint-pair granularity: the pair's
instance count and first instances are re-evaluated over that pair's
events when one of them changes.

| F | family | dataflow (DD operators) | status |
|---|---|---|---|
| F1 | entity_history | params by uid ⋈ `nodes_by_uid`, ⋈ `edges_by_src` ∪ `edges_by_dst`; reduce per artifact | maintained |
| F2 | version_history | valid-time band join (`iv_start`/`iv_cont` × window buckets), filter overlap and `tt_s ≤ as_of`, ⋈ `versions_by_vid`; reduce per artifact | maintained |
| F3 | snapshot_subgraph | seeds ⋈ incident edges valid and believed at `t_valid`, one join per hop (`distinct` node set); set ⋈ `nodes_by_uid`; induced edges = set ⋈ `edges_by_src`, semijoin dst ∈ set; reduce per artifact | maintained |
| F4 | diff_snapshots | instant routing at t1 and t2 ⋈ `iv_start ∪ iv_cont`, filter valid and believed, ⋈ `versions_by_vid`; reduce per (artifact, entity) → added / removed / props-changed item, only when different; reduce per artifact over the items | maintained |
| F5 | neighborhood_evolution | params by uid ⋈ incident edges (believed, valid at t1/t2 or overlapping [t1, t2)); reduce per artifact | maintained |
| F6 | aggregate_events | event routing ⋈ window buckets, filter window, belief, `rel_types`; `count_total` per (artifact, src or dst); reduce per artifact sorts the groups | maintained |
| F7 | graph_metric_timeseries | event routing; `count_total` per (artifact, series bucket); reduce assembles the series | maintained |
| F8 | burst_detection | params by uid ⋈ incident edges, filter belief and window; `count_total` per (artifact, series bucket); reduce computes the z-score flags | maintained |
| F9 | count_temporal_motifs | event routing ⇒ events per (artifact, unordered endpoint pair); reduce per (artifact, pair) counts that pair's instances (O(events × delta-run)); `explode` + `count_total` sums per artifact; `count_total` of window events | maintained at pair granularity |
| F10 | find_temporal_motif_instances | as F9; the per-(artifact, pair) reduce also emits the pair's first `offset + limit` instances; reduce per artifact takes the first `offset + limit` of their union (sorted) | maintained at pair granularity |
| F11 | temporal_reachability | **`iterate`**: arrivals (artifact, node) → min τ; body = arrivals ⋈ `edges_by_src` (entered), filter belief and τ' = max(τ, vt_s) < min(vt_e, t_b), `concat` the seeds (artifact, src, t_a), `reduce(min)`; `consolidate` after `leave`; reduce per artifact paginates | maintained |
| F12 | temporal_paths | prefixes from (artifact, src) extended by one join per hop along `edges_by_src` (unrolled to the largest registered `max_hops`, 4), filter per hop (belief, τ monotone and < min(vt_e, t_b), node-simple, interior ≠ dst); last hop joined directly on (node, dst); hop pruning: a prefix continues only if dst is within the remaining hops (backward feasible-edge distance, maintained by joins + `distinct`); reduce per artifact: count and top-k by (arrival, hops, key) | maintained |
| F13 | compute (∅-scope control) | constant, seeded once at epoch 0 | trivial |

`co_active` has no registered instances and is not built. Every family is
expressed as a dataflow; none is removed from the comparison on
expressibility grounds.

## What is recomputed per update

Differential dataflow's `reduce` re-evaluates its closure over the whole
input group of every key whose input changed. These are the reduce keys
and their groups; nothing else in the crate is re-evaluated from scratch on
an update.

| F | reduce key | group re-read when it changes |
|---|---|---|
| F1 | artifact | the uid's node versions and incident edges (all beliefs) |
| F2 | artifact | versions of the artifact's kind overlapping its window |
| F3 | artifact | node versions of the 1-hop candidate set; edges among it valid at `t_valid` |
| F4 | (artifact, entity) | that entity's versions valid at t1 / t2 (one or two rows) |
| F4 | artifact | the entities that differ between t1 and t2 |
| F5 | artifact | the uid's incident edges relevant to t1, t2, [t1, t2) |
| F6 | artifact | one count per group key in the window |
| F7, F8 | artifact | one count per series bucket (≤ 17) |
| F9, F10 | (artifact, endpoint pair) | the pair's events in the window |
| F9 | artifact | two totals |
| F10 | artifact | the union of the pairs' first `offset + limit` instances, and the total |
| F11 | (artifact, node), inside `iterate` | candidate arrivals for that node (min) |
| F11 | artifact | the reachable set (for pagination) |
| F12 | (artifact, node) | backward-distance candidates (min) |
| F12 | artifact | the artifact's complete paths (count, top-k) |

The largest groups are F2's (every node version is open-ended, so a window
overlaps all of them), F11's reachable set and F12's complete-path set;
each is re-read only when a change reaches that artifact.

## F11: the `iterate` retraction bug and its fix

The earlier build of this crate abandoned `iterate` for F11 after it
failed on `tests/fixtures/tiny1`'s third burst: a pure retraction of the
edge realizing node n2's best arrival (n0 → n2 at 150) left the maintained
answer at the pre-retraction value instead of falling back to n0 → n1 → n2
(200). The failing `iterate` code was not kept, so it was rebuilt as
described (arrivals ⋈ entered `edges_by_src`, `concat` seeds, `reduce(min)`)
in a minimal F11-only probe. **Root cause:** the changelog keeps a closed
version as a historical row — the burst retracts the open row and
re-inserts the same row with its finite `tt_e` — and a loop body that joins
arrivals with every edge row without the `believed_at(as_of)` filter still
traverses the superseded n0 → n2 version and still derives 150; the probe
reproduces the reported symptom exactly in that case and only then. Differential dataflow's
`iterate` was not at fault: the variable at round k+1 is the body applied
to round k, starting from the seeds, so once the superseded row is not
traversable the old arrival has no derivation and is withdrawn. A second,
independent hazard was also present in that shape: without `consolidate`
after `leave`, the output carries cancelling ± updates from different
rounds at the same outer epoch, and applying them in arrival order can drop
a still-valid arrival (seen as n5 disappearing after a pure insertion).
Both reproduce in a minimal F11-only probe on the fixture (no filter:
epochs 0–2 agree, epoch 3 stays at 150; filter, no consolidation: epoch 2
loses n5; both: all epochs agree), and both are fixed in `views.rs`. The
regression tests are `tests/maintained_tests.rs`
(`f11_iterate_falls_back_to_next_best_path_on_pure_retraction`,
`tiny1_epoch3_shape_superseded_row_is_not_traversable`).

## Validation performed (xzgpu)

1. `cargo test --release`: 37 tests — changelog encoder, digests against
   Python-computed values, each family's reference on hand-worked cases,
   the withheld-correction check, and `tests/maintained_tests.rs`:
   - F11 under pure retraction, double retraction, re-insertion and a
     corrected re-insertion; F11/F12 never traverse a superseded row;
   - a seeded randomized changelog (7 nodes, 54 edges, 8 bursts of
     closures, edge corrections that move valid time past the epoch-0
     extent, edge and node property corrections, insertions) with 36
     artifacts over all 12 data-reading families (different windows,
     `belief` modes, `kind`s, hops, `max_hops`, `limit`/`cursor`,
     `node_filter`, roles): maintained output equals the reference at every
     epoch, 1,620 comparisons over 5 seeds, every family non-trivial in at
     least one;
   - per-pair motif counting and first-k enumeration against the reference
     enumeration; monotonicity and coverage of the bucketing.
2. `cargo clippy --release --all-targets -- -D warnings`: clean.
3. **Oracle check on `tests/fixtures/tiny1/`** (6 nodes, 10 edges, one
   artifact per family, 3 bursts, every digest computed by TGMS's own
   refresh path by `tests/fixtures/generate_tiny1.py`): `ivm-dd
   shape-test` 13 agree, 0 disagree; `ivm-dd run` oracle agreement 52/52
   (13 artifacts × epochs 0–3), 0 disagree.
4. **First real exported cell** (`collegemsg-c3-none-n1000-s0`, 61,734
   versions, 934 artifacts over all 13 families, 20 bursts; export
   manifest's file hashes verified; untimed, `nice -n 19`, shared box at
   load ≈ 3, 2026-10-02T22:22Z): `ivm-dd shape-test` 934 agree, 0
   disagree; `ivm-dd run` oracle agreement 19,614/19,614 (934 artifacts ×
   epochs 0–20), 0 disagree, 0 not answered. Epoch-0 load 1.77 s, 0.58 GB
   RSS; per-burst `refresh_ms` median 4.1 (1.2–41.0, the maximum on the
   class-D `d2_full` burst), `refresh_ms + publish_ms` median 4.4. These
   are a calibration, not a scored measurement. The earlier build
   (per-artifact recomputation over globally routed rows) aborted on
   allocation at a 25 GB address-space limit during the same cell's
   epoch-0 load.
5. **Traversal state on a synth-iv-shaped graph** (synthetic: 600 nodes,
   60k edges, intervals 0.5–50 % of the extent, windows 5–50 %): F11 ≈ 6 MB
   and 0.07 s of epoch-0 load per artifact; F12 ≈ 150 MB and 0.7 s per
   artifact (40 artifacts: 6.2 GB, 29 s).

## Risks and what is still open

- **F12 state at N = 10,000.** At ≈ 150 MB per F12 artifact on a
  synth-iv-shaped graph, the probe's 317 F12 artifacts project to ≈ 48 GB,
  at the state cap; synth-iv-60k N = 1,000 cells (≈ 70 F12 artifacts)
  project to ≈ 10 GB. If the cap is exceeded, the family is removed for
  that cell and recorded. The projection is from a synthetic graph and has
  to be confirmed on the real store.
- **One real cell so far.** Only `collegemsg-c3-none-n1000-s0` was
  exported at the time of writing; synth-iv-60k (interval-valued edges,
  where F2/F4 fan-out and F12 state are largest) has not been checked on
  real rows.
- **F12 expansion budget.** TGMS gives up on a `temporal_paths` call after
  2,000,000 expansions and the oracle records it as refused (not
  compared); the dataflow has no such budget and always enumerates every
  path.
- **Withheld-correction check.** `withheld.rs` evaluates each family's
  reference over the rows delivered by the read point instead of replaying
  the dataflow; the two are equal by the tests above. (Its reference for
  F1 and F5 now scopes the snapshot to the uid, as the maintained join
  does; before, it handed them the whole snapshot and would have reported
  spurious false-fresh artifacts.)
