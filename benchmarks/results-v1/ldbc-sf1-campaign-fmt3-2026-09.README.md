# P-SF1 — SF1 reruns at the format-3 engine on xzgpu (2026-09)

Companion to `ldbc-sf1-campaign.json` (commit `8fd1581a6ad2`, wall 4233.0 s).
Pinned at the current tip of `origin/main`, not `ebe1dc2` as the brief
originally named (coordinator update — the mirror had already received the
newer commit).

Machine: xzgpu. Treatment commit `a6b3e94c4ff1` (`Merge ddc2f1e: bound the
identity postings across compaction cycles (D-087 — the soak/replay memory
growth)`) — built fresh in a new worktree `work/tgms-xz-a6b3e94`
(`PYO3_PYTHON=.../venv/bin/python cargo build --release -p tgms-engine-py`,
artifact copied to `tgms/_engine.cpython-312-x86_64-linux-gnu.so`).
`build_info()`: `{'profile': 'release', 'opt_level': '3', 'engine_version':
'0.8.0', 'debug_assertions': False, 'manifest_format_version': 3}`.
`adapter._store.postings_stats("edge")` present on `NativeStore` (D-087
marker) — confirmed.

## Store build (`scripts/build_snb_store.py`, same flags as the existing
record's provenance: `--backend native --batch 250 --digest manifest
--write-path bulk --compact-every 100000`, csv_root unchanged)

- Wall: 780.8 s (streaming 20,367,142 records; 1 compaction run, 113.6 s of
  the 780.8 s). Fidelity gate: PASS, 2,997,352 nodes / 17,369,790 edges —
  identical totals to the reference `stores/snb-sf1` dataset card.
- RSS of the build process, sampled every 60 s (`ps -o rss=`):
  start 283,488 KB; climbed steadily during streaming ingest and plateaued
  at 2,445,440 KB (2.33 GB) for ~7 consecutive minutes; then spiked during
  the finalization/digest phase to 6,951,124 KB then 24,976,448 KB (peak,
  ~23.8 GB) just before the process exited (fidelity check + card write).
  End (last live sample) = peak = 24,976,448 KB. The plateau during the
  many-commit streaming phase itself (the D-087-relevant portion) stayed
  flat rather than growing; the late spike is the whole-store stats/digest
  pass, not the per-commit path.

## Per-plan table (existing ms / new ms / ratio / rows equal / outcome)

| plan | existing ms | new ms | ratio | rows equal | outcome (old->new) |
|---|---:|---:|---:|:---:|---|
| BI10 | 19,194 | 11,432 | 0.60 | True | COMPLETED->COMPLETED |
| BI11 | 28,783 | 21,865 | 0.76 | True | COMPLETED->COMPLETED |
| BI12 | 122,201 | 98,263 | 0.80 | True | COMPLETED->COMPLETED |
| BI17 | 51,991 | 37,361 | 0.72 | True | COMPLETED->COMPLETED |
| BI18 | 29,735 | 24,646 | 0.83 | True | COMPLETED->COMPLETED |
| BI3 | 22,766 | 16,688 | 0.73 | True | COMPLETED->COMPLETED |
| BI4 | 44,510 | 36,639 | 0.82 | True | COMPLETED->COMPLETED |
| BI6 | — | — | — | True (both None) | ERRORED->ERRORED |
| BI6.v2 | (no baseline) | 291,971 | — | n/a | (new)->COMPLETED |
| BI7 | 13,076 | 7,336 | 0.56 | True | COMPLETED->COMPLETED |
| BI9 | 66,037 | 60,501 | 0.92 | True | COMPLETED->COMPLETED |
| IC11 | 10,160 | — | — | False | COMPLETED->BIND_FAILED |
| IC12 | 16,807 | — | — | False | COMPLETED->BIND_FAILED |
| IC2 | 11,206 | — | — | False | COMPLETED->BIND_FAILED |
| IC5 | 18,452 | — | — | False | COMPLETED->BIND_FAILED |
| IC6 | 39,453 | — | — | False | COMPLETED->BIND_FAILED |
| IC8 | 12,510 | — | — | False | COMPLETED->BIND_FAILED |
| IC9 | 35,443 | — | — | False | COMPLETED->BIND_FAILED |
| IS1 | (no baseline) | — | — | n/a | (new)->BIND_FAILED |
| IS2 | 12,307 | — | — | False | COMPLETED->BIND_FAILED |
| IS3 | 7,747 | — | — | False | COMPLETED->BIND_FAILED |
| IS4 | (no baseline) | — | — | n/a | (new)->BIND_FAILED |
| IS5 | (no baseline) | — | — | n/a | (new)->BIND_FAILED |
| IS6 | 10,239 | — | — | False | COMPLETED->BIND_FAILED |
| IS7 | 13,366 | — | — | False | COMPLETED->BIND_FAILED |

`rows equal` is a count-level comparison (`rows` field); neither this record
nor the existing one carries per-row content (`--emit-rows` was not used by
either run), so a row-digest comparison via `scripts/ldbc_compare.py
--sort-keys` was not possible — recorded as a method limitation, not
skipped silently.

## The outcome-class change (scored-bi arm unaffected; characterization-interactive arm entirely BIND_FAILED)

All 14 characterization-interactive plans (the 11 original IC/IS rows plus
the 3 new D2 rows IS1/IS4/IS5) that previously completed now record
`BIND_FAILED` with `phantom_anchor: true`, e.g. (verbatim):

    IC11: bound id(s) ['246290604668947'] name no entity in the store
    (source: validation_params-sf1.csv, first row with all keys). Binding
    them would make the plan scan the whole store and return nothing.

The rebuilt store's own `dataset_card.json` independently states
`parameter_sources.interactive_rows: "UNAVAILABLE — 11 of 11 bind to ids
that name no entity here"` for this exact BI-snapshot store. The existing
`ldbc-sf1-campaign.json` record nonetheless shows all 11 original IC/IS
rows as `COMPLETED`. This is reported as an outcome-class change per the
pre-registration's refutation clause; no cause is investigated or claimed
here — left to the coordinator.

The scored-bi arm is otherwise row-identical by count on every plan, same
outcome class throughout (including BI6's ERRORED reproducing), and all new
`ms` values ratio below 1.0 (faster) but outside the +-20% band predicted —
reported as a wall-time miss, not a correctness refutation, per the
pre-registration.

## Dataset

CSV-input digest (per-file manifest, `relative_path byte_size mtime_utc_iso
sha256`, sorted by path, 55 files) =
`9b0373fa78b2be5dba12a8181115f064ab52735d7b4641e396df9622f1bda7ae`
(`ldbc-sf1-campaign-fmt3-2026-09-csv-manifest.txt`), computed fresh on
xzgpu, sha256-verified byte-identical after transfer.

## Records and transfer

- `ldbc-sf1-campaign-fmt3-2026-09.json` — sha256
  `6da13c913ecca80bb7f3b976e011dde31a103e2ebfb20f282d208ac826a82165`,
  verified byte-identical between the xzgpu copy and the copy committed
  here (the manifest's `build_info` and `dataset` fields were added after
  transfer, so this digest is of the final, committed file).
- `ldbc-sf1-campaign-fmt3-2026-09-csv-manifest.txt` — the CSV-input
  per-file manifest backing the digest above.

The store copy (`stores/snb-sf1-xz-a6b3e94`, 4.4 GB) was deleted from
xzgpu after the record was verified. The engine worktree
(`work/tgms-xz-a6b3e94`) remains in place.
