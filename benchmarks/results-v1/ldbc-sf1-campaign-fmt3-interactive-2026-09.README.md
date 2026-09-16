# P-SF1b — SF1 characterization-interactive arm rerun, with `--csv` (2026-09)

Companion to `ldbc-sf1-campaign-fmt3-2026-09.json` (P-SF1, unedited). This
record covers **only** the characterization-interactive arm (the 11 original
IC/IS plans plus the post-freeze IS1/IS4/IS5); P-SF1's scored-bi arm is
unaffected and stands as recorded there.

## The bug (`SF1_INTERACTIVE_BIND_DIAGNOSIS_2026-09-15.md`)

P-SF1's rerun invoked `scripts/tgir_ldbc_sf1.py` without `--csv
<initial_snapshot>`. Without it, the characterization-interactive arm binds
raw `validation_params-sf1.csv` ids drawn from the separately generated LDBC
Interactive dataset (its own §E addendum-4 definition) instead of
`sample_anchor()` draws from the store's own BI-snapshot corpus. Those raw
ids name no entity in a BI-snapshot store, so every one of the 14 plans
recorded `BIND_FAILED` / `phantom_anchor: true` — `csv_root: ""` in that
manifest is the tell. The addendum-4 code itself is byte-identical between
the original record's commit and `a6b3e94`; nothing in the runner was wrong,
only the invocation.

This record is the corrected rerun: same store, same plans, same seeds,
`--csv /mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-merged-fk/graphs/csv/bi/composite-merged-fk/initial_snapshot`
supplied.

## Pin

Per the coordinator's 2026-09-15 pin, this rerun uses the current tip
`54dcab0` (**not** `a6b3e94`, which P-SF1 used) — built fresh in a new
worktree `work/tgms-xz-54dcab0`
(`PYO3_PYTHON=.../venv/bin/python cargo build --release -p tgms-engine-py`,
artifact copied to `tgms/_engine.cpython-312-x86_64-linux-gnu.so`).
`build_info()`: `{'profile': 'release', 'opt_level': '3', 'engine_version':
'0.8.0', 'debug_assertions': False, 'manifest_format_version': 3}`.

## Store build (`scripts/build_snb_store.py`, same flags as P-SF1's and the
existing baseline's provenance: `--backend native --batch 250 --digest
manifest --write-path bulk --compact-every 100000`, csv_root unchanged)

- Wall: 881.0 s (streaming 20,367,142 records at up to ~27.6k rec/s
  cumulative; 1 compaction run, 139.0 s of the 881.0 s) — +12.8% vs P-SF1's
  780.8 s, within the ±20% band. Fidelity gate: PASS, 2,997,352 nodes /
  17,369,790 edges — identical to the reference `stores/snb-sf1` dataset
  card and to P-SF1's `stores/snb-sf1-xz-a6b3e94` totals.
- RSS (`ps -o rss=`, sampled every 60 s): start ~12,676 KB; climbed through
  streaming to a plateau at ~2,444,336–2,445,344 KB (~2.33 GB) for ~9
  consecutive minutes (longer than P-SF1's ~7 min plateau, consistent with
  general host-load variance, not a different code path); then the
  finalization/digest/compaction phase spiked RSS to 14,231,644 KB, then
  21,689,624 KB (last live sample from the 60 s sampler); the store's own
  `dataset_card.json` records a `maxrss_kb` of 25,201,012 KB (~24.0 GB) —
  consistent with P-SF1's ~24.98 GB peak.

## Interactive-arm run

Ran only the 14 characterization-interactive plans via a small external
driver (`scripts/run_interactive_arm.py`, untracked scratch script — not a
modification of `tgir_ldbc_sf1.py`) that reuses `tgir_ldbc_sf1.py`'s own
`run_child`, `CAMPAIGN_SEED`, `DEFAULT_CEILINGS`, `ROWS_DIGEST_RULE` etc.
directly, restricted to the interactive-arm plan ids (`tgir_ldbc_sf1.py`'s
own `--plan` flag only supports `all` or one id, so running one arm without
also running the 10 BI plans needs an external loop; nothing about plan
binding, admission, execution, or the ceiling protocol was changed).
`--emit-rows` was used (a `sf1b-rows/tgms-<ID>.json` dump per plan exists on
xzgpu, not transferred). Every plan ran in its own child subprocess
(`every_plan_in_a_child`), so per-plan wall includes a fresh store open
(~140–270 s each observed, dominated by the ~165 s SF1 index-build warm-up),
not just the measured `ms`.

One deviation from a single unattended invocation: the first attempt was
interrupted by the *orchestration* timeout of the tool driving it (a 30-
minute cap on the ssh session), not by any plan or ceiling — IC11 through
IS4 (11 of 14) had already flushed to disk. The driver was re-run with a
`--resume` flag (skips plan ids already present in `--out`) under `setsid
nohup`, completing IS5/IS6/IS7. `manifest.wall_s` (590.0 s total) is
therefore the **sum of each plan's own `run_child` wall_s**, not one
top-of-process timer — noted as `manifest.wall_s_method` in the record,
since a single timer would only have covered the resumed tail. No plan was
re-run: the resume loaded the 11 already-recorded outcomes verbatim.

## Result: outcome-class bug fixed; count-level comparison vs the pre-P-SF1
baseline (`ldbc-sf1-campaign.json`, the last record where this arm actually
completed — P-SF1's own record is unusable as a baseline here since every
row in it is the `BIND_FAILED` bug this rerun fixes)

| plan | baseline ms | new ms | ratio | rows equal | wall within ±20% | outcome |
|---|---:|---:|---:|:---:|:---:|---|
| IC11 | 10,160 | 2,731 | 0.269 | True | No (favourable) | COMPLETED |
| IC12 | 16,807 | 10,725 | 0.638 | True | No (favourable) | COMPLETED |
| IC2 | 11,206 | 3,181 | 0.284 | True | No (favourable) | COMPLETED |
| IC5 | 18,452 | 11,493 | 0.623 | True | No (favourable) | COMPLETED |
| IC6 | 39,453 | 34,677 | 0.879 | True | **Yes** | COMPLETED |
| IC8 | 12,510 | 4,005 | 0.320 | True | No (favourable) | COMPLETED |
| IC9 | 35,443 | 34,138 | 0.963 | True | **Yes** | COMPLETED |
| IS2 | 12,307 | 4,047 | 0.329 | True | No (favourable) | COMPLETED |
| IS3 | 7,747 | 518 | 0.067 | True | No (favourable) | COMPLETED |
| IS6 | 10,239 | 2,730 | 0.267 | True | No (favourable) | COMPLETED |
| IS7 | 13,366 | 4,473 | 0.335 | True | No (favourable) | COMPLETED |
| IS1 | (no baseline — post-freeze) | 2,324 | — | n/a | n/a | COMPLETED |
| IS4 | (no baseline — post-freeze) | 121 | — | n/a | n/a | COMPLETED |
| IS5 | (no baseline — post-freeze) | 2,414 | — | n/a | n/a | COMPLETED |

Compare output (count-level; `scripts/ldbc_compare.py --sort-keys
benchmarks/ldbc-ref-v1/sort_keys.yaml` needs a per-row `--emit-rows` dump on
**both** sides, and the baseline predates `--emit-rows` — as stated in
P-SF1's own README for its scored-bi arm — so a row-content diff is not
possible here either; this is a method limitation, stated, not a silent
skip):

    11/11 outcome-class identical (COMPLETED), 11/11 rows identical,
    2/11 wall within ±20% (9 favourable misses, all faster)

**Prediction verdict** (P-SF1's own predictions, frozen, unchanged by this
rerun): outcome class identical — **holds** (14/14 COMPLETED, the bug is
fixed). Rows identical — **holds** for all 11 plans with a baseline (count
level only). Wall within ±20% — **refuted as a favourable miss** for 9 of
11 plans (2 of 11 — IC6, IC9 — do fall inside the band); per the
pre-registration's own rule, "a favourable miss is a miss." This mirrors
exactly what P-SF1's own README reported for its scored-bi arm (all-faster,
outside-band wall times), so the direction and magnitude of the miss is
consistent with the rest of this campaign, not an anomaly specific to the
interactive arm.

## Validation

`scripts/check_result_manifest.py` on this record: **FAIL** (`'schema_version'
is a required property (+9 more)`) — expected, not a defect introduced here:
this record continues the same ad hoc `results-v1/*.json` shape as P-SF1 and
the pre-existing corpus, which `benchmarks/schema/README.md`'s own audit
already documents as failing the formal schema near-universally. No new
manifest-format work was in scope for this rerun.

## Dataset

CSV-input digest (per-file manifest, `relative_path byte_size mtime_utc_iso
sha256`, sorted by path, 55 files) =
`83b98724893a178a21f6de9a4848bea9a96897750f09aaadd5850867098299e3`
(`ldbc-sf1-campaign-fmt3-interactive-2026-09-csv-manifest.txt`), computed
fresh on xzgpu. The file set and every per-file hash are identical to
P-SF1's own manifest (confirmed by direct diff) — same physical CSV
directory; the aggregate digest differs from P-SF1's only because this
run's directory walk visited files in a different (still deterministic,
sorted-by-path) order.

## Records and transfer

- `ldbc-sf1-campaign-fmt3-interactive-2026-09.json` — sha256
  `990ffae5496c4af25471dc88145f0cb22b37a1203641f393fffaa800ccff3e4a`,
  verified byte-identical between the xzgpu copy and the copy committed
  here.
- `ldbc-sf1-campaign-fmt3-interactive-2026-09-csv-manifest.txt` — sha256
  `83b98724893a178a21f6de9a4848bea9a96897750f09aaadd5850867098299e3`,
  verified byte-identical between the xzgpu copy and the copy committed
  here.

The store copy (`stores/snb-sf1-xz-54dcab0`, 4.4 GB) was deleted from
xzgpu after the record was verified. The engine worktree
(`work/tgms-xz-54dcab0`) remains in place. P-SF1's own record and README
(`ldbc-sf1-campaign-fmt3-2026-09.json` / `.README.md`) are unedited; this
file supersedes only its characterization-interactive arm, as recorded in
`manifest.supersedes` of the JSON above.
