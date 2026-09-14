# storm-v1 — Correction Storm: C2/C5 build note and C6 pre-registration template

**Date: 2026-09-13. Status: infrastructure landed, no campaign run yet.**
This is not a results README (contrast `benchmarks/crash-v1/README.md`,
`benchmarks/m5-v1/`) — no measurement has been taken under this directory.
It is (a) a short note on what Lane C's C2 (rate/age/degree/range-width
axes) and C5 (dependency-DAG generator + cascade driver) tasks added on top
of the storm-core lane (`tgms/eval/storm.py`, `scripts/
bench_correction_storm.py`, `tests/test_storm.py`), and (b) the C6
pre-registration template (design memo §8) with every number left blank
for the coordinator's freeze — this file does not bind anything by itself;
the freeze binds a `campaign.yaml` + `FREEZE_BINDING` pair per §8, not yet
created.

Design of record: `docs/design/CORRECTION_STORM_DESIGN_2026-09-13.md`.

## What C2 added

`tgms/eval/storm.py` gained a `Mix` class (`build_mix(name, ...)`), a
`Storm(mix=...)`-compatible callable implementing:

- **`--mix {c1,c2,c3,c4}`** — the append-vs-correction rate axis (99/1,
  95/5, 80/20, and 80/20 with a scheduled burst), a *reweighting* of
  `tgms.eval.corrections.generate()`'s own output, never a reimplementation
  of its taxonomy.
- **`--age {recent,hours,days,deep}`** — the correction-age axis, built
  directly over `corrections.py`'s private `_believed_nodes`/`_version_for`
  (named by the design memo itself as the mechanism to build over, since
  `generate()`'s own outside-window `_interval` cannot reach past one
  `step`). Every age-banded correction is labeled `generator="age_<band>"`
  so it is never confused with one of `corrections.GENERATORS`' own cells.
- **`--degree {low,mid,high}`** / **`--range-width {narrow,mid,wide}`** —
  best-effort accept/reject selectors over the generated pool (falling back
  to the unfiltered pool when nothing matches, rather than starving
  `Storm.run`'s bounded-attempt loop).
- **`--burst-size` / `--burst-after`** — C4's "10,000 historical corrections
  in one interval", bundled as one `Correction` with many ops.
- **`--measure-ttf {sum,end-to-end}`** — `sum` is the storm-core lane's own
  model (Σ per-artifact costs from the one shared oracle pass); `end-to-end`
  additionally, for the `tgms-*` arms only, actually re-runs
  `check_artifact` -> `refresh` over that arm's nominated set as one timed
  wall-clock interval. Both modes produce byte-identical non-timing
  records for the same seed (`tests/test_storm.py::
  test_end_to_end_and_sum_ttf_modes_agree_outside_timing`).
- The SNAP caveat: `Storm.interval_vt` (probed once per store) and
  `BatchResult.age_vt_meaningful` record, per cell, whether this store's
  valid time is a genuine interval or purely event-instant — never silently
  assumed either way.

Every one of these is a CLI flag on `scripts/bench_correction_storm.py` and
is recorded in that script's own manifest `config` block.

## What C5 added

`tgms/eval/storm_dag.py` (new module):

- **`build_dag(registry, store, shape, depth, fanout, seed)`** — registers a
  real dependency DAG (`chain`, `tree`, `diamond`, `layered`, `power-law`;
  depth 1-32, fanout 1-1,000) using the same `"operator"`-kind registration
  idiom `storm.py`/`bench_m5.py` already use, with real `parents` tuples
  naming real, already-registered `ArtifactId`s — no synthetic side graph.
  Each node reads one uid; roughly half of each non-root node's children
  inherit their parent's uid (a real correction changes them too) and half
  draw a different real uid (flagged via the parent edge, but their payload
  does not change on recompute — the "unnecessary invalidation" case).
- **`cascade(registry, store, refreshed, k)`** — the k-hop worklist the
  design memo says belongs in the benchmark, not `tgms/artifact/
  propagate.py` (`parent_recheck` stays one-level, caller-driven, no
  cascade — unmodified, pinned by `tests/test_propagation.py::
  test_walks_one_level_only_no_cascade`): repeatedly calls
  `parent_recheck`, refreshes each flagged candidate, and re-calls with the
  refreshed id. Reports nodes visited, per-hop latency, unnecessary
  invalidations, **false-safe** (computed by an oracle pass over every
  *other* currently-registered artifact after the walk — must be empty for
  a `k` at least as deep as the DAG's real depth from the corrected root;
  `tests/test_storm_dag.py` demonstrates both a `k` that confirms
  quiescence/false-safe=0 and a truncated `k` that honestly reports a
  false-safe it did not reach), and peak RSS.
- `scripts/bench_correction_storm.py --dag-shape/--dag-depth/--dag-fanout
  --dag-cascade-k` runs this as a phase after the correction-storm batch
  loop, over the same store/registry, reported under the manifest's own
  `"dag"` key.

## Deviations from a literal reading of the design memo (all recorded here
per the task's own "report every deviation" instruction; none touch
`tgms/artifact/*`, `tgms/tgir/{check,depscope,footprint,explain}.py`,
`scripts/bench_m5.py`, or `benchmarks/m5-v1/`)

1. **`cascade`'s signature is `cascade(registry, store, refreshed, k)`**,
   not the memo's literal `cascade(registry, refreshed, k)` (§4) — a
   cascade that refreshes candidates needs a live store, the same reason
   `Storm`/`demo_propagation.py` both carry one.
2. **C4's steady-state ratio** (between bursts) is not specified by §2
   beyond "burst of 10,000" — implemented as C3's 80/20, since the burst
   event is C4's distinguishing feature, not a fifth ratio.
3. **The age axis needs a non-`None` query window** to make "outside" mean
   anything (`corrections.py::_outside`), but `Storm`'s own storm-wide
   `Target` is always built with `window=None` (per-artifact windows are
   not visible to a `mix` callable by the frozen extension-point contract).
   The age axis therefore synthesizes its own local recency window (the
   substrate's own last 15% of span) rather than reusing a real registered
   artifact's window.
4. **`build_dag`'s node registrations populate `payload` at generation 0**
   (via the same `ResultStore` write `refresh._publish` uses), unlike
   `storm.py`'s own `_register_operator_artifact`, which leaves it `None`
   and tracks digests externally instead. `cascade`'s false-safe oracle has
   no such external table and reads `record.payload` for *any* registered
   artifact, visited or not — a `None` initial payload made every
   never-yet-refreshed artifact look unconditionally "changed" on its first
   real refresh, a false-safe manufactured by a bookkeeping gap rather than
   a cascade defect. Found and fixed via the `layered` shape's own
   independent-roots case during this task's own testing.
5. **`build_dag` enforces `DEFAULT_MAX_NODES` (4,000)** as a safety valve
   independent of `depth`/`fanout` — `tree`/`layered`/`power-law` at the
   memo's own stated extremes (depth 32, fanout 1,000) would ask for
   astronomically many real registrations. A truncated build reports
   `DagInfo.truncated=True` rather than silently building a smaller graph
   than requested.
6. **`--dag-shape`'s "depth"/"fanout" do not mean the same thing across
   shapes**: `chain` ignores fanout (always 1); `diamond` can overshoot the
   requested depth by one lap; `power-law` reinterprets `depth * fanout` as
   a population size, not a layer count (there are no layers in a
   preferential-attachment DAG). Each is documented at its own
   `_plan_*` builder in `storm_dag.py`.
7. **`storm_campaign.slurm`'s array dimension is (store x mix x age x
   n_artifacts x seed), not `(arm x store x seed)`** as a literal reading
   of §7 might suggest — every configured arm is scored in the same task,
   in the same one-pass oracle run `storm.py`'s own module docstring
   already commits to; splitting arms across array tasks would just repeat
   that identical oracle computation `len(arms)` times.
8. **The campaign scripts are named `storm_campaign.slurm`/
   `storm_campaign_merge.py`**, per this task's own file-ownership list —
   the design memo's §7 names a hypothetical `scripts/storm_job.slurm`
   instead; not created, to avoid two competing Slurm entry points.

## C6 pre-registration template (numbers left blank for the freeze)

**Predictions, with reasoning, recorded before any run** (copied verbatim
from the design memo §8 — restating them here is not a new prediction, it
is the freeze coordinator's own checklist made local to this directory):

| prediction | reasoning | measured |
|---|---|---|
| row-touch false-fresh stays ≳ 40% and is 100% in `new-identity` | CE-1/2/3 are structural | _(blank)_ |
| entity-touch ≈ row-touch on false-fresh; window-overlap sound but near-⊤ precision | overlap is a superset of the `vt` arm | _(blank)_ |
| avoided recomputation (decision count) stays ≳ 95% at C1/C2, degrades at C3, lowest at C4 | burst concentrates corrections in one region | _(blank)_ |
| avoided recomputation (wall clock) < the decision count | check cost is O(prefix), paid regardless | _(blank)_ |
| TTF speedup vs global recompute grows with N and shrinks with log size | numerator scales with N, denominator with check cost | _(blank)_ |
| R-18 trips at 10^5 and at C4 >= 10^4; not at C1-C3 <= 10^4 | §3 arithmetic | _(blank)_ |

**Per-arm avoided-recomputation and TTF ratios** (the C6 freeze fills these
in per store x mix x age x N cell; `scripts/storm_campaign_merge.py`'s
`per_cell` table is the source):

| arm | avoided_recompute_decision | avoided_recompute_wall | ttf_p50_ms | ttf_p95_ms |
|---|---:|---:|---:|---:|
| global-recompute | _(blank)_ | _(blank)_ | _(blank)_ | _(blank)_ |
| entity-touch | _(blank)_ | _(blank)_ | _(blank)_ | _(blank)_ |
| window-overlap | _(blank)_ | _(blank)_ | _(blank)_ | _(blank)_ |
| row-touch | _(blank)_ | _(blank)_ | _(blank)_ | _(blank)_ |
| tgms-L0 | _(blank)_ | _(blank)_ | _(blank)_ | _(blank)_ |
| tgms-L1 | _(blank)_ | _(blank)_ | _(blank)_ | _(blank)_ |

**Falsification criteria** (design memo §8, unchanged): (a) any false-fresh
anywhere; (b) any false-safe; (c) wall-clock avoided recomputation <= 0 at
some (N, log size); (d) TTF speedup < 1x on the largest store; (e) a coarse
baseline matching TGMS on false-fresh *and* cost at some cell.

## DAG phase — run of record (addendum-1's grid, unchanged from the C6
freeze; 40/40 cells: 5 shapes × depth {4, 16} × fanout {2, 10} × seed {0, 1})

**Gate result: G-S2_false_safe FAILED.** 20/40 cells (every seed=0 cell,
independent of shape/depth/fanout) report `false_safe_count=3`; all 20
seed=1 cells report 0. Falsifier (b) ("any false-safe in the dag phase at
`cascade_k == depth`") **triggered** — `cascade_k == depth` holds in all
40/40 cells. `tgms-L0`/`tgms-L1` false-fresh is 0/40 in every cell (the
oracle-falsifier check: every artifact the oracle found changed was also
found by both TGMS arms, digest-compared correctly, with no exceptions).

**Mechanism (confirmed, not a harness bug).** Every seed=0 cell assigns the
DAG root the identical uid (`__inj000780081`) and reports the identical
false-safe set (`storm-000026`, `storm-000051`, `storm-000063`) regardless
of DAG shape — these are artifacts from the incidental default storm-batch
population each DAG task registers before building its DAG (`--n-artifacts
100 --batches 20`, no `--mix`/`--age`), not DAG nodes, and they depend on
the corrected uid only through their own query footprint, never through a
declared `parents` edge. `cascade`'s false-safe oracle is registry-wide by
design (§4/§8-G-S2) and is therefore correct to flag them; a k-hop walk
that only follows declared edges structurally cannot reach an artifact
whose dependency is footprint-only, at any `k`.

**Per (shape, depth, fanout), seed 0 / seed 1:**

| shape/depth/fanout | nodes_visited | false_safe | quiescent | med hop latency (ms) | cascade wall_s | tgms-L0 ttf_p50 (ms) | tgms-L1 ttf_p50 (ms) |
|---|---:|---:|---|---:|---:|---:|---:|
| chain/4/2 | 3 / 3 | 3 / 0 | T / T | 120.3 / 134.8 | 13.6 / 13.4 | 14481.8 / 15009.8 | 14461.8 / 14983.8 |
| chain/4/10 | 3 / 3 | 3 / 0 | T / T | 134.0 / 135.9 | 14.3 / 13.8 | 15082.9 / 15405.1 | 15087.7 / 15400.4 |
| chain/16/2 | 15 / 15 | 3 / 0 | T / T | 134.8 / 135.2 | 17.4 / 17.2 | 14857.0 / 15567.5 | 14872.0 / 15563.4 |
| chain/16/10 | 15 / 15 | 3 / 0 | T / T | 149.6 / 150.2 | 19.6 / 19.1 | 17021.2 / 17477.7 | 17024.5 / 17479.4 |
| tree/4/2 | 14 / 14 | 3 / 0 | T / T | 394.2 / 398.6 | 17.0 / 16.5 | 14725.4 / 15076.0 | 14742.7 / 15091.8 |
| tree/4/10 | 1110 / 1110 | 3 / 0 | T / T | 7364.9 / 7380.4 | 310.9 / 311.7 | 15201.7 / 15657.7 | 15224.7 / 15663.7 |
| tree/16/2† | 3999 / 3999 | 3 / 0 | T / T | 6901.9 / 5870.7 | 1163.2 / 995.6 | 16395.4 / 15154.2 | 16394.1 / 15227.7 |
| tree/16/10† | 3999 / 3999 | 3 / 0 | T / T | 12343.2 / 12294.5 | 1009.2 / 1002.7 | 14737.5 / 15202.2 | 14737.4 / 15196.5 |
| diamond/4/2 | 6 / 6 | 3 / 0 | F / F | 201.6 / 206.6 | 15.3 / 14.9 | 15337.7 / 15730.8 | 15340.2 / 15716.4 |
| diamond/4/10 | 22 / 22 | 3 / 0 | F / F | 745.2 / 754.4 | 19.7 / 19.1 | 15203.7 / 15626.3 | 15205.6 / 15622.9 |
| diamond/16/2 | 24 / 24 | 3 / 0 | F / F | 206.7 / 211.0 | 20.2 / 20.2 | 15417.5 / 15827.1 | 15416.3 / 15811.1 |
| diamond/16/10 | 88 / 88 | 3 / 0 | F / F | 787.9 / 717.0 | 39.8 / 36.0 | 16471.2 / 15205.5 | 16514.7 / 15197.0 |
| layered/4/2 | 6 / 6 | 3 / 0 | T / T | 263.7 / 272.2 | 15.0 / 15.0 | 14744.4 / 15590.6 | 14739.7 / 15597.6 |
| layered/4/10 | 30 / 30 | 3 / 0 | T / T | 1369.7 / 1311.7 | 24.5 / 22.9 | 15258.5 / 15150.4 | 15248.4 / 15158.6 |
| layered/16/2 | 30 / 30 | 3 / 0 | T / T | 264.2 / 267.3 | 21.5 / 21.3 | 14806.0 / 15304.2 | 14830.2 / 15339.2 |
| layered/16/10 | 150 / 150 | 3 / 0 | T / T | 1340.9 / 1378.6 | 56.3 / 57.0 | 15143.1 / 15775.2 | 15139.6 / 15798.4 |
| power-law/4/2 | 7 / 7 | 3 / 0 | T / T | 418.4 / 132.2 | 15.9 / 14.6 | 15469.3 / 15113.7 | 15481.3 / 15089.4 |
| power-law/4/10 | 39 / 39 | 3 / 0 | T / T | 391.7 / 422.3 | 23.8 / 24.3 | 14900.1 / 15853.2 | 14912.8 / 15831.7 |
| power-law/16/2 | 31 / 31 | 3 / 0 | T / T | 1114.2 / 1847.6 | 22.3 / 21.1 | 15378.6 / 15175.8 | 15366.0 / 15182.1 |
| power-law/16/10 | 159 / 159 | 3 / 0 | T / T | 6133.9 / 6933.9 | 54.8 / 56.2 | 14651.9 / 15585.4 | 14642.3 / 15598.1 |

†`truncated=True` (`DEFAULT_MAX_NODES=4000` safety valve hit). tgms-L0/L1
TTF columns come from each task's own incidental storm-batch population
(identical parameters for every DAG cell) — noise across configs, not a
designed sweep, reported since it is the only per-arm TTF these tasks
carry. Seeds agree perfectly on `nodes_visited`/`quiescent` in every
combo; the only disagreement is `false_safe_count`, fully explained above.

**Records**: `storm-campaign-dag-2026-09.json` (merged, provenance job ids
211321/211503/211504), `storm-campaign-dag-2026-09-rows.jsonl` (per-task,
verbatim), `dag-records-40-tasks.tar.gz` (raw per-task record directories).
`scripts/check_result_manifest.py` passes. **Not yet committed** — held in
the worktree pending the coordinator's scoring of G-S2/falsifier (b).

**Addendum-2, mechanism.** `--dag-seed-from-affected` (additive to
`scripts/bench_correction_storm.py` / `tgms/eval/storm_dag.py`, default
off, v1 walk byte-identical when absent) seeds the cascade from every
artifact `tgms.artifact.lookup.affected()` finds for the DAG-root
correction batch — the same footprint pre-filter the `tgms-*`/
`entity-touch`/`window-overlap` storm arms already use — not just the
declared-edge root, then walks each seed's own `parents` edges for the
same `k`; the registry-wide false-safe oracle is unchanged. Full terms in
`campaign.yaml`'s `addendum_2` block.

## DAG phase v2 — run of record (addendum-2, same 40-cell grid, job 211555;
commit `8962b78`, the same anchor v1 ran — this run isolates
`--dag-seed-from-affected` and does not include the D-161 scope-derivation
rollout)

**Gate result: G-S2_false_safe PASSED — 0/40 cells, registry-wide and
DAG-node both.** Every cell that failed in v1 (all 20 seed=0 cells) now
shows `false_safe_count=0`; `tgms-L0`/`tgms-L1` false-fresh is 0/40 across
every cell, same as v1.

**Measured vs. addendum-2's pre-registered prediction — not the predicted
magnitude, but the predicted qualitative outcome.** The prediction read
"nodes_visited unchanged in seed-1 cells and +3 in seed-0 cells." Measured:
`nodes_visited` increases in **every** cell, by an amount that is constant
per seed, not per cell or shape — **exactly +61 in all 20 seed=0 cells,
exactly +62 in all 20 seed=1 cells**, with zero variance. This is because
`affected()` is a conservative footprint pre-filter, not the exact
false-safe set: at this run's anchor commit (`8962b78`, pre-D-161), most of
the incidental storm-population artifacts each DAG task registers before
building its DAG carry the coarse `(TOP_TERM,)` scope (Addendum-4's own
finding), so *any* correction's `affected()` answer sweeps in most of that
population as seeds — not just the handful that actually turn out to have
changed. The DAG's own cascade discovery is a small, shape-dependent
fraction of that count; the +61/+62 constant is the incidental population's
own TOP_TERM survivor count for that seed, unrelated to shape/depth/fanout
(the same underlying storm population, same seed, same default `mix=None`
call, precedes every DAG build regardless of shape).

**Quiescence**: `diamond`'s four (depth, fanout) combinations were
`quiescent=False` in v1 (the frontier was still non-empty when `k` ran
out) and are `quiescent=True` in v2 for all of them, at both seeds — the
larger seed set reaches a fixed point within the same `k`.

**Per-cell table** (v1 → v2; all `v2_fs_names` are empty — no false-safe
artifacts of any kind in v2):

| shape/depth/fanout/seed | v1 false_safe | v2 false_safe | v1 nodes_visited | v2 nodes_visited | Δ | v2 seeds | L0/L1 false_fresh (v2) | quiescent v1→v2 | wall_s v1→v2 |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|
| chain/4/2/s0 | 3 | 0 | 3 | 64 | 61 | 65 | 0/0 | T→T | 13.6→16.1 |
| chain/4/2/s1 | 0 | 0 | 3 | 65 | 62 | 66 | 0/0 | T→T | 13.4→15.0 |
| chain/4/10/s0 | 3 | 0 | 3 | 64 | 61 | 65 | 0/0 | T→T | 14.3→16.1 |
| chain/4/10/s1 | 0 | 0 | 3 | 65 | 62 | 66 | 0/0 | T→T | 13.8→15.0 |
| chain/16/2/s0 | 3 | 0 | 15 | 76 | 61 | 77 | 0/0 | T→T | 17.4→19.6 |
| chain/16/2/s1 | 0 | 0 | 15 | 77 | 62 | 78 | 0/0 | T→T | 17.2→18.3 |
| chain/16/10/s0 | 3 | 0 | 15 | 76 | 61 | 77 | 0/0 | T→T | 19.6→19.7 |
| chain/16/10/s1 | 0 | 0 | 15 | 77 | 62 | 78 | 0/0 | T→T | 19.1→19.6 |
| tree/4/2/s0 | 3 | 0 | 14 | 75 | 61 | 62 | 0/0 | T→T | 17.0→19.2 |
| tree/4/2/s1 | 0 | 0 | 14 | 76 | 62 | 68 | 0/0 | T→T | 16.5→19.0 |
| tree/4/10/s0 | 3 | 0 | 1110 | 1171 | 61 | 167 | 0/0 | T→T | 310.9→351.7 |
| tree/4/10/s1 | 0 | 0 | 1110 | 1172 | 62 | 190 | 0/0 | T→T | 311.7→357.2 |
| tree/16/2/s0† | 3 | 0 | 3999 | 4060 | 61 | 62 | 0/0 | T→T | 1163.2→1194.7 |
| tree/16/2/s1† | 0 | 0 | 3999 | 4061 | 62 | 71 | 0/0 | T→T | 995.5→1198.7 |
| tree/16/10/s0† | 3 | 0 | 3999 | 4060 | 61 | 290 | 0/0 | T→T | 1009.2→1200.6 |
| tree/16/10/s1† | 0 | 0 | 3999 | 4061 | 62 | 380 | 0/0 | T→T | 1002.7→1199.1 |
| diamond/4/2/s0 | 3 | 0 | 6 | 67 | 61 | 68 | 0/0 | **F→T** | 15.3→17.3 |
| diamond/4/2/s1 | 0 | 0 | 6 | 68 | 62 | 69 | 0/0 | **F→T** | 14.9→16.5 |
| diamond/4/10/s0 | 3 | 0 | 22 | 83 | 61 | 84 | 0/0 | **F→T** | 19.7→22.1 |
| diamond/4/10/s1 | 0 | 0 | 22 | 84 | 62 | 85 | 0/0 | **F→T** | 19.1→21.2 |
| diamond/16/2/s0 | 3 | 0 | 24 | 85 | 61 | 86 | 0/0 | **F→T** | 20.2→22.6 |
| diamond/16/2/s1 | 0 | 0 | 24 | 86 | 62 | 87 | 0/0 | **F→T** | 20.1→22.2 |
| diamond/16/10/s0 | 3 | 0 | 88 | 149 | 61 | 150 | 0/0 | **F→T** | 39.8→41.5 |
| diamond/16/10/s1 | 0 | 0 | 88 | 150 | 62 | 151 | 0/0 | **F→T** | 35.9→42.3 |
| layered/4/2/s0 | 3 | 0 | 6 | 67 | 61 | 62 | 0/0 | T→T | 15.0→17.3 |
| layered/4/2/s1 | 0 | 0 | 6 | 68 | 62 | 65 | 0/0 | T→T | 15.0→16.8 |
| layered/4/10/s0 | 3 | 0 | 30 | 91 | 61 | 62 | 0/0 | T→T | 24.5→27.1 |
| layered/4/10/s1 | 0 | 0 | 30 | 92 | 62 | 64 | 0/0 | T→T | 22.9→26.4 |
| layered/16/2/s0 | 3 | 0 | 30 | 91 | 61 | 62 | 0/0 | T→T | 21.5→24.8 |
| layered/16/2/s1 | 0 | 0 | 30 | 92 | 62 | 65 | 0/0 | T→T | 21.3→24.2 |
| layered/16/10/s0 | 3 | 0 | 150 | 211 | 61 | 62 | 0/0 | T→T | 56.3→63.8 |
| layered/16/10/s1 | 0 | 0 | 150 | 212 | 62 | 64 | 0/0 | T→T | 57.0→63.9 |
| power-law/4/2/s0 | 3 | 0 | 7 | 68 | 61 | 63 | 0/0 | T→T | 15.9→17.6 |
| power-law/4/2/s1 | 0 | 0 | 7 | 69 | 62 | 64 | 0/0 | T→T | 14.6→16.9 |
| power-law/4/10/s0 | 3 | 0 | 39 | 100 | 61 | 75 | 0/0 | T→T | 23.8→27.4 |
| power-law/4/10/s1 | 0 | 0 | 39 | 101 | 62 | 63 | 0/0 | T→T | 24.3→25.5 |
| power-law/16/2/s0 | 3 | 0 | 31 | 92 | 61 | 65 | 0/0 | T→T | 22.3→24.8 |
| power-law/16/2/s1 | 0 | 0 | 31 | 93 | 62 | 66 | 0/0 | T→T | 21.1→24.1 |
| power-law/16/10/s0 | 3 | 0 | 159 | 220 | 61 | 115 | 0/0 | T→T | 54.8→56.2 |
| power-law/16/10/s1 | 0 | 0 | 159 | 221 | 62 | 69 | 0/0 | T→T | 56.2→59.7 |

†`truncated=True` in both v1 and v2 (`DEFAULT_MAX_NODES=4000` safety valve).

**Addendum-2 predictions, measured aggregate (no verdicts):**

| prediction | measured |
|---|---|
| registry-wide false-safe cells (max 0) | **0/40** |
| DAG-node false-safe cells (max 0) | **0/40** |
| seed-1 cells with nodes_visited unchanged | **0/20** (all 20 changed, uniformly by +62) |
| seed-0 cells with nodes_visited +3 | **0/20** (all 20 changed, uniformly by +61, not +3) |
| false-fresh total across all 40 cells (tgms-L0 + tgms-L1) | **0** |

**Records**: `storm-campaign-dag-v2-2026-09.json` (merged, provenance job id
211555 only), `storm-campaign-dag-v2-2026-09-rows.jsonl`,
`dag-v2-records-40-tasks.tar.gz`. `scripts/check_result_manifest.py`
passes. Every one of the 40 underlying records carries
`dag.cascade.seeded_from == "affected"`, `config.addendum_id ==
"storm-v1-addendum-2"`, `config.freeze_sha256 == "ac21f29c…"` — confirmed
directly, all 40. **Not yet committed** — held in the worktree pending the
coordinator's scoring.

## DAG phase v3 — run of record (addendum-3's `dag_phase_v3`, same 40-cell
grid; jobs 211614 + 211700 + 211706; commit `fdd393c`, the D-161-rolled-out
engine — this is the same `--dag-seed-from-affected` mechanism v2 tested,
now run where 13 of 14 read operators derive a real scope instead of 3)

**All six pre-registered predictions PASS, and each matches its predicted
range exactly, not just its floor:**

| prediction | measured |
|---|---|
| registry-wide false-safe cells (max 0) | **0/40** |
| DAG-node false-safe cells (max 0) | **0/40** (trivial — 0 false-safe artifacts of any kind) |
| tgms false-fresh total (predicted 0/40) | **0** (tgms-L0 + tgms-L1, summed over all 40 cells) |
| `nodes_visited` delta vs v1, seed-0 cells (predicted ∈ [3, 20]) | **exactly +15 in all 20 seed-0 cells** |
| `nodes_visited` delta vs v1, seed-1 cells (predicted ∈ [0, 15]) | **exactly +11 in all 20 seed-1 cells** |
| `summary.narrowing_coverage.n_all_top_term` (predicted 0) | **0/40 cells** |

**Reading the deltas against v2's own result.** v2 (pre-rollout, 8962b78)
measured a uniform +61/+62 — most of the incidental storm population was
`TOP_TERM`-scoped, so `affected()` swept in most of it regardless of
shape. Under the rollout (fdd393c), `n_all_top_term=0` in every cell — no
artifact in any of the 40 underlying storm populations falls back to the
coarse scope — so `affected()`'s answer shrinks to a small, still-uniform-
per-seed constant (+15 seed-0, +11 seed-1) that reflects genuinely
narrowed, non-`TOP_TERM` footprint matches rather than a population-wide
sweep. The constant-per-seed (not per-shape) pattern persists for the same
structural reason as v2: the same incidental storm population (same seed,
same default call) precedes every DAG build regardless of shape.

**Quiescence**: as in v2, all four `diamond` (depth, fanout) combinations
are `quiescent=False` in v1 and `quiescent=True` in v3, at both seeds.

**Per-cell table** (v1 → v3):

| shape/depth/fanout/seed | v1 false_safe | v3 false_safe | v1 nodes_visited | v3 nodes_visited | Δ | n_all_top_term | L0/L1 false_fresh | quiescent v1→v3 | wall_s v1→v3 |
|---|---:|---:|---:|---:|---:|---:|---:|---|---:|
| chain/4/2/s0 | 3 | 0 | 3 | 18 | 15 | 0 | 0/0 | T→T | 13.6→15.3 |
| chain/4/2/s1 | 0 | 0 | 3 | 14 | 11 | 0 | 0/0 | T→T | 13.4→14.8 |
| chain/4/10/s0 | 3 | 0 | 3 | 18 | 15 | 0 | 0/0 | T→T | 14.3→15.3 |
| chain/4/10/s1 | 0 | 0 | 3 | 14 | 11 | 0 | 0/0 | T→T | 13.8→14.9 |
| chain/16/2/s0 | 3 | 0 | 15 | 30 | 15 | 0 | 0/0 | T→T | 17.4→17.6 |
| chain/16/2/s1 | 0 | 0 | 15 | 26 | 11 | 0 | 0/0 | T→T | 17.2→19.1 |
| chain/16/10/s0 | 3 | 0 | 15 | 30 | 15 | 0 | 0/0 | T→T | 19.6→19.9 |
| chain/16/10/s1 | 0 | 0 | 15 | 26 | 11 | 0 | 0/0 | T→T | 19.1→19.1 |
| tree/4/2/s0 | 3 | 0 | 14 | 29 | 15 | 0 | 0/0 | T→T | 17.0→19.6 |
| tree/4/2/s1 | 0 | 0 | 14 | 25 | 11 | 0 | 0/0 | T→T | 16.5→19.0 |
| tree/4/10/s0 | 3 | 0 | 1110 | 1125 | 15 | 0 | 0/0 | T→T | 310.9→351.0 |
| tree/4/10/s1 | 0 | 0 | 1110 | 1121 | 11 | 0 | 0/0 | T→T | 311.7→350.7 |
| tree/16/2/s0† | 3 | 0 | 3999 | 4014 | 15 | 0 | 0/0 | T→T | 1163.2→1209.1 |
| tree/16/2/s1† | 0 | 0 | 3999 | 4010 | 11 | 0 | 0/0 | T→T | 995.5→1199.8 |
| tree/16/10/s0† | 3 | 0 | 3999 | 4014 | 15 | 0 | 0/0 | T→T | 1009.2→1198.1 |
| tree/16/10/s1† | 0 | 0 | 3999 | 4010 | 11 | 0 | 0/0 | T→T | 1002.7→1172.4 |
| diamond/4/2/s0 | 3 | 0 | 6 | 21 | 15 | 0 | 0/0 | **F→T** | 15.3→17.0 |
| diamond/4/2/s1 | 0 | 0 | 6 | 17 | 11 | 0 | 0/0 | **F→T** | 14.9→16.5 |
| diamond/4/10/s0 | 3 | 0 | 22 | 37 | 15 | 0 | 0/0 | **F→T** | 19.7→22.3 |
| diamond/4/10/s1 | 0 | 0 | 22 | 33 | 11 | 0 | 0/0 | **F→T** | 19.1→21.5 |
| diamond/16/2/s0 | 3 | 0 | 24 | 39 | 15 | 0 | 0/0 | **F→T** | 20.2→22.8 |
| diamond/16/2/s1 | 0 | 0 | 24 | 35 | 11 | 0 | 0/0 | **F→T** | 20.1→21.9 |
| diamond/16/10/s0 | 3 | 0 | 88 | 103 | 15 | 0 | 0/0 | **F→T** | 39.8→50.0 |
| diamond/16/10/s1 | 0 | 0 | 88 | 99 | 11 | 0 | 0/0 | **F→T** | 35.9→41.5 |
| layered/4/2/s0 | 3 | 0 | 6 | 21 | 15 | 0 | 0/0 | T→T | 15.0→17.3 |
| layered/4/2/s1 | 0 | 0 | 6 | 17 | 11 | 0 | 0/0 | T→T | 15.0→16.8 |
| layered/4/10/s0 | 3 | 0 | 30 | 45 | 15 | 0 | 0/0 | T→T | 24.5→23.7 |
| layered/4/10/s1 | 0 | 0 | 30 | 41 | 11 | 0 | 0/0 | T→T | 22.9→26.6 |
| layered/16/2/s0 | 3 | 0 | 30 | 45 | 15 | 0 | 0/0 | T→T | 21.5→21.4 |
| layered/16/2/s1 | 0 | 0 | 30 | 41 | 11 | 0 | 0/0 | T→T | 21.3→23.1 |
| layered/16/10/s0 | 3 | 0 | 150 | 165 | 15 | 0 | 0/0 | T→T | 56.3→55.9 |
| layered/16/10/s1 | 0 | 0 | 150 | 161 | 11 | 0 | 0/0 | T→T | 57.0→55.0 |
| power-law/4/2/s0 | 3 | 0 | 7 | 22 | 15 | 0 | 0/0 | T→T | 15.9→15.6 |
| power-law/4/2/s1 | 0 | 0 | 7 | 18 | 11 | 0 | 0/0 | T→T | 14.6→17.0 |
| power-law/4/10/s0 | 3 | 0 | 39 | 54 | 15 | 0 | 0/0 | T→T | 23.8→27.1 |
| power-law/4/10/s1 | 0 | 0 | 39 | 50 | 11 | 0 | 0/0 | T→T | 24.3→23.9 |
| power-law/16/2/s0 | 3 | 0 | 31 | 46 | 15 | 0 | 0/0 | T→T | 22.3→21.7 |
| power-law/16/2/s1 | 0 | 0 | 31 | 42 | 11 | 0 | 0/0 | T→T | 21.1→20.3 |
| power-law/16/10/s0 | 3 | 0 | 159 | 174 | 15 | 0 | 0/0 | T→T | 54.8→60.0 |
| power-law/16/10/s1 | 0 | 0 | 159 | 170 | 11 | 0 | 0/0 | T→T | 56.2→55.9 |

†`truncated=True` in both v1 and v3 (`DEFAULT_MAX_NODES=4000` safety valve).

**Records**: `storm-campaign-dag-v3-2026-09.json` (merged, provenance job
ids 211614 + 211700 + 211706), `storm-campaign-dag-v3-2026-09-rows.jsonl`,
`dag-v3-records-40-tasks.tar.gz`. `scripts/check_result_manifest.py`
passes. **Not yet committed** — held in the worktree pending the
coordinator's scoring.

## Regenerating (once the C6 freeze creates `campaign.yaml`/`FREEZE_BINDING`)

```sh
# 1. Submit (iTiger; adjust TGMS_REPO/TGMS_STAGE/grid env vars via --export):
sbatch scripts/storm_campaign.slurm
# 2. Poll: sacct -j <jobid> --format=JobID,State,Elapsed,ExitCode -X
# 3. scp records/ + node_meta/ down, then merge:
python scripts/storm_campaign_merge.py \
    --records-dir <pulled>/records --node-meta-dir <pulled>/node_meta \
    --stores synth-iv-60k,collegemsg --mixes c1,c2,c3,c4 \
    --ages none,recent,hours,days,deep --n-artifacts-list 1000 \
    --n-seeds 5 --base-seed 0 --commit <sha> --array-job-id <jobid> \
    --out benchmarks/storm-v1/storm-campaign-<date>.json
# 4. Validate:
python scripts/check_result_manifest.py benchmarks/storm-v1/storm-campaign-<date>.json
```

The C6 pre-registration table above is still `_(blank)_` by design — the
main correction-storm cell grid (addendum-1, 36 cells) is **12/36 cells
complete; 24 cells pending quota headroom; not yet scored** (a cluster
disk-quota incident on 2026-09-14 — see `SUBMISSION_NOTE.txt` on iTiger —
has failed the same 24 cells on four consecutive submission attempts;
resubmission is parked until the PI frees space). The R-18 probe
(addendum-1) completed (job 211320, 5/5 batches, not wall-capped) but its
own results are not yet written up in this README. The DAG phase
(v1/v2/v3, all three addenda) has completed and its results are reported
above, with G-S2/falsifier (b) stated as measured outcomes for the
coordinator to score.
