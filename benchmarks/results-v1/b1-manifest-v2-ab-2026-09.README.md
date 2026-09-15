# B1-v2 A/B — format 3 (reader torn-tail fix) vs. the pinned format-2 soak engine (2026-09)

Same Addendum-3 (manifest forecast) protocol as `b1-manifest-ab-2026-09.json`
(the v1 B1 A/B): SF1 manifest bytes at matched 2.5M ops, SS20 batch=1
commitcost phase deciles/p50 (300 commits, 100k-row seed store), chain-open
at G~10k K=512. Reported beside the thresholds the brief names; **no
verdicts here** — scoring is the coordinator's.

Machine: xzgpu, 40 cores / 93 GB, `Linux-5.4.0-216-generic-x86_64-with-glibc2.31`.
Control commit `886805f600bc` — the format-2 engine already pinned for the
24h longevity soak (`MANIFEST_FORMAT_VERSION == 2`, checkpoint-every default
512); **not rebuilt** for this lane. Treatment commit `7a5ff9871a0e` (public
main, `MANIFEST_FORMAT_VERSION == 3`, the reader torn-tail two-condition
rule) — built fresh into a new detached worktree `work/tgms-xz-7a5ff98`
(`cargo build --release -p tgms-engine-py`, artifact copied to
`tgms/_engine.cpython-312-x86_64-linux-gnu.so`; the shared venv's installed
packages were never touched).

Records: `b1-manifest-v2-ab-2026-09.json` (schema-conformant summary,
validated with `scripts/check_result_manifest.py`), `b1-manifest-v2-ab-2026-09-raw.json`
(consolidated raw records), companion `b1-manifest-v2-ab-2026-09-logs/`
(verbatim run logs + per-rep commitcost JSON, sha256-verified byte-identical
against the xzgpu copies before those were deleted).

## B1(a) — SF1 manifest bytes at matched 2.5M node ops

`scripts/build_snb_store.py --backend native --batch 250 --compact-every 0
--digest manifest --write-path assert`, killed at the `2,500,000 ops` log
line (process was still running at that line in both arms, same as v1;
confirmed exited before measuring).

| | control (886805f, format 2) | treatment (7a5ff98, format 3) | threshold |
|---|---:|---:|---|
| manifest bytes | 73,790,679 B (73.8 MB) | 74,403,447 B (74.4 MB) | 62 MB ± 5% (58.9–65.1 MB) |
| segment bytes | 158,966,639 B (159.0 MB) | 163,447,175 B (163.4 MB) | — |
| ops/s at each 500k checkpoint | 9,098 / 5,761 / 4,121 / 3,155 / 2,543 | 12,400 / 9,401 / 7,579 / 6,264 / 5,335 | — (observation only) |

Observation, no claim attached: the treatment ran at roughly double the
control's ops/s at every checkpoint (e.g. 5,335 vs 2,543 ops/s at 2.5M).
Both arms' manifest bytes land close together (~74 MB), well above the 62 MB
± 5% band recorded beside them.

## B1(b) — SS20 batch=1 commitcost, 300 commits, 100k-row seed store

3 reps each (control, treatment default K=512), 1 rep each for the two
off-default K-sweep values (K=512 is the treatment default already covered
by the 3-rep run, so only K=128 and K=1024 were additionally swept — this
differs slightly from v1, which re-ran K=512 explicitly as part of its
sweep).

| | control | treatment (K=512) | treatment K=128 | treatment K=1024 |
|---|---:|---:|---:|---:|
| manifest_us first→last decile (median of reps) | 1.115x | 1.079x | 1.068x (1 rep) | 1.150x (1 rep) |
| engine-commit total_us first→last decile (median of reps) | 1.733x | 1.674x | 1.818x (1 rep) | 2.074x (1 rep) |
| engine-commit p50 (total_us), reps | 4,652 / 5,490 / 5,430 | 5,478 / 5,320 / 5,772 | 4,597 (1 rep) | 5,276 (1 rep) |
| engine-commit p50 median | 5,430 us | 5,478 us | — | — |

Threshold: decile ≤ 1.10x (refute ≥ 1.2x) — reported per component above
(manifest_us and engine-commit total_us separately, as the underlying phases
move differently, same as v1's OR-of-two-components framing). Threshold: p50
paired ≤ 0.70x control **or** ≤ 5.58 ms absolute — treatment median p50 is
5,478 us = 5.478 ms (at the absolute bound); the paired ratio
treatment/control is 5,478/5,430 = 1.009x (not below 0.70x).

## B1(c) — cold/warm chain-open at G~10k, K=512

Same corrected methodology as v1: times `NativeAdapter(store/"native")`
construction directly (the Rust-level `NativeStore::open`), not
`tgms.open()`. 3 reps per arm, back-to-back in one process, immediately
after each arm's B1(a) run (no root on this host, so no explicit
posix_fadvise cache-drop between reps — first rep is process-cold, later
reps warm, same caveat v1 recorded elsewhere).

| | control (G≈10,759) | treatment (G≈11,029) |
|---|---:|---:|
| open, 3 reps | 1,427.6 / 2,279.2 / 2,247.9 ms | 1,252.7 / 1,705.3 / 2,691.3 ms |
| open, median | 2,247.9 ms | 1,705.3 ms |

Threshold: ≤ 70 ms, component-scored. `NativeAdapter()` exposes no internal
phase timer to Python (unlike the commit path's `phase_p50_us`), so a
checkpoint-load-vs-delta-replay component split was not measured — flagged
here rather than fabricated. The raw open time is reported instead.

## Notes

- Dataset digest recomputed this run: `d8ccf1970f67f657d7121b3586399d180c727e867055f1da7c73d73caebe67d1`
  (sha256 of the sorted `(relative_path, size)` listing under
  `initial_snapshot/`) — structurally identical corpus to v1's B1 A/B. v1's
  own recorded digest string is 62 hex characters, two short of a full
  sha256; that looks like a copy/truncation artifact in the v1 record, not a
  corpus difference.
- Working store copies (`/mnt/project/xzhang/tgms/tmp/b1v2-control`,
  `.../b1v2-treatment`) were deleted from xzgpu after B1(c). The control
  worktree `tgms-xz-886805f` was left in place, untouched and unbuilt by
  this lane. The treatment worktree `tgms-xz-7a5ff98` remains on xzgpu.
