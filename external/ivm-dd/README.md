# ivm-dd — differential-dataflow IVM configuration (P-EXT2)

Implements `docs/design/EXTERNAL_BASELINES_DESIGN_2026-10-02.md` **§3** ("P-EXT2 —
incremental view maintenance with differential dataflow") for the TGMS
external-baseline campaign, per the pre-registration in
`docs/design/OSDI27_AUDIT_AND_PLAN_2026-09-13.md` §4.3b (P-EXT2 / Addendum
EXT-A). Also implements §1's export-format consumption (the lane-X1 bundle,
as actually produced by `scripts/export_storm_workload.py`), §4.1's per-burst
quantities, and §3.5's withheld-correction check.

**Crate location.** This task placed the crate at `external/ivm-dd/`. The
memo's own §3.1 says `benchmarks/external-v1/ivm-dd/`. That is a real
discrepancy between the two documents this crate was built against — reported
here and in the lane report, not silently resolved either way.

## Versions

- Rust toolchain: **1.97.1** (`rust-toolchain.toml`, pinned)
- `timely` **0.31.0**, `differential-dataflow` **0.25.1** — the latest
  crates.io releases at build time (2026-10-02), pinned exactly in
  `Cargo.toml` and recorded (with their full transitive tree) in the
  committed `Cargo.lock`
- `serde`/`serde_json` 1.x, `sha2` 0.10, `clap` 4.x

## Build (on xzgpu; never on the laptop — see "Where this ran")

```sh
cd external/ivm-dd
cargo build --release
```

This crate declares its own empty `[workspace]` in `Cargo.toml` specifically
so `cargo build` from the repo root never tries to pull it into
`crates/tgms-engine-core`/`crates/tgms-engine-py`'s workspace (memo §3.1).

## CLI

```sh
ivm-dd run <cell-dir> --out <out-dir>        # full per-burst run; writes result.json + run.log
ivm-dd shape-test <cell-dir>                 # epoch-0 digest vs oracle.jsonl, every family
ivm-dd withheld <cell-dir> --out <out-dir>   # the 37th-cell F-epoch/F-watermark check (needs >=11 batches)
```

`<cell-dir>` is one lane-X1 exported cell directory (`versions-epoch0.jsonl`,
`artifacts.jsonl`, `deltas.jsonl`, `oracle.jsonl`, `export-manifest.json` —
see `src/export.rs`'s doc comment for the exact bundle shape as
`scripts/export_storm_workload.py` actually produces it, which differs in a
few particulars from memo §1.3's description; `src/export.rs` follows the
real script).

## Architecture

- `src/model.rs` — the bitemporal `VersionRow` (mirrors
  `tgms.core.model.NodeVersion`/`EdgeVersion`).
- `src/digest.rs` — canonical-JSON + sha256, independently reimplemented to
  match `tgms.core.model.canonical_json`/`digest` and
  `tgms.temporal.algebra._canonicalize_floats` byte-for-byte (pinned in
  `tests/digest_tests.rs` against literal values computed by the real Python
  on the laptop). The Python side of the same contract lives at
  `external/neo4j-recompute/neo4j_recompute/canon.py` (lane N1); the two are
  independent implementations that must agree, not a shared module — Rust and
  Python can't share code here, only the contract.
- `src/changelog.rs` — memo §3.1's changelog encoding: a version is inserted
  (+1) at the epoch of its `tt_s`, retracted (−1) at the epoch whose
  transaction closed it. Replays the export bundle's `deltas.jsonl` forward
  through its own live table to recover the exact old row a `closed` entry
  needs retracted, and computes the `retracted_vt_span` covariate (memo A6).
- `src/dataflow.rs` — the maintained dataflow: one timely worker (memo §3.4),
  built with `differential-dataflow` 0.25.1's actual API (`Collection::reduce`/
  `join_map`/`consolidate`/`arrange_by_key`, all inherent methods — no
  separate trait imports needed in this version). See its module doc for the
  routing scheme and every deviation from the memo, in detail.
- `src/families/*.rs` — the 13 registered operator families as pure payload
  functions (`tgms/temporal/ops_*.py`'s logic reimplemented in Rust), each
  unit-tested directly and cross-checked end to end against real TGMS oracle
  digests via `tests/fixtures/tiny1/`.
- `src/withheld.rs` — the 37th-cell withheld-correction check (memo §3.5).
- `src/record.rs` — `result.json`/`run.log` writer.

## Family → dataflow table, as implemented (vs. memo §3.3)

| F | family | memo's design | as implemented | deviation |
|---|---|---|---|---|
| F1 | entity_history | join params⋈nodes_by_uid⋈edges; reduce per artifact | Uid route; reduce calls `f1_entity_history::compute` | none (matches) |
| F2 | version_history | interval routing⋈params; filter; reduce | Kind route (`k:node`/`k:edge`); reduce filters window/belief inside the closure | routing coarser (whole-kind scan, not window-bucketed) |
| F3 | snapshot_subgraph | params by seed⋈incident edges⋈nodes; reduce | **Global** route; reduce does the 1-hop BFS over the full snapshot | routing coarser — F3 needs a neighbour's own node row (different uid than the seed), which a flat uid route can't see without a second dependent join |
| F4 | diff_snapshots | instant routing at t1/t2; reduce | Global route; reduce computes both snapshots + diff | none beyond routing (memo's own registered instances are all `scope: null`, i.e. already whole-graph) |
| F5 | neighborhood_evolution | params by uid⋈incident edges; reduce | Uid route; reduce calls `f5_neighborhood_evolution::compute` | none (matches) |
| F6 | aggregate_events | event routing⋈params; count; reduce | Global route; reduce groups+counts | routing coarser |
| F7 | graph_metric_timeseries | event routing⋈params; count; reduce | Global route; reduce buckets+counts | routing coarser |
| F8 | burst_detection | params by uid⋈incident edges; reduce | Uid route; reduce computes the z-score series | none (matches) |
| F9 | count_temporal_motifs | event routing⋈params ⇒ per-(art,pair); reduce per pair; map+count | Global route; reduce does a direct O(events³)-worst-case (art,pair) scan inline (`families::motif_common`) | not per-(art,pair) keyed in DD — one (larger) reduce per artifact instead; **flagged for the memo's own Opus review of F9–F12** |
| F10 | find_temporal_motif_instances | as F9 + per-pair top-k merge | Global route; same inline scan, global sort+paginate | same as F9 |
| F11 | temporal_reachability | **`iterate`**: arrivals⋈edges_by_src; `reduce(min)`; seed `concat` | Global route; reduce calls `fixpoint_arrivals` (plain-Rust label-correcting fixpoint) fresh every time | **does not use `iterate`** — see "F11: the iterate finding" below. This is the one departure from both the memo *and* this task's explicit instruction; reported, not silently patched. |
| F12 | temporal_paths | four unrolled hop-joins; reduce: count, top-k | Global route; reduce runs a plain-Rust bounded DFS (`families::f12_temporal_paths::dfs_paths`), then sorts+caps top-k | not expressed as four static DD joins; **flagged for Opus review**, same reasoning as F9/F10 |
| F13 | compute (∅-scope control) | trivial, never changes | seeded once directly into `state` at epoch 0, outside the join/reduce machinery entirely (`RouteKind::None`) | none — matches "trivial" exactly |

**Routing, overall (deviation from memo §3.2, flagged):** the memo's
256-bucket valid-time routing bounds how many *artifacts* a single changed
version can fan out to, at 60k-edge scale. This crate routes by natural key
instead — a version fans out to its own node uid(s) (`"u:<uid>"`), a kind
bucket (`"k:node"`/`"k:edge"`), or a global bucket (`"g:all"`) — which is
correct (every artifact's route key is guaranteed to receive every row it
needs) but coarser for the families that are whole-population scans anyway
(F2, F3, F4, F6, F7, F9, F10, F11, F12). This is a scalability simplification
made under this session's time constraints, not a correctness one; it is the
first thing to revisit if a real cell's per-burst wall at scale disagrees
badly with the estimate below.

### F11: the `iterate` finding

The memo names `temporal_reachability` as the one family needing DD's
`iterate` (§3.3), and the task instructions require it. A genuine
`iterate`-based dataflow was built: a shared `edges_by_src` arrangement built
once outside the loop, entered into the iterative subscope each round,
`join_core`'d against the current per-artifact arrival frontier (re-keyed to
join on plain uid, then re-keyed back to `(artifact, uid)`), `concat`'d with
the re-supplied seed, and `reduce(min)`'d to the next round's frontier — the
textbook pattern (cf. `differential_dataflow::algorithms::graphs::bfs`).

It passed two of the three correction epochs on `tests/fixtures/tiny1`, but
failed the third: epoch 3's correction is a pure **retraction** (an edge
realizing the then-current-best arrival is retracted with no replacement
inserted). The maintained fixpoint did not fall back to the next-best path
through the graph — it kept reporting the pre-retraction answer. That is a
genuine incremental-maintenance bug inside the `iterate` wiring (every other
family handles the identical retraction correctly on the same fixture, via
the ordinary route+reduce path), and this session could not root-cause it in
the time remaining — plausible suspects (not confirmed): the per-round
`reduce(min)`'s consolidation interacting with the re-supplied `seed` in a way
that masks the retraction, or a subtlety of how `Product<T, u64>` timestamps
inside the iterative subscope interact with the outer epoch's retraction
arriving at the same outer time as later insertions.

Per this task's instruction to "stop and report rather than improvise": F11 is
therefore wired through the same Global route+reduce path as F9/F10/F12,
calling the already-tested `fixpoint_arrivals` fresh on every change. This is
correct (confirmed by `tests/fixtures/tiny1`, which checks F11 like every
other family) but not incremental at the sub-artifact level the memo intends,
and is the first candidate for the Opus review the memo already calls for on
F9–F12 — that review should now cover F11's `iterate` wiring too, which is
preserved in the git history (the commit immediately before the fallback) for
whoever picks this up.

## Validation performed

1. **Unit tests** (`cargo test`, all on xzgpu): changelog encoder
   (`tests/changelog_tests.rs`), canonical-JSON/digest against literal Python
   output (`tests/digest_tests.rs`), each family's pure logic on hand-worked
   cases (`tests/family_tests.rs`), the withheld-correction check on a
   hand-built 11-batch bundle (`tests/withheld_tests.rs`) — 30 tests, all
   green.
2. **Shape test against a deterministic tiny synthetic cell**
   (`tests/fixtures/tiny1/`, generated by
   `tests/fixtures/generate_tiny1.py` from the *real* TGMS Python harness: 6
   nodes, 10 edges, 13 artifacts — one per family — 3 corrections, with every
   `oracle.jsonl` digest computed by the actual
   `tgms.artifact.refresh.refresh` call path, never hand-rolled):
   `tests/tiny_cell_tests.rs` asserts every artifact's digest from this
   crate's `dataflow::run` equals the real oracle's digest at epoch 0 *and*
   after every one of the 3 bursts. **13/13 families agree, every epoch.**
   This is the real shape test's offline, always-available twin — see "What
   is still blocked" for why the real one (memo §4.6 item 3, against an
   exported collegemsg cell) could not be run this session.
3. `cargo clippy --release --all-targets -- -D warnings`: clean.
4. **Smoke run** (`ivm-dd run tests/fixtures/tiny1`, `nice -n 19`, xzgpu):
   3 bursts, `load_ms ≈ 2.6`, per-burst `refresh_ms` 1.3–1.8, `publish_ms`
   0.04–0.11, oracle agreement 52/52 at every epoch including all three
   bursts. See "Wall estimate" below for why this cannot calibrate the real
   grid.

## What is still blocked

Lane X1 (`scripts/export_storm_workload.py`) has not yet produced any real
exported cell on xzgpu: `/mnt/project/xzhang/tgms/external-v1/export/` does
not exist, and no `export_storm_workload.py`/`ext_export.py` process was
running at the time this crate was built and tested (checked directly: no
matching process, no output directory, `synth-iv-60k` absent from
`stores/`). This blocks:

- Validation step (2) in the task's own ordering — a shape test against a
  real exported collegemsg cell, checked against its own committed
  `oracle.jsonl`/TGMS result digest. `tests/fixtures/tiny1/`'s shape test is
  real, but it is not *that* test.
- The withheld-correction check against the real 37th cell
  (`synth-iv-60k/c4/deep/seed0`), which needs `synth-iv-60k` built (absent on
  xzgpu) and ≥11 committed batches; `tests/withheld_tests.rs` exercises the
  same code path on a hand-built bundle instead.
- A calibrated smoke-run wall estimate at real scale (900–10,000 artifacts,
  up to 60k edges) — `tests/fixtures/tiny1`'s 6-node/10-edge/13-artifact
  scale says nothing about the cost of this crate's deliberately coarser
  routing or F9/F10/F12's O(events³)-worst-case/DFS reduce closures at that
  scale.

This is reported as a blocker, not worked around with a fabricated cell.

## Wall estimate for 43 cells + probe (uncalibrated — see above)

The memo's own §3.7 estimate (load ≈1–3 min/cell, bursts ms–s, checking ≈5
min ⇒ 36 cells ≈4–6 h; probe ≈1 h; 37th cell ≈0.5 h; 8-worker rerun ≈1.5 h ⇒
**≈8 h total**) is the only estimate available until a real exported cell
lands — `tests/fixtures/tiny1` is 1,500–2,000× smaller than the storm-v2
grid's N≈900 cells (6 nodes/10 edges vs. hundreds of nodes/tens of thousands
of edges, 13 artifacts vs. ~900) and its sub-2-ms refresh times cannot be
scaled up responsibly.

Two reasons this crate's actual number is likely to come in **above** the
memo's estimate, worth a recalibration pass before the timed grid runs:

- **Routing.** Every F2/F3/F4/F6/F7/F9/F10/F11/F12 artifact (9 of 13 families)
  re-scans its *entire* relevant row set on *every* burst that touches
  anything in that scope, rather than only the bucket-routed subset the memo's
  §3.2 scheme would give it. At N≈900 this is probably fine; at the N=10,000
  probe (8,922 artifacts, memo §4.5's own flagged risk cell for F12) it may
  not be.
- **F9/F10/F12 algorithmic shape.** Direct O(events³)-worst-case motif
  enumeration and a plain recursive DFS, not the memo's windowed
  engine-kernel index or unrolled joins. Memo §4.5 already flags this exact
  risk ("F12 (and F9/F10) blow-up... 4-hop prefixes can reach 10⁶ per
  artifact") independently of this crate's routing choice.

**Recommendation:** once X1 produces even one real cell, run `ivm-dd run` on
it under `nice -n 19` (untimed) before scheduling the timed grid, exactly as
the task's validation step (4) asks — this crate's own per-burst wall at that
point either confirms the memo's ≈8 h estimate or gives the coordinator a
real number to re-plan against. This session's own position: do not start the
timed grid on the memo's estimate alone.
