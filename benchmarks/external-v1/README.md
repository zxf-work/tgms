# external-v1 — external-baseline campaign (Neo4j recompute / differential-dataflow IVM / TGMS same-host control)

**Date assembled: 2026-10-08 (lane C1-records). Status: landed.** All three
campaign records in this directory were assembled on xzgpu from real,
completed grids (`scripts/external_record.py` at public main `79e79c7b`,
run against the result trees under `/mnt/project/xzhang/tgms/external-v1/
{export,t1,n1,d1}` — read-only inputs, not touched by this assembly).

| file | campaign | cells | sha256 |
|---|---|---|---|
| `neo4j-recompute-2026-10-07.json` + `-rows.jsonl` | Neo4j 5.26 full recompute | 43 | see `SHA256SUMS.txt` |
| `ivm-differential-2026-10-07.json` + `-rows.jsonl` | differential-dataflow IVM | 43 (+1 withheld-only) | see `SHA256SUMS.txt` |
| `tgms-control-2026-10-05.json` + `-rows.jsonl` | TGMS same-host control (lane T1) | 19 | see `SHA256SUMS.txt` |

All three validate against `benchmarks/schema/result_manifest.schema.json`
(`scripts/check_result_manifest.py benchmarks/external-v1/<file>.json`).

## What was measured

Three configurations, all run against the same 43-cell export of the
`storm-v2` correction-storm workload (12 seed-0 main-grid cells × 2 stores
+ the N=10,000 probe + 6 new age-banded cells; three-seed collegemsg/
synth-iv-60k variants bring the export to 43 total). The export bundle and
all three configurations' result trees live under
`/mnt/project/xzhang/tgms/external-v1/` on xzgpu and are not copied into
this repo (only their assembled summaries are).

1. **Neo4j 5.26 full recompute** (`neo4j-recompute-*`) — every registered
   artifact's query re-run after each burst, on Neo4j 5.26.0 + APOC
   5.26.0-core + Temurin 21.0.12 (JDK) + the official Java driver 5.28.6.
   Lane N1.
2. **differential-dataflow incremental view maintenance** (`ivm-differential-*`)
   — one maintained view per operator family (`ivm-dd`, single worker),
   built from public main `79e79c7b`. Binary sha256
   `3e5a890bdfdac0ad056ce0e02da02de8e62e547917f440eb880578b995e83f31`
   (`cargo_lock_sha256` `505819c28e17948e80aa48f5e502fe95bde8aa25cc0d40dada009770a38d3d9e`;
   `differential_dataflow` 0.25.1, `timely` 0.31.0). Lane D1.
3. **TGMS same-host control** (`tgms-control-*`) — the same
   `bench_correction_storm.py` harness the committed `storm-v2` grid used,
   re-run on xzgpu so a reader never has to cross hosts to get a TGMS
   reference number for a cell measured on xzgpu. Harness built at
   `fdd393c91c1199f7cfe03aba53ed1733f43111b0-dirty` (every T1 cell's own
   `git_commit` field, consistent across all 19). Lane T1.

**Host** (all three): xzgpu — 93 GB RAM, 40 (logical) CPUs, Linux
5.4.0-216-generic, verified from every campaign's own `machine` field and
cross-checked against the per-cell `host-snapshot-{before,after}.txt`
`uptime`/`free` captures lanes N1/D1/T1 each took.

**Host protocol**: the Memgraph container was stopped for each timed
window and restarted after (no co-tenant store running during a timed
cell); no `nice`; a host snapshot (`uptime`, `free`, top processes) was
taken immediately before and after every cell, independent of the
configuration's own self-reported load. Lane D1's grid waited out a
co-tenant job (2026-10-06T12:39Z → 2026-10-07T~14:00Z) and ran on a quiet
box starting 2026-10-07T14:23:19Z — confirmed directly: `d1/.../
host-snapshot-before.txt`'s first cell timestamp is
`2026-10-07T14:23:19Z` with `load average: 0.13`.

## Reading notes (verbatim meaning, restated)

- **A1** — the three external configurations were measured on xzgpu; the
  *committed* TGMS numbers lane C1's assembler reads for its EXT1/EXT2
  ratios (`benchmarks/storm-v1/storm-v2-main-grid-2026-09-15.json` +
  probe) are from the iTiger cluster, a different host. The
  `tgms-control` record above is the same-host control reported beside
  them so a reader is never forced to compare an xzgpu number to an
  iTiger number without also having an xzgpu-vs-xzgpu number on hand.
- **A2** — six age cells were added beyond the committed grid: 2 stores
  (`collegemsg`, `synth-iv-60k`) × `c3` × `{recent, hours, days}` × seed
  0. Each one's `digests.json` records its own `equality_level` literally
  as `"new cell (no committed digest)"` (abbreviated `new (A2)` in the
  table below), with the note: *"extra cell (Addendum-A2-style); no
  committed grid row, so there is no L1/L2 reference to check against —
  this export's own digest becomes the reference for the T1 control and
  both external configurations, per the memo's own A2/T1 ruling."*
- **A3** — per-cell equality level is stated plainly in the table below;
  L1 is never claimed without the digest match (`l1_eventlog_sha_match`
  in `t1-equality.json`, byte-identical final event log) — all 19 T1
  cells are L1, exactly as measured, none asserted from a weaker check.
- **A11** (proposed, applied at scoring) — on xzgpu the reference for
  every cell is the same-host control T1 on the identical exported
  bundle, not the committed iTiger-cluster row the assembler's own
  EXT1/EXT2 ratio arithmetic reads. **P-EXT1(a)** ("the committed internal
  baseline is representative") is scored only on the 22 synth cells that
  reproduce the committed campaign (L2/L2-partial); the 18 collegemsg
  main-grid cells are not tied to the cluster records — their own
  `digests.json` explains why: *"the committed per-batch detail file for
  this cell is not in the repo (main-grid cells' per-batch rows lived
  only on the now-cleared iTiger checkout); L2 here is log_bytes-per-batch
  + n_registered + narrowing_coverage.per_template_counts only"* — which
  is why all 18 collegemsg main-grid cells show `FAIL` in the table
  below, not a weaker-but-real L2. Applying the A11 substitution itself
  (scoring EXT1/EXT2 against `tgms-control` instead of the committed
  cluster rows) is the paper-macro generator's job (lane W2ad), not this
  assembler's — `scripts/external_record.py`'s own ratios are built and
  tested against the committed cluster rows only, by design (see
  "Macro-stub landing" below).

## Per-cell equality level (43 export cells)

"export equality" is each cell's own `digests.json` `equality_level`
(shared by both external configurations, since both check against the
same export bundle). "T1 equality" is lane T1's `t1-equality.json`
`l1_eventlog_sha_match`-derived level, for the 19 cells T1 measured.

| cell_id | export equality | T1 equality |
|---|---|---|
| `collegemsg-c1-deep-n1000-s0` | FAIL | L1 |
| `collegemsg-c1-deep-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c1-deep-n1000-s2` | FAIL | -- (not in T1) |
| `collegemsg-c1-none-n1000-s0` | FAIL | L1 |
| `collegemsg-c1-none-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c1-none-n1000-s2` | FAIL | -- (not in T1) |
| `collegemsg-c3-days-n1000-s0` | new (A2) | L1 |
| `collegemsg-c3-deep-n1000-s0` | FAIL | L1 |
| `collegemsg-c3-deep-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c3-deep-n1000-s2` | FAIL | -- (not in T1) |
| `collegemsg-c3-hours-n1000-s0` | new (A2) | L1 |
| `collegemsg-c3-none-n1000-s0` | FAIL | L1 |
| `collegemsg-c3-none-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c3-none-n1000-s2` | FAIL | -- (not in T1) |
| `collegemsg-c3-recent-n1000-s0` | new (A2) | L1 |
| `collegemsg-c4-deep-n1000-s0` | FAIL | L1 |
| `collegemsg-c4-deep-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c4-deep-n1000-s2` | FAIL | -- (not in T1) |
| `collegemsg-c4-none-n1000-s0` | FAIL | L1 |
| `collegemsg-c4-none-n1000-s1` | FAIL | -- (not in T1) |
| `collegemsg-c4-none-n1000-s2` | FAIL | -- (not in T1) |
| `synth-iv-60k-c1-deep-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c1-deep-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c1-deep-n1000-s2` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c1-none-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c1-none-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c1-none-n1000-s2` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c1-none-n10000-s0` | L2 | L1 |
| `synth-iv-60k-c3-days-n1000-s0` | new (A2) | L1 |
| `synth-iv-60k-c3-deep-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c3-deep-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c3-deep-n1000-s2` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c3-hours-n1000-s0` | new (A2) | L1 |
| `synth-iv-60k-c3-none-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c3-none-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c3-none-n1000-s2` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c3-recent-n1000-s0` | new (A2) | L1 |
| `synth-iv-60k-c4-deep-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c4-deep-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c4-deep-n1000-s2` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c4-none-n1000-s0` | L2-partial | L1 |
| `synth-iv-60k-c4-none-n1000-s1` | L2-partial | -- (not in T1) |
| `synth-iv-60k-c4-none-n1000-s2` | L2-partial | -- (not in T1) |

## Oracle-agreement summary

Independently reconstructed per cell by `scripts/external_check.py` from
each export bundle's own `oracle.jsonl` (never a re-print of either
configuration's own self-reported `gates.oracle_agreement`).

| campaign | cells | agree | disagree | not_answered | oracle_refused | n_compared |
|---|---|---|---|---|---|---|
| neo4j-recompute | 43 | 857,469 | **195** | 0 | 0 | 857,664 |
| ivm-differential | 43 | 857,664 | 0 | 0 | 0 | 857,664 |

`ivm-differential`: 0/43 cells disagree (`d_pass: true` in the record).

`neo4j-recompute`: **10/43 cells disagree**, not the 2 the pre-registration
memo's text names. All 195 disagreements trace to one artifact,
`storm-000911`, in the `synth-iv-60k` store (seed 0 only — the seed-1/
seed-2 `synth-iv-60k` cells fully agree, per `n1/PROGRESS.log`:
`synth-iv-60k-c4-deep-n1000-s1 ... oracle=18543/18543`,
`...-s2 ... oracle=18963/18963`). It disagrees at every burst/epoch of
every seed-0 `synth-iv-60k` cell (9 cells at `n1000`, 1 at the `n10000`
probe) — i.e. the same persistent single-artifact mismatch recurs across
cells sharing that store+seed, not an independent disagreement per cell:

| cell_id | agree | n_compared | disagree |
|---|---|---|---|
| `synth-iv-60k-c1-deep-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c1-none-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c1-none-n10000-s0` (probe) | 53,526 | 53,532 | 6 |
| `synth-iv-60k-c3-days-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c3-deep-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c3-hours-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c3-none-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c3-recent-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c4-deep-n1000-s0` | 18,543 | 18,564 | 21 |
| `synth-iv-60k-c4-none-n1000-s0` | 18,543 | 18,564 | 21 |

Per `n1/synth-iv-60k-c1-none-n1000-s0/check.json` and
`n1/synth-iv-60k-c1-none-n10000-s0/check.json` (the probe), the
disagreeing artifact is `storm-000911` at every epoch (epochs 0-20 for
the n1000 cells, 0-5 for the probe) — one artifact, compared
cross-cell/cross-epoch to confirm the identical name recurs: `disagree:
['storm-000911']` at every single burst in all 10 cells above, never a
different name.

## The withheld-correction cell

`synth-iv-60k-c4-deep-n1000-s0-withheld` (not one of the 43 scored
cells): the `ivm-dd` dataflow configuration's `F-epoch` feeder reports
`false_fresh_count = 39` at the probe point (`probe_reports_complete_through_10:
true`); the `F-watermark` feeder's probe never completes through batch 10
(`probe_reports_complete_through_10: false`, recorded as
`ivm_f_watermark_unanswerable: true`). TGMS's own `false_fresh`/
`stale_marked` at R10 is 0 by contract (the harness never serves a stale
answer as fresh — a property of the maintenance protocol, not a
measurement this check performs) — but that number was **not** supplied
to this check run: `d1/synth-iv-60k-c4-deep-n1000-s0-withheld/
check-withheld.json`'s own `withheld.tgms_false_fresh` is `null`, with
the note *"TGMS's own false_fresh/stale_marked at R10 ... was not
supplied to this run ... left null rather than assumed 0"*. This README
states the 0-by-contract figure as the harness's design property, not as
a number read from `check-withheld.json` — that file's own `tgms_false_fresh`
row is the one to cite for "measured, not asserted."

## Estimates vs. actuals

| campaign | memo estimate | measured | source |
|---|---|---|---|
| Neo4j recompute (N1) | 40-160 h | **18.2 h** (65,497 s summed `wall=` across 43 `n1/PROGRESS.log` lines) | `n1/PROGRESS.log` |
| differential-dataflow (D1) | ~40 min | **22.5 min** (1,351 s summed `wall=` across 43 cells + the withheld cell, `d1/PROGRESS.log`) | `d1/PROGRESS.log` |
| TGMS same-host control (T1) | ~51 h | **51.68 h** (186,065 s summed `config.wall_s` across the 19 `tgms-control` cells) | `tgms-control-2026-10-05.json` |

Note: this lane's own assignment text states the Neo4j figure as "≈16 h
incl. probe" and the dataflow figure as "≈40 min" — the measured sums
above (18.2 h and 22.5 min respectively) are what `PROGRESS.log` actually
records; the assignment text's figures are given as task-brief
approximations, not restated here as the measured values. The T1 figure
matches the brief's "~51 h" closely.

## Known contradictions

- **The dataflow CLI hard-codes `workers=1`.** Every `d1/*/result.json`'s
  `config.workers` is `1` (confirmed on the sampled cell above); the
  memo's planned 8-worker rerun was never run — there is no 8-worker
  result tree anywhere under `/mnt/project/xzhang/tgms/external-v1/`.
- **The Neo4j oracle-loading fix and the timed grid's start, reconciled
  from the host log and file hashes (coordinator check, 2026-10-08).**
  `n1/HOST-N1.log` records a first launch at `2026-10-05T16:36:51Z` whose
  first cell was stopped at `16:38:18Z` ("STOPPING grid: load_oracle bug
  found in runner.py"); the fixed `runner.py` was written to the runner
  copy at `16:42:25Z` (its sha256 equals the file at commit `4e1d539b`,
  which differs from the file at `b7674fd6`); the grid was relaunched at
  `16:42:33Z` and the first kept cell ran `16:42:35Z`–`16:45:43Z`
  (`result.json` mtime `16:45:42Z`). The git commit of the fix
  (`16:44:23Z`) postdates the relaunch because the lane committed after
  copying; every kept timed cell ran on the fixed runner. The discarded
  first attempt's output was removed by the lane before the rerun.
- **`ivm_dd::record::oracle_agreement`'s own bookkeeping never reads
  `refused`** (documented in `scripts/external_check.py`'s own module
  docstring as a known latent bug in that crate, not patched by this
  lane — out of scope; `external_check.py` works around it by excluding
  refused names itself).
- **`versions.rustc` is recorded as `"unknown"`** in every `d1/*/result.json`
  (confirmed on the sampled cell), not `"1.97.1"` — the toolchain
  currently installed on xzgpu (`~/.cargo/bin/rustc --version`) reports
  `rustc 1.97.1 (8bab26f4f 2026-07-14)`, consistent with but not
  confirmed by the recorded build metadata, which has a version-capture
  gap for this field. This README states "rustc 1.97.1" as the current
  toolchain on the build host, not as a figure read from the binary's
  own recorded `versions.rustc`.
- **`HOST-N1.log`'s own pre-flight check reports `openjdk version
  "11.0.27"`**, while every Neo4j cell's own `result.json.config.jdk_version`
  reports `"Temurin 21.0.12"` — two different JVMs visible in the same
  lane's logs (the pre-flight line is a generic host `java -version`
  check, not necessarily the JVM Neo4j itself ran under; not
  reconciled further here).
- **18 collegemsg main-grid cells read `FAIL`, not a weaker-but-real L2**,
  because the committed per-batch reference file for the main grid lived
  only on the now-cleared iTiger checkout (see reading note A11 above) —
  this is a data-availability gap in the *committed* reference, not a
  defect in the xzgpu measurement.

## Macro-stub landing (`scripts/sys_paper_macros.py`)

See the hand-back for the exact landed/pending count and the field gap.
`scripts/sys_paper_macros.py`'s `compute_external_baselines` currently
calls `m.add_pending(...)` unconditionally for all 29 `recExt1*`/`recExt2*`
macros — it does not attempt to read `benchmarks/external-v1/*.json` at
all yet, so landing these three records changes 0/29 stubs until that
generator is wired up (lane W2ad's task, not this lane's — this lane
does not edit `scripts/sys_paper_macros.py`).

## Per-batch rows (addendum 2026-10-08)

`tgms-control-2026-10-05-batches.jsonl` is the raw per-batch data behind
the 19 `tgms-control-2026-10-05.json` cells — one line per batch, every
arm's own `ttf_ms`/`check_wall_ms`/`refresh_wall_ms`/`invalidated_count`/
`false_fresh_count`/`false_stale_count` plus the batch's
`global_recompute_wall_ms`, each value carried over byte-exact (no
rounding) from its source file. `tgms-control-2026-10-05-rows.jsonl`
(the file already described above) carries only per-cell equality
manifests, never this per-batch detail, because lane T1's own per-batch
rows stayed on xzgpu rather than being committed at the time that record
landed.

These per-batch rows exist so that the time-to-fresh of the `tgms-L1`
arm can be reconstructed in "sum" mode (`check_wall_ms + refresh_wall_ms`)
directly from the raw batches, rather than read off this record's own
`arms.tgms-L1.ttf_p50_ms`, which 18 of these 19 cells measured in
"end-to-end" mode instead (`tgms-control-2026-10-05.json`'s own
per-cell `config.measure_ttf`; the 19th, `synth-iv-60k-c1-none-n10000-s0`,
already measured "sum"). `scripts/sys_paper_macros.py`'s
`compute_ext_sum_mode` is the reader.

Source: a read-only `scp` pull on 2026-10-08 of each cell's own
`storm-*-rows.jsonl` from `xzgpu`,
`/mnt/project/xzhang/tgms/external-v1/t1/<cell_id>/` — nothing on xzgpu
was modified. `tgms-control-2026-10-05-batches.SOURCES.txt` records each
pulled file's own sha256 and its exact xzgpu path, one line per cell
(19 lines); `tgms-control-2026-10-05-batches.jsonl`'s own sha256 is in
`SHA256SUMS.txt` beside it.

## Fifth-arc re-measurement of the same-host control (2026-10-08)

Lane R1's re-run of the same 19-cell T1 same-host control, on the fixed
engine + harness (public main `93d4543af61f786884884bee1ebf440ca5ce5673`),
with `--check-cache chain` added to every cell — the exact T1 loop
(`external-v1/t1_control.sh`) replayed as `external-v1/t1b_control.sh`,
same cell order, flags, probe placement, and age-cells-last ordering,
against the same `external-v1/export/` bundles T1 itself checked
against. Worktree `work/tgms-xz-93d4543a` on xzgpu; `crates/` unchanged
since `b6cdde0`, so the engine `.so` (sha256
`d48ae71caca80d9f3ea9f360b2e7a2f242f3e9e03ee223d69c4f8aff08b7faca`) was
copied in from `work/tgms-xz-b6cdde0` rather than rebuilt. Outputs under
fresh `external-v1/t1b/` (T1's own `external-v1/t1-stores/` stayed
read-only; R1 worked from fresh copies under `external-v1/t1b-stores/`).
Memgraph was stopped for the timed window at `2026-10-08T20:50:19Z` and
restarted by the driver at completion, `2026-10-09T00:23:26Z`
(`HOST-T1B.log`'s own first/last lines) — same host protocol as T1.

**Addendum ARC5-B — the equality reading.** Every R1 cell's own
`t1-equality.json` reports `verdict: "L2"`, not T1's `L1`
(`l1_eventlog_sha_match: false` in all 19) — all 19 cells' own
`PROGRESS.log` line confirms `equality=L2`, `batches_realized=20/20` (or
`5/5` for the probe). This is not a regression: the edge-correction
records in this re-run carry **fresh, per-correction `disc` stamps**
(confirmed directly — e.g. `collegemsg-c1-deep-n1000-s0`'s batch 0
stamps `correction_disc="a1-d6b1931bec9a77cc"`, batch 1
`"a2-0d75d9f0aaae0247"`, each one different, never the fixed-per-class
`"#0"`/`"a2-disjoint"` the pre-Addendum-7 generator used), the fix landed
in commit `3b55042`. That commit postdates every export bundle under
`external-v1/export/` and predates this arc's fixes, so the exported
`digests.json` a cell is scored against still carries the old
fixed-per-class `disc` values — an **L1** byte-identical-eventlog match
is structurally impossible against that export now, for any run built
after `3b55042`, regardless of correctness. Everything else about the
run — `ttf_mode`, batch/record order, the `changed` lists, `refused`,
and every arm's `invalidated_count`/`false_fresh_count`/
`false_stale_count` — matches T1's on all batches, i.e. the fixes change
`disc` bookkeeping only and leave the scored logs byte-identical in
every other respect.

**Producing the evidence (19 rows, all batches).** For
`correction_class`/`correction_generator`/`correction_placement`/
`changed`, T1's committed `t1_equality.per_batch` (in
`tgms-control-2026-10-05-rows.jsonl`) and R1's own `t1-equality.json`
were each independently checked against the *same* immutable export
bundle's `digests.json`/`deltas.jsonl` — when both report
`all_match: true` for a batch, T1 and R1 are transitively identical on
that batch's correction_class/generator/placement/changed (two things
equal to the same third thing are equal to each other). For each arm's
`invalidated_count`/`false_fresh_count`/`false_stale_count`, T1's
committed per-batch counts (`tgms-control-2026-10-05-batches.jsonl`) were
diffed directly against R1's own raw per-batch rows (pulled read-only
from `external-v1/t1b/<cell>/storm-*-rows.jsonl`, sha256-recorded in
`tgms-control-2026-10-08-arc5-batches.SOURCES.txt`). `refused_count` is
reported for R1 only below — T1's 2026-10-05 committed addendum never
carried that field, so there is no committed T1 value to diff against
(stated as a gap, not assumed zero/equal); every R1 batch's own
`refused_count` is 0 regardless.

| cell_id | R1 verdict (vs. export) | correction-metadata match (vs. T1, transitive) | arm-count match (vs. T1, direct) | fully identical |
|---|---|---:|---:|---|
| `collegemsg-c1-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c1-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c3-days-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c3-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c3-hours-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c3-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c3-recent-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c4-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `collegemsg-c4-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c1-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c1-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c1-none-n10000-s0` (probe) | L2 | 5/5 | 5/5 | yes |
| `synth-iv-60k-c3-days-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c3-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c3-hours-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c3-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c3-recent-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c4-deep-n1000-s0` | L2 | 20/20 | 20/20 | yes |
| `synth-iv-60k-c4-none-n1000-s0` | L2 | 20/20 | 20/20 | yes |

**19/19 cells fully identical** on every field this check could compare
(385 batches total: 18×20 + 5). No mismatch of any kind was found.

**Walls vs. T1's, and per-cell `ttf_p50_ms` for the three TGMS arms.**
R1 is dramatically faster than T1 — not a measurement artifact, but the
expected effect of the engine work landed between T1's commit
(`fdd393c91c1199f7cfe03aba53ed1733f43111b0-dirty`) and R1's
(`93d4543a`), including the check-cache chain setting itself (T1 never
set `--check-cache`, so every T1 check re-walked the full event log; R1
walks it once per batch and memoizes):

| cell_id | T1 wall (s) | R1 wall (s) | ratio (R1÷T1) | `global-recompute` p50 (ms) | `tgms-L0` p50 (ms) | `tgms-L1` p50 (ms) |
|---|---:|---:|---:|---:|---:|---:|
| `collegemsg-c1-deep-n1000-s0` | 8905.0 | 374.8 | 0.042 | 5036.5 | 3867.7 | 3844.1 |
| `collegemsg-c1-none-n1000-s0` | 9191.1 | 396.1 | 0.043 | 4898.8 | 4401.6 | 4375.9 |
| `collegemsg-c3-days-n1000-s0` | 8991.9 | 384.8 | 0.043 | 4999.0 | 4061.0 | 4037.5 |
| `collegemsg-c3-deep-n1000-s0` | 9155.7 | 381.8 | 0.042 | 4969.5 | 4241.6 | 4145.2 |
| `collegemsg-c3-hours-n1000-s0` | 8969.5 | 385.9 | 0.043 | 5050.3 | 4394.8 | 4317.1 |
| `collegemsg-c3-none-n1000-s0` | 8744.3 | 369.2 | 0.042 | 4849.2 | 3972.1 | 3938.8 |
| `collegemsg-c3-recent-n1000-s0` | 8512.2 | 359.3 | 0.042 | 5001.7 | 3753.3 | 3712.3 |
| `collegemsg-c4-deep-n1000-s0` | 9403.6 | 693.4 | 0.074 | 5092.5 | 4248.0 | 4198.4 |
| `collegemsg-c4-none-n1000-s0` | 9244.8 | 669.6 | 0.072 | 4973.8 | 4043.8 | 4026.9 |
| `synth-iv-60k-c1-deep-n1000-s0` | 9645.0 | 786.2 | 0.082 | 15484.7 | 8655.7 | 8568.7 |
| `synth-iv-60k-c1-none-n1000-s0` | 9652.0 | 779.9 | 0.081 | 15625.5 | 9058.2 | 9083.8 |
| `synth-iv-60k-c1-none-n10000-s0` (probe) | 18922.9 | 1399.7 | 0.074 | 162369.6 | 128542.9 | 128435.7 |
| `synth-iv-60k-c3-days-n1000-s0` | 9377.6 | 735.2 | 0.078 | 15644.3 | 8840.1 | 8788.9 |
| `synth-iv-60k-c3-deep-n1000-s0` | 9536.1 | 754.4 | 0.079 | 15568.7 | 8953.3 | 8892.2 |
| `synth-iv-60k-c3-hours-n1000-s0` | 9340.4 | 748.5 | 0.080 | 15456.1 | 8748.6 | 8728.9 |
| `synth-iv-60k-c3-none-n1000-s0` | 9328.7 | 738.2 | 0.079 | 15603.3 | 7766.6 | 7727.3 |
| `synth-iv-60k-c3-recent-n1000-s0` | 9557.0 | 779.3 | 0.082 | 15625.7 | 9101.3 | 9070.8 |
| `synth-iv-60k-c4-deep-n1000-s0` | 10012.2 | 1040.5 | 0.104 | 15811.6 | 8974.2 | 8952.9 |
| `synth-iv-60k-c4-none-n1000-s0` | 9574.6 | 990.1 | 0.103 | 15904.9 | 5013.9 | 5004.5 |

Summed wall: T1 186,064.7 s (51.68 h) vs. R1 12,766.8 s (3.55 h) —
overall ratio **0.069** (R1 ≈14.6× faster end-to-end across all 19
cells), consistent with `HOST-T1B.log`'s own start/end timestamps
(`20:50:19Z` → `00:23:26Z` next day ≈ 12,787 s wall-clock for the whole
loop, matching the summed per-cell figure to within inter-cell
overhead). No verdict on what this ratio implies is drawn here — the
coordinator scores it.

**Records**: `tgms-control-2026-10-08-arc5.json` + `-rows.jsonl`,
assembled with `scripts/external_record.py --campaign tgms-control`
(extended this lane to carry `check_cache`/`check_cache_misses_median`/
`e2e_refresh_calls_median`, since T1's original 2026-10-05 run had no
per-batch sidecar on this host for the assembler to read those per-batch
fields from — see the assembler's own commit). `scripts/
check_result_manifest.py` passes. `tgms-control-2026-10-08-arc5-batches.jsonl`
+ `.SOURCES.txt` mirror the 2026-10-05 per-batch-rows addendum exactly
(one line per batch, byte-exact values, no rounding), built from the
same read-only tar pull of `external-v1/t1b/` this section's own
equality reading uses; sha256 of all four files is in `SHA256SUMS.txt`.

## Per-check cost addendum (2026-10-08): the field neither control record carries

Neither `tgms-control-2026-10-05-batches.jsonl` (the pre-arc control) nor
`tgms-control-2026-10-08-arc5-batches.jsonl` (the fifth-arc re-measurement
above) carries `candidate_survivors` in its per-batch rows, so the
same-host control's own per-check cost (`tgms-L0`/`tgms-L1`
`check_wall_ms` ÷ `candidate_survivors`, the quantity
`recStormV3PerCheckMs{Synth,CollegeMsg}` reports for the storm grids) was
landed as the literal text "not measured" in `scripts/sys_paper_macros.py`.
The per-batch rows on xzgpu do carry it: a read-only `scp` pull on
2026-10-08 of each cell's own `storm-*-rows.jsonl` from
`/mnt/project/xzhang/tgms/external-v1/t1b/<cell_id>/` (the fifth-arc
tree) and `/mnt/project/xzhang/tgms/external-v1/t1/<cell_id>/` (the
pre-arc tree) — nothing on xzgpu was modified, one `tar`/`scp` pull of
both trees together.

**Records**: `tgms-control-checks-2026-10-08.jsonl` — one line per
batch, both controls (730 lines total: 2 controls x (18 cells x 20
batches + 1 probe cell x 5 batches)), each line carrying `control`
(`2026-10-05` or `2026-10-08-arc5`), `cell_id`, `batch_index`,
`candidate_survivors`, `intersects_calls`, `tgms_l0_check_wall_ms`, and
`tgms_l1_check_wall_ms`, every value byte-exact (no rounding) from its
source `storm-*-rows.jsonl` row. `tgms-control-checks-2026-10-08
.SOURCES.txt` records each of the 38 pulled per-cell files' own sha256,
which control it belongs to, and its exact xzgpu path. Both files'
sha256 is in `SHA256SUMS.txt`.

**Computed medians** (median over the 9 N=1,000 cells per store of that
cell's own median-over-batches `tgms-L0.check_wall_ms` /
`candidate_survivors`, ms — same two-level-median convention
`recStormV3PerCheckMs{Synth,CollegeMsg}` uses):

| store | pre-arc (2026-10-05) | fifth-arc (2026-10-08-arc5) |
|---|---:|---:|
| collegemsg | 185.9 | 12.3 |
| synth-iv-60k | 204.0 | 12.3 |

The fifth-arc `--check-cache chain` setting (added to every R1 cell, see
the "Fifth-arc re-measurement" section above) drops the per-check cost
by roughly 16-17x on both stores, consistent with that section's own
observation that R1 "walks it once per batch and memoizes" instead of
re-walking the full event log on every check the way the pre-arc T1
control did. The probe cell (`synth-iv-60k-c1-none-n10000-s0`, N=10,000)
medians 11.9 ms/check on the fifth-arc side — landed separately
(`recExt1ControlArcFiveProbePerCheckMs`) rather than folded into either
store's N=1,000 median above. No verdict on what these numbers imply is
drawn here — the coordinator scores it.
