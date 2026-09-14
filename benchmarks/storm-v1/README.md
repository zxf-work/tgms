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

No experiments were run to produce this file — every number above is
`_(blank)_` by design; this is infrastructure only.
