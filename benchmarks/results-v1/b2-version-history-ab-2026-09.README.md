# B2 A/B — bounded `version_history` (2026-09)

Pre-registered forecast and falsifiers:
`docs/design/BOUNDED_VERSION_HISTORY_FORECAST_2026-09-13.md` SS4/SS5
(untracked, coordinator-internal; same gitignore boundary as the B1 memo).

Machine: xzgpu, 40 cores / 93 GB, `Linux-5.4.0-x86_64`, Python 3.12.13.
Control commit `cb0e6afc17e7` reads the original format-1
`stores/synth-{1m,10m}-native` **directly, read-only — originals never
modified**. Treatment commit `4af218192d8c` reads an **upgraded copy** of
each (`tgms store upgrade-manifests`), left in
`tmp/b2-{1m,10m}-upgraded` on the server (not committed; scratch).

Records: `b2-version-history-ab-2026-09.json` (schema-conformant summary),
`b2-version-history-ab-2026-09-raw.json` (consolidated raw records),
companion `b2-version-history-ab-2026-09-logs/` (verbatim logs + per-rep
JSON, sha256-verified before server cleanup).

**Methodology note.** The probe calls `call_operator(adapter,
"version_history", ..., skip_cost_check=True)` — the plain path raises
`CostError` from the standing admission-guardrail refusal the memo
describes (confirmed empirically: the first attempt at this measurement hit
exactly that exception on both engines at both scales, before the probe was
fixed to bypass it, exactly as the memo instructed). Peak RSS is `VmHWM`
read from `/proc/self/status` **inside** the same process, right before
exit — the project's own documented convention (`docs/eval_resources.md`'s
`_vm_status()`; `ru_maxrss` is noted there as fork-polluted and avoided).

## version_history(kind="edge", belief="all"), 3 reps

| scale | | control | treatment |
|---|---|---:|---:|
| 1M | wall (median) | 13,421.3 ms | 218.2 ms |
| 1M | peak RSS (VmHWM, median) | 1,366,276 KB (1.399 GB) | 167,968 KB (0.172 GB) |
| 10M | wall (median) | 179,250.1 ms | 1,866.8 ms |
| 10M | peak RSS (VmHWM, median) | 13,414,332 KB (13.74 GB) | 1,229,724 KB (1.259 GB) |

At 10M: **95.2x faster, 10.9x less peak memory.** Control's own reproduction
runs well above the historical baseline (75.8 s / 10.6 GB) — measured here
at ~177 s avg / ~13.7 GB avg — so today's host/data conditions are not
identical to the original D-069 measurement; this does not change the
treatment-vs-falsifier-bound comparisons below, which pass by wide margins
either way.

## nodes_columnar, 1 rep (bonus data point — not the pre-registered check)

| scale | control wall / VmHWM | treatment wall / VmHWM |
|---|---:|---:|
| 1M (20,000 node versions) | 32.3 ms / 130,544 KB | 21.1 ms / 124,128 KB |
| 10M (60,000 node versions) | 87.8 ms / 786,448 KB | 70.6 ms / 777,560 KB |

The memo's actual secondary falsifier is on a **compacted SNB SF1 store**
(2,997,352 node versions) — a different, much larger scale that this lane
did **not** build/measure (out of time/resource budget). The numbers above
are on synth-1m/10m instead, where the memo itself notes the pathology is
"invisible" at 60k node versions. **Not scored as a falsifier result** —
flagged as an open item.

## Digest stability

`scripts/check_digest_stability.py` on the treatment engine: `native: 19
cases in 2.7s`, `duckdb: 19 cases in 6.2s`, **`OK: 38 result_digests
unchanged across native, duckdb`**, rc=0. The specific frozen digest named
by the memo,
`84e8853c5d6732deb6d3e4329b46fd5f03770eff399b1099a6b68409be78fbfd`, is
confirmed present for `version_history` on both backends at
`scripts/frozen_digests_v1.json:257,278` and is included in the 38 checked.

## Verdicts (verbatim)

1. **Peak RSS <= 2.5 GB at 10M**: **PASS** (1.259 GB).
2. **Wall <= 113.7 s (1.5x * 75.8 s) at 10M**: **PASS** (1.867 s).
3. **CollegeMsg digest `84e8853c...` unchanged**: **PASS**.
4. **1M->10M peak-RSS ratio <= 2.5x**: **REFUTED** — measured **7.33x**
   (0.172 GB -> 1.259 GB). Per the memo's own wording, this "refutes the
   near-flatness claim even if the absolute numbers pass" — exactly what
   happened here. Plausible mechanism, consistent with Addendum 1 items 1
   and 3: at 1M the peak is dominated by the fixed per-store floor (uid
   dictionary + out-degree map), which the 10M store's genuinely larger key
   array (~320 MB, 32 B/row x 10M) then exceeds — so the *absolute* bound
   holds at both scales but the *flatness* claim does not.
5. **Second operator (`nodes_columnar`/compacted SF1)**: **not run** —
   scope/time limitation, see above; the bonus synth-1m/10m numbers above
   are informational only and should not be read as satisfying this check.

**Overall: PARTIALLY REFUTED.** All three primary absolute bounds pass
decisively (memory, wall, digest), matching the design's central claim that
`version_history` at 10M becomes cheap in absolute terms. The RATIO
falsifier is refuted, which specifically undercuts the "near-flatness"
sub-claim — the win is real but not flat across scale the way SS4 predicted.

## Erratum (2026-09-15, coordinator)

The "95.2x faster" in this README and in the record's `falsifiers.*.measured`
prose is a mis-division: the record's own medians give 179,250.1 ms /
1,866.8 ms = **96.0x** (the 10.9x memory figure is correct: 13,414,332 /
1,229,724 KB). The JSON record is left byte-identical (its digest is of
record); `scripts/osdi_paper_macros.py::osdiVhWall*` recomputes the ratio
from the raw reps and is the number the paper uses.
