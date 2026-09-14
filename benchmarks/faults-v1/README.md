# faults-v1 — the trust-boundary fault matrix (Lane E, task E1)

This directory holds records from `scripts/eval_trust_boundary_matrix.py`,
the driver for `docs/design/TRUST_BOUNDARY_FAULT_MATRIX_DESIGN_2026-09-13.md`
(FROZEN): the plan-level (F1) and execution-level (F2) fault-injection
matrix and its four-way outcome classifier
(`tgms.eval.plan_faults.classify`).

**Records here come only from the pre-registered remote campaign — never
from a dev-host smoke or demo run.** The design's §5/§8 pre-registration
(sample sizes, seeds, the falsification bar) binds before any run whose
output is meant to stand as evidence; a run on a developer's own machine,
against a locally-built `stores/collegemsg` or `stores/ldbc-fixture`, is
for exercising the driver during implementation and review, not a record.
Such runs must not be committed under this path. (The E1 implementation
was itself verified this way — see the task's own report — and none of
those runs are checked in here.)

## File naming

`<cell>-<suite>.json`, one file per `(cell, suite)` pair, e.g.
`F1-1-collegemsg.json`. `<cell>` is a key of
`tgms.eval.plan_faults.CELL_REGISTRY` (`F1-1` .. `F1-11`, `F2-1` .. `F2-7`,
plus `F1-6b`, the memo's own directed "run it as a separate cell" oracled
sub-case of F1-6 — see that module's docstring). `<suite>` is a frozen
suite name (`collegemsg`, `emaileu`) or `ldbc-fixture` for surface-B runs
against the 29 runnable TGIR-v1 plans.

## Record shape

Each file is one manifest (`scripts/eval_trust_boundary_matrix.py
:build_manifest`):

| field | meaning |
|---|---|
| `generated`, `timestamp_utc` | UTC timestamp of the run |
| `git_commit` | the commit the driver ran at |
| `machine` | `{host, platform, cpus}` |
| `seed` | the `random.Random` seed (§5: 0-4) |
| `dataset` | `{name, digest}` — sha256 of the suite file |
| `protocol` | which arm produced the record — `"model-free (primary arm, section 5)"`, or the coordinator's Addendum 1 secondary arm (`--strict-gate`) |
| `surface` | `A` (agent plan-DAG) or `B` (TGIR plan) |
| `cell` | the cell id |
| `n_requested`, `n_cases` | requested vs. actually-produced trial count (a gap flags a high mutator "not applicable" rate — §8's falsification bar: >30% is reported, not hidden) |
| `outcomes` | `{n, counts, gold_mismatch, misattributed}` — the per-cell histogram over `correct` / `safe-refusal` / `explicit-failure` / `silent-violation` |
| `false_refusal_rate` | safe-refusals on the control (unmutated) arm, when measured for that cell |
| `per_case` | one row per trial: `outcome`, `outcome_reason`, `invariants_violated`, `gold_mismatch`, `misattributed`, `weak_support` (Addendum 1: `true` when the classified answer rests on a claim capped `weakly_supported` by truncated evidence — reported as a flag, not a reclassification; §4's outcome stays whatever `classify` returned) |
| `result_digest` | sha256 of `canonical_json(per_case)` |

## Gating arms (coordinator ruling, Addendum 1 to the frozen design,
2026-09-13)

The **primary** arm classifies the answer the deployed system actually
delivers: `gate_answer`'s default drops only `verdict == "unsupported"`,
identical to `tgms.eval.harness.run_task_ours`'s own gate. An emitted
`unverifiable` claim the evidence does not support is therefore a genuine
`I1` `silent-violation` under this arm when it reaches classification —
that is a finding to report, not a condition to pre-empt by widening the
gate. `--strict-gate` is a named **secondary** arm (drops `unsupported`
*and* `unverifiable`) kept for contrast, not as the headline reading.

---

## The pre-registered campaign — 2026-09-13 (Lane E, task E-campaign)

**Headline: the §8 "zero silent violations" claim is FALSIFIED by this
campaign.** 271 of 300 F1-9 (`wrong_step_citation`) trials are classified
`I1` `silent-violation` under the primary arm — reproducible (seed 0),
reported verbatim below with every trial's `per_case` index, never rerun
to make it disappear. This is not a classifier bug: the shipped production
gate (`tgms.eval.harness.run_task_ours`, replicated here by `gate_answer`'s
default) drops only `verdict == "unsupported"`, and these 271 claims are
emitted with `verdict == "unverifiable"` — a claim whose evidence pointer
was re-pointed to a step of a *different* run and never resolves — which
the production gate does not drop. Per Addendum 1: *"An emitted
`unverifiable` claim that the recorded evidence does not support is
classified `silent-violation` of I1 — a finding, not a pre-fix... If the
primary arm shows violations, the gate is fixed by a separate ruling and
the matrix re-run; both results are kept."* This record is that primary-arm
finding; the gate fix and re-run, if the coordinator orders one, is a
separate task.

The other headline number worth stating up front: **the model-free primary
arm ran 5,704 of the 7,000 pre-registered-scale trials it attempted** (296
short — see Shortfalls below, all structural corpus-size limits, not
random mutator noise), across 19 surface-A cells (CollegeMsg) and 4
surface-B cells (the 29 runnable TGIR-v1 plans on the LDBC-shaped fixture),
each also run under the `--strict-gate` secondary arm except the two cells
(`F1-8`, `F2-5`) the driver's own docstring documents as gate-independent.

- **commit**: `4af2181`
- **hosts**: itiger01 (16 tasks), itiger02 (4 tasks), itiger04 (20 tasks) —
  iTiger cluster, partition `bigTiger`
- **array job**: 211007, `--array=0-39%6`, all 40 tasks `COMPLETED`, exit
  `0:0` — wall-clock per task 1-29s, whole array well under 5 minutes
- **engine**: rebuilt from source on itiger07 via `srun` +
  `uv sync --extra agent --reinstall-package tgms` before the campaign;
  confirmed `tgms._engine.MANIFEST_FORMAT_VERSION == 2` (the checkout's
  previous `.so`, built Aug 28, predates the B1/B2 manifest-format-2 /
  `version_page` changes on `main`)
- **manifest**: `fault-matrix-campaign-2026-09-13.json` conforms to
  `benchmarks/schema/result_manifest.schema.json`
  (`scripts/check_result_manifest.py` — passes)
- **result_digest** (sha256 over the 40 constituent records'
  `(cell, surface, strict_gate, result_digest)`):
  `8c6631f22c39e4c295466eff278d2ab708e222c7ed71a6f102080f8590ec4314`
- **file sha256** (`fault-matrix-campaign-2026-09-13.json`):
  `ce21527d4815b0abd050f8bb41fa53dd32ccc69908fc2f01f5094b6caefd3a9f`

### Out of scope: email-eu

`benchmarks/frozen-v1/` carries a raw event log only for CollegeMsg
(`collegemsg.eventlog.jsonl`); there is no equivalent for email-eu.
`tgms/data/loaders.py`'s `"email-eu"` entry downloads
`email-Eu-core-temporal.txt.gz` from SNAP, and a fresh `tgms ingest`
reassigns transaction times (D-023), so it cannot reproduce
`suite-emaileu.json`'s frozen gold. §5's "email-eu 94" arm is therefore
**not run** in this campaign. Surface B (the LDBC-shaped fixture) stands
in as the second injection surface instead, per the design's own §1.

### Per-cell outcome table (both gates)

`n` is `n_cases`/`n_requested`. Surface `B` rows are the 29 runnable
TGIR-v1 plans against the LDBC-shaped fixture (`stores/ldbc-fixture`,
freshly built via `scripts/build_ldbc_fixture.py` on iTiger — it did not
exist there before this campaign). `--strict-gate` is a documented no-op
for `F1-8`/`F2-5`/surface B (the driver's own module docstring), so those
rows have no strict counterpart — running one would just reproduce the
primary-arm file byte-for-byte.

| cell | surface | suite/plans | gate | n | correct | safe-refusal | explicit-failure | silent-violation | gold_mismatch | misattributed | weak_support |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| F1-1 | A | collegemsg | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-1 | B | ldbc-fixture | primary | 100/100 | 92 | 0 | 8 | 0 | 0 | 0 | 0 |
| F1-1 | A | collegemsg | strict | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-2 | A | collegemsg | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-2 | A | collegemsg | strict | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-3a | A | collegemsg | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-3a | B | ldbc-fixture | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-3a | A | collegemsg | strict | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-3b | A | collegemsg | primary | 100/100 | 82 | 0 | 18 | 0 | 62 | 0 | 0 |
| F1-3b | B | ldbc-fixture | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-3b | A | collegemsg | strict | 100/100 | 82 | 0 | 18 | 0 | 62 | 0 | 0 |
| F1-4 | A | collegemsg | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-4 | A | collegemsg | strict | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-5 | A | collegemsg | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-5 | B | ldbc-fixture | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-5 | A | collegemsg | strict | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-6 | A | collegemsg | primary | 0/100 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| F1-6 | A | collegemsg | strict | 0/100 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| F1-6b | A | collegemsg | primary | 0/100 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| F1-6b | A | collegemsg | strict | 0/100 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| F1-7 | A | collegemsg | primary | 300/300 | 0 | 0 | 300 | 0 | 0 | 0 | 0 |
| F1-7 | A | collegemsg | strict | 300/300 | 0 | 0 | 300 | 0 | 0 | 0 | 0 |
| F1-8 | A | collegemsg | primary | 0/300 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| F1-9 | A | collegemsg | primary | 300/300 | 29 | 0 | 0 | **271** | 46 | 300 | 0 |
| F1-9 | A | collegemsg | strict | 300/300 | 29 | 0 | 271 | 0 | 29 | 29 | 0 |
| F1-10 | A | collegemsg | primary | 300/300 | 0 | 0 | 300 | 0 | 0 | 0 | 0 |
| F1-10 | A | collegemsg | strict | 300/300 | 0 | 0 | 300 | 0 | 0 | 0 | 0 |
| F1-11 | A | collegemsg | primary | 2/300 | 2 | 0 | 0 | 0 | 0 | 0 | 2 |
| F1-11 | A | collegemsg | strict | 2/300 | 2 | 0 | 0 | 0 | 0 | 0 | 2 |
| F2-1 | A | collegemsg | primary | 300/300 | 0 | 11 | 289 | 0 | 0 | 0 | 0 |
| F2-1 | A | collegemsg | strict | 300/300 | 0 | 11 | 289 | 0 | 0 | 0 | 0 |
| F2-2 | A | collegemsg | primary | 100/100 | 0 | 6 | 94 | 0 | 0 | 0 | 0 |
| F2-2 | A | collegemsg | strict | 100/100 | 0 | 6 | 94 | 0 | 0 | 0 | 0 |
| F2-3 | A | collegemsg | primary | 100/100 | 54 | 8 | 6 | 32 | 18 | 0 | 0 |
| F2-3 | A | collegemsg | strict | 100/100 | 54 | 8 | 6 | 32 | 18 | 0 | 0 |
| F2-4 | A | collegemsg | primary | 300/300 | 201 | 57 | 42 | 0 | 27 | 0 | 18 |
| F2-4 | A | collegemsg | strict | 300/300 | 201 | 57 | 42 | 0 | 27 | 0 | 18 |
| F2-5 | A | collegemsg | primary | 100/100 | 0 | 100 | 0 | 0 | 0 | 0 | 0 |
| F2-6 | A | collegemsg | primary | 300/300 | 174 | 18 | 108 | 0 | 21 | 0 | 0 |
| F2-6 | A | collegemsg | strict | 300/300 | 174 | 18 | 108 | 0 | 21 | 0 | 0 |

Full per-trial rows live in the 40 companion raw files (`<cell>-<suite>
[-strict].json`, unmodified copies of what the driver wrote on iTiger) and
are re-aggregated (never re-typed) into the merged manifest's
`per_cell_gate_table` / `silent_violations` by
`scripts/fault_matrix_campaign_merge.py`.

### Silent violations, verbatim (both gates, every cell — none rerun)

**F1-9, primary gate, headline: 271/300.** Every index below is that
trial's position in `F1-9-collegemsg.json`'s `per_case` array (this cell's
trial rows carry no separate `task_id` — see that file for the full row,
including `mutator: "wrong_step_citation"` and
`invariants_violated: [{"invariant": "I1", "detail": "provenance pointer
names an uncited step"}]` on every one of them):

```
0, 1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26,
28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 53,
54, 55, 56, 58, 59, 60, 61, 62, 63, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 77, 78, 79, 80,
81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 97, 99, 101, 102, 103, 104, 105, 106,
108, 109, 110, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 125, 126, 128, 129,
130, 131, 132, 133, 134, 136, 137, 138, 139, 140, 141, 142, 143, 144, 145, 146, 147, 148, 149,
150, 151, 152, 153, 154, 155, 156, 157, 158, 159, 160, 161, 162, 163, 164, 165, 167, 168, 169,
170, 171, 172, 173, 174, 175, 177, 178, 180, 181, 182, 183, 184, 185, 186, 187, 188, 189, 190,
191, 192, 193, 194, 195, 196, 197, 199, 200, 201, 202, 203, 204, 205, 207, 208, 209, 210, 212,
213, 214, 215, 216, 217, 218, 219, 220, 221, 222, 223, 224, 225, 226, 227, 228, 229, 230, 231,
233, 234, 235, 236, 237, 238, 240, 241, 242, 243, 244, 245, 246, 247, 248, 250, 251, 252, 253,
254, 255, 256, 257, 258, 259, 260, 261, 262, 263, 264, 265, 266, 268, 269, 270, 271, 272, 273,
274, 275, 276, 277, 278, 279, 280, 281, 282, 283, 285, 286, 287, 289, 290, 291, 292, 293, 295,
296, 297, 298, 299
```

The complementary 29 indices (`7, 27, 41, 52, 57, 64, 76, 95, 96, 98, 100,
107, 111, 124, 127, 135, 166, 176, 179, 198, 206, 211, 232, 239, 249, 267,
284, 288, 294`) are the `correct` + `misattributed: true` trials §2's
original reading anticipated for *all* undetected `wrong_step_citation`
cases — the value also appears in the wrongly-cited payload, so the claim
stays supported by the evidence it cites. Under strict gating
(`F1-9-collegemsg-strict.json`) these same 29 indices are the only
`correct` trials; the other 271 become `explicit-failure`
(`E_GATED_EMPTY` — the claim's only citation is unverifiable and strict
gating drops it before delivery, leaving no claim to deliver). The design's
own citation (`docs/site_facts.json`: "36/100") is a different corpus/N and
is not directly comparable to this campaign's 29/300 (9.7%) strict-gate
detection rate — noted, not reconciled here.

**F2-3, both gates, non-headline (assumption A2, excluded by
`CELL_REGISTRY["F2-3"].headline`): 32/100, identical trial set under both
gates** (F2-3's outcome is force-classified from injected ground truth
regardless of gate). Task ids (from `F2-3-collegemsg.json` /
`F2-3-collegemsg-strict.json`, `per_case` indices in parentheses):

```
collegemsg-t4-coactive_count-009 (0), collegemsg-t1-burst_buckets-018 (8),
collegemsg-t1-partners_window-030 (11), collegemsg-t4-first_reached_history-015 (13),
collegemsg-t1-burst_buckets-055 (14), collegemsg-t1-busiest_bucket-004 (20),
collegemsg-t1-earliest_arrival_at-038 (23), collegemsg-t3-first_burst-021 (25, 29, 37),
collegemsg-t1-reach_first-022 (26, 64), collegemsg-t4-coactive_count-003 (27),
collegemsg-probe-now-002 (31), collegemsg-t1-reach_first-001 (42),
collegemsg-t1-busiest_bucket-004 (43), collegemsg-t1-burst_buckets-029 (44),
collegemsg-t4-first_reached_history-000 (49), collegemsg-t1-busiest_bucket-044 (50, 83),
collegemsg-t3-first_burst-009 (53), collegemsg-t1-partners_window-019 (55),
collegemsg-t1-reach_count-058 (59), collegemsg-t3-first_burst-003 (62),
collegemsg-probe-now-003 (67), collegemsg-t1-reach_count-049 (73),
collegemsg-t1-burst_buckets-039 (78), collegemsg-t1-reach_first-012 (80),
collegemsg-t1-earliest_arrival_at-007 (81), collegemsg-t3-first_burst-023 (85),
collegemsg-t1-partners_window-056 (89), collegemsg-t3-first_burst-017 (98)
```

Every one of these carries `reason: "assumption-A2-exercised (silent
partial result, undetectable by construction)"` — exactly the outcome §3
pre-registers ("F2-3 lies outside the trust model... Run it, expect a
silent violation... excluded from the headline count").

No other cell produced any `silent-violation` trial under either gate.

### Shortfalls (n_cases < n_requested) — all structural, none a mutator fluke

Every shortfall below was independently confirmed by probing the relevant
mutator/candidate-filter directly against the frozen `suite-collegemsg.json`
corpus (500+ draws each, or an exhaustive scan) — none is an artifact of
attempt-budget exhaustion at the campaign's own `max_attempts`:

- **`F1-6` / `F1-6b` — 0/100, both gates.** The "semantically wrong
  operator" mutator (swap for another registered op with a compatible arg
  schema) found **zero** applicable substitution sites across all 116
  `oracle_plan` tasks in `suite-collegemsg.json` (dev 22 + test 94),
  confirmed over 500 independent draws per cell. No trial ever ran; this
  is not evidence for or against the §8 "zero detections predicted"
  reading — there is nothing to detect.
- **`F1-8` — 0/300, primary only** (strict is a documented no-op for this
  cell, never run). `run_f1_8_cell`'s candidate filter (a rows-producing
  step under a `limit` arg, feeding a reducing `compute` step) matched
  **zero of 116** `oracle_plan` tasks — confirmed by direct enumeration.
  §8 predicts a refusal via `E_INCOMPLETE` / `executor.py:204-210` *if*
  such a plan shape exists in the corpus; none does, so this cell is
  **unmeasured** against CollegeMsg, not zero-refusal-rate.
- **`F1-11` — 2/300, both gates.** `truncated_count_cases` (the same
  candidate-selection logic `faults_ext.py` already uses) found exactly
  **2 of 116** tasks with a paginated-count claim shape, confirmed by
  direct enumeration against the live store. Both trials landed `correct`
  + `weak_support: true`, qualitatively matching §8's "100% capped to
  `weakly_supported`" prediction, but at 2 trials this campaign has none
  of the pre-registered N=300 statistical power for that cell.

### §8 pre-registration: predicted vs. measured

| prediction (§8) | cells | measured | verdict |
|---|---|---|---|
| 100% explicit-failure at `validate_static` (syntactic, exhaustive) | F1-2, F1-4, F1-5 | 100/100 explicit-failure, every surface run | **match** |
| F1-1: 100% when `task_input_uids` supplied (surface A); "unknown" on surface B (no grounding rule — an open cell) | F1-1 | surface A: 100/100 explicit-failure. Surface B: 92/100 `correct`, 8/100 explicit-failure | **surface A matches; surface B confirms the open cell** — TGIR envelopes carry no claims for I2 to check, so a fabricated id mostly reaches `correct` unchallenged |
| 100% (`E_INVALID_ARG`) | F1-3a | 100/100 explicit-failure, both surfaces | **match** |
| ≥95% detection, matching the existing 500/500 | F1-7, F1-10 | 300/300 (100%) explicit-failure, both | **match / exceeds** |
| 100% capped `weakly_supported`; 0% under ablation; ~15/0 reference | F1-11 | 2/2 `correct` + `weak_support`, but N=2 not 300 | **qualitatively consistent, statistically underpowered** (corpus has only 2 eligible tasks) |
| refusal via `E_INCOMPLETE` / executor guard | F1-8 | 0/300 — no eligible base plan in the corpus | **unmeasured**, not confirmed or refuted |
| explicit-failure or certificate | F2-1, F2-5, F2-6 | F2-1: 289 fail + 11 refusal (300/300); F2-5: 100/100 refusal; F2-6: 108 fail + 18 refusal + 174 `correct` (§2 explicitly allows `correct` here — "or correct if a scan fallback reproduces O-ENV") | **match** |
| "zero detections predicted and zero I-violations *claimed*" (excluded from the headline, not necessarily zero raw hits) | F1-3b, F1-6, F2-3 | F1-3b: 82 `correct` (gm=62) + 18 explicit-failure, 0 silent-violation; F1-6/F1-6b: 0 trials (no eligible site); F2-3: 32/100 silent-violation both gates, exactly the pre-registered "assumption-A2-exercised" case | **match** (F2-3's silent-violations are the pre-registered exception, not a surprise) |
| *(no explicit §8 number — the design's §2 body text expected the undetected `wrong_step_citation` cases to remain `supported`, i.e. a provenance miss, not an I1 violation)* | **F1-9** | primary: 271/300 `silent-violation` (I1) + 29/300 `correct`; strict: 29/300 `correct` + 271/300 `explicit-failure` | **the §2 body text's expectation does not hold under the coordinator's Addendum 1 gate ruling** — see the headline note above. This is the campaign's central finding. |

The exact paper wording §8 proposes ("the pipeline produced zero
evidence-relative silent violations...") **cannot be used as written** for
the primary arm on this record: F1-9 supplies a reproducible, seed-pinned,
non-F2-3 counterexample. The `--strict-gate` secondary arm's table has
silent-violation trials in exactly one cell, `F2-3` (32/100, the
pre-registered assumption-A2 exception every gate reproduces identically
since its classification is forced from injected ground truth, not from
gating) — every headline cell is silent-violation-free under strict
gating, including `F1-9`. The "zero (headline) silent violations" claim
therefore holds for the `--strict-gate` secondary arm but not for the
primary arm that measures the deployed system, and the gap between the two
arms — 271 F1-9 trials — *is* the reportable result.

### Protocol

`scripts/fault_matrix_campaign.slurm` ran a 40-task array (`--array=0-39%6`)
against a checkout fast-forwarded to `4af2181`
(`/project/xzhang12/tgms`, `HEAD_BEFORE=02228fc… want=4af2181` →
`HEAD_AFTER=4af2181…`), engine rebuilt on itiger07 via `srun` +
`uv sync --extra agent --reinstall-package tgms` (confirmed
`tgms._engine.MANIFEST_FORMAT_VERSION == 2` afterward). Each array task is
one `(cell, surface, gate)` triple: 19 surface-A cells × primary gate, 17
of those 19 again × `--strict-gate` (excluding the two cells the driver's
own docstring calls gate-independent, `F1-8`/`F2-5`), and 4 surface-B
cells × primary gate only (`--strict-gate` is documented as a no-op on
surface B too). Same base seed (0) for every task, so the primary and
strict arms of one cell draw the identical mutation/task-selection
sequence and differ only in which claims the gate drops before
classification.

Two deviations from a naive "run the harness on /project" script, both
confirmed by smoke-testing this exact script (`--n 2` on itiger07 via
`srun`) before the real campaign:

1. **The store each task reads is copied node-local
   (`/tmp/tgms-faultmatrix-$SLURM_JOB_ID/stores/…`) before the run, and
   `TMPDIR` is node-local too** — not `/project`. Reason specific to this
   campaign (beyond crash-v1's NFS-sillyrename precedent): `tgms.store.Store`
   (opened by the driver's `resolve_store()`, never `read_only=True`) takes
   an OS-level `fcntl.flock` single-writer lock on `<store>/writer.lock`
   for the whole process lifetime, even though every trial here only
   reads. With up to 6 array tasks running concurrently
   (`--array=...%6`), 5 of 6 would hit `WriterLockedError` immediately
   against one shared `/project/xzhang12/tgms/stores/collegemsg` or
   `stores/ldbc-fixture`. Each task instead gets its own private copy
   (~18 MB collegemsg, ~16 KB ldbc-fixture) and its own writer lock;
   nothing is written back, and the copy is `rm -rf`'d on exit.
2. **A `git` shim** (iTiger compute nodes have no `git` binary — same
   finding as crash-v1) answers only `git rev-parse HEAD` from
   `$TGMS_COMMIT=4af2181`, so every raw record carries the real commit
   instead of the driver's own graceful "n/a" fallback.
3. **Surface-B output filenames.** The driver requires a `--suite` value
   that loads successfully (`load_suite`) even for surface-B cells, whose
   own computation never touches suite tasks (`run_cell`'s `surface=="B"`
   branch dispatches straight to `run_tgir_cell`). Rather than fabricate a
   `benchmarks/frozen-v1/suite-ldbc-fixture.json` stub, the campaign passes
   `--suite collegemsg` (a real file, genuinely unused for these 4 cells)
   and renames the driver's output file from `<cell>-collegemsg.json` to
   `<cell>-ldbc-fixture.json` after the run, matching the file-naming
   convention this README already documented. The raw file's own internal
   `dataset.name`/`digest` fields are left exactly as the driver wrote them
   (unmutated, per this project's habit of never silently rewriting a
   record); `scripts/fault_matrix_campaign_merge.py` derives the correct
   effective `(suite, surface)` from the filename instead of trusting
   those two fields for cells that support both surfaces.

### Data handling

The 40 raw per-task records (and per-task stdout logs, and per-task
host/uname sidecars) were staged at
`/project/xzhang12/faults-v1-work/{records,logs,node_meta}/`, sha256-summed
there, scp'd down, and **the sha256sums were re-checked locally before the
server-side copies were deleted** (`rm -rf /project/xzhang12/faults-v1-work`
— confirmed gone). Total server-side footprint at peak: 3.7 MB, well under
the 2 GB campaign budget. `scripts/fault_matrix_campaign_merge.py` never
hand-types a number: every count in this README and in the merged manifest
is read back out of the 40 committed raw files.

### Regenerating

```sh
# 1. On iTiger: fast-forward /project/xzhang12/tgms to the target commit,
#    rebuild the engine on a compute node (uv sync --extra agent
#    --reinstall-package tgms via srun), confirm MANIFEST_FORMAT_VERSION,
#    and build stores/ldbc-fixture if missing
#    (scripts/build_ldbc_fixture.py --out stores/ldbc-fixture).
# 2. Smoke-test: srun ... eval_trust_boundary_matrix.py --cells F1-1 --n 2
#    --store stores/collegemsg --out <scratch>.
# 3. Submit the campaign:
sbatch scripts/fault_matrix_campaign.slurm
# 4. Poll: sacct -j <jobid> --format=JobID,State,Elapsed,ExitCode -X
#    (or watch /project/.../faults-v1-work/logs/task-<id>.out for the
#    RUN_STARTED / TASK_DONE lines).
# 5. Once all 40 tasks show ExitCode 0:0, scp records/ + logs/ + node_meta/
#    down, sha256 verify, THEN delete the server-side copies.
# 6. Merge:
python scripts/fault_matrix_campaign_merge.py \
    --records-dir <pulled>/records --node-meta-dir <pulled>/node_meta \
    --commit 4af2181 --array-job-id <jobid> \
    --out benchmarks/faults-v1/fault-matrix-campaign-<date>.json \
    --raw-out-dir benchmarks/faults-v1
# 7. Validate:
python scripts/check_result_manifest.py benchmarks/faults-v1/fault-matrix-campaign-<date>.json
```

A rerun at the same base seed against the same commit and corpus is
bit-for-bit reproducible in its trial sequence (each cell reseeds its own
`random.Random(0)`); F1-9's 271 silent-violations, F2-3's 32, and the three
structural shortfalls (F1-6/F1-6b/F1-8/F1-11) are all corpus-and-code
properties, not sampling noise, and are expected to reproduce exactly
unless the corpus, the gate ruling, or the classifier changes.

---

## Re-run under D-160 — 2026-09-15 (Lane E, task E3)

**Headline: the 2026-09-13 finding is fixed, not explained away. F1-9's
271/300 primary-arm silent-violations are 0 under the new gate — every one
of those trials now classifies `explicit-failure` instead. F2-3 is
unchanged at 32/100 (the pre-registered stated-assumption probe, outside
the headline count, unaffected by any gate by construction). No cell
outside F2-3 shows a nonzero silent-violation.** This is exactly the
`fault-matrix-campaign-2026-09-13.json` record read forward through D-160
(`docs/STABILITY.md` §9, coordinator ruling 2026-09-15): *"the trust
boundary must not emit a claim it cannot verify... the matrix re-runs
under the new production gate; both records are kept and both are
reported."* Both records remain committed side by side —
`fault-matrix-campaign-2026-09-13.json` (pre-fix, primary arm gates
`unsupported` only) and `fault-matrix-campaign-2026-09-15-d160.json`
(post-fix, primary arm gates `unsupported` AND `unverifiable`) — and
neither supersedes the other.

**Only the primary arm was re-run — no `--strict-gate` tasks.** D-160 made
`gate_answer`'s `strict` parameter a no-op alias: since the default now
already drops `unverifiable`, `strict=True` computes the identical
`GATED_VERDICTS` drop set. Re-running the former 17-task strict arm would
have reproduced this record's own primary-arm output byte-for-byte, so
`scripts/fault_matrix_campaign.slurm` was re-parameterized (commit
`f36a04d`, following `a501fce`) to a 23-task table — the original
40-task layout's primary-arm rows only (19 surface-A + 4 surface-B),
**identical cell identities and per-cell N** to the original — renumbered
`--array=0-22%6`.

**A cross-check the campaign wasn't designed to need, but got for free:**
every cell that has an old strict-gate counterpart matches this new
primary-arm record exactly — same `counts`, same `gold_mismatch`, same
`misattributed`, same `weak_support`, same `n_cases` — because D-160 made
the production gate behave exactly like the old secondary arm. (Surface B
and `F1-8`/`F2-5` have no old strict counterpart: the original campaign's
own docstring calls `--strict-gate` a no-op there and never ran it.) This
is independent confirmation that the re-run's harness, corpus, and seed
reproduce the original campaign faithfully and that the only thing that
moved is the gate.

### Particulars

- **local commit (the D-160 gate change)**: `a501fceb45e8e53cc00fa1f8d93625564ddd5d94`
  (`eval: the production claim gate drops unverifiable claims too (D-160)`
  is `c38abfc`, of the three commits on top of `ea2300b`; the slurm-script
  bugfix `f36a04d` landed after this campaign ran — see "A bug found
  mid-run" below)
- **remote checkout**: a **separate** worktree,
  `/project/xzhang12/tgms-d160`, `git worktree add ... 4af2181` off the
  existing `/project/xzhang12/tgms` (still at `4af2181`, untouched — the
  2026-09-13 record's own checkout). No push was made (none is possible
  without a remote branch); the three owned Python files were `scp`'d in
  directly and sha256-verified byte-identical to the local worktree copy
  before the campaign ran:

  | file | sha256 |
  |---|---|
  | `tgms/eval/harness.py` | `629c1b63300e44bdb76df34d19ba80c2081de6786e7e39300602ca6ceb7095ee` |
  | `tgms/agent/reporter.py` | `0275f81b4ea1b37d70bab38d011d6a1877a2ad85ef625eb45b5f5b755bb276ad` |
  | `tgms/eval/plan_faults.py` | `c4d6af8e3e8d791a1dc1d7752ac55aae66e30d644a5d89a5aafd83a38253a6fc` |

  (`reporter.py` is byte-identical to `4af2181`'s own copy — it carries no
  claim-drop rule of its own, so D-160 changed nothing in it; it is listed
  here for completeness and honesty about what was verified, not because
  its content differs.) `git diff 4af2181 HEAD -- <these three files>` on
  the local worktree touches only commit `c38abfc`, confirming the scp'd
  files carry exactly the D-160 diff and nothing else that changed on
  `main` between `4af2181` and this campaign's local branch tip.
  `tgms/_engine.cpython-312-x86_64-linux-gnu.so` and `stores/` were **not**
  copied — this change is Python-only, so the worktree reuses
  `/project/xzhang12/tgms`'s own engine build (copied in directly,
  `MANIFEST_FORMAT_VERSION == 2`, confirmed via
  `import tgms._engine; tgms._engine.MANIFEST_FORMAT_VERSION`) and its
  `stores/` directory (symlinked, read-only data unaffected by this
  change).
- **hosts**: itiger01, itiger02, itiger04 — iTiger cluster, partition
  `bigTiger`
- **array job**: `211059`, `--array=0-22%6`, all 23 tasks `COMPLETED`, exit
  `0:0` — earliest task start `2026-09-13T23:22:06`, latest end
  `2026-09-13T23:22:41`, whole array **35 seconds** wall-clock (model-free,
  deterministic, no LLM or network call anywhere in the driver)
- **`TGMS_COMMIT`**: set explicitly to the local D-160 commit sha above
  (**not** the script's own `4af2181` default) via the git shim, so every
  raw record's `git_commit` field names what actually ran, not the
  worktree's base commit
- **manifest**: `fault-matrix-campaign-2026-09-15-d160.json` conforms to
  `benchmarks/schema/result_manifest.schema.json`
  (`scripts/check_result_manifest.py` — passes)
- **result_digest** (sha256 over the 23 constituent records'
  `(cell, surface, strict_gate, result_digest)`):
  `0b623c90d128ee5c307ad39d86e0ed134afd61679a01607562b8ed23df2f9ddd`
- **dataset digest**: `c70039b579ecebe9549c420a3db9336ee73fd6cca92d8975e80cdcc3bfa373b4`
  (identical to the 2026-09-13 record's — same corpus)
- **file sha256** (`fault-matrix-campaign-2026-09-15-d160.json`):
  `868c36c18270c90740f8261cb670a3d6305ebed5a8c999eddea7ad1953fe3e95`
- **total trials**: 3,102/3,900 requested — identical to the 2026-09-13
  primary arm's own total, cell for cell (see the shortfalls note below)

### A bug found mid-run, and the fix applied after

The first version of the re-parameterized `fault_matrix_campaign.slurm`
(commit `a501fce`) folded an optional `TGMS_TAG` (default `"d160"`) into
each copied record's filename — `<cell>-<suite>-d160.json` instead of
`<cell>-<suite>.json`. `scripts/fault_matrix_campaign_merge.py` (not an
owned file for this task, left unmodified) recovers each record's
*effective* surface by stripping a `-strict` suffix off the filename and
comparing what remains to the literal string `"ldbc-fixture"`; with an
extra `-d160` in the way, every surface-B record's remainder read
`"ldbc-fixture-d160"` and would have silently merged as surface A.

This job (`211059`) ran under that pre-fix script — its 23 raw records on
iTiger really were named `task-<id>--<cell>-<suite>-d160.json`. Rather
than burn a second array job, the 23 files were `scp`'d down as-produced,
sha256-verified against the remote copies (all 23 matched — see below),
then renamed locally (`task-<id>--<cell>-<suite>-d160.json` →
`task-<id>--<cell>-<suite>.json`, content untouched) before merging. The
merge tool's `bad_commit` check and the 23-way cross-validation against
the old strict-gate reading above are exactly what would have caught a
silent B→A misclassification, and neither did — surface B's 4 cells
(`F1-1`, `F1-3a`, `F1-3b`, `F1-5`) read correctly as surface `B` in the
merged manifest and the table below. The script itself was fixed
afterward (commit `f36a04d`) so a future re-run does not need this manual
step: `TGMS_TAG` now only appears on the `RUN_STARTED` log line, never in
a record's filename.

### Per-cell outcome table (primary arm only — no `--strict-gate` cells)

`n` is `n_cases`/`n_requested`. Same corpus, same seed, same cells as the
2026-09-13 record's own primary arm — only the `silent-violation` column
for `F1-9` differs (271 → 0, those 271 trials now `explicit-failure`);
every other cell's row is bit-for-bit identical to its 2026-09-13
counterpart.

| cell | surface | suite/plans | gate | n | correct | safe-refusal | explicit-failure | silent-violation | gold_mismatch | misattributed | weak_support |
|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| F1-1 | A | collegemsg | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-1 | B | ldbc-fixture | primary | 100/100 | 92 | 0 | 8 | 0 | 0 | 0 | 0 |
| F1-2 | A | collegemsg | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-3a | A | collegemsg | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-3a | B | ldbc-fixture | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-3b | A | collegemsg | primary | 100/100 | 82 | 0 | 18 | 0 | 62 | 0 | 0 |
| F1-3b | B | ldbc-fixture | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-4 | A | collegemsg | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-5 | A | collegemsg | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-5 | B | ldbc-fixture | primary | 100/100 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| F1-6 | A | collegemsg | primary | 0/100 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| F1-6b | A | collegemsg | primary | 0/100 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| F1-7 | A | collegemsg | primary | 300/300 | 0 | 0 | 300 | 0 | 0 | 0 | 0 |
| F1-8 | A | collegemsg | primary | 0/300 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| **F1-9** | A | collegemsg | primary | 300/300 | 29 | 0 | **271** | **0** | 29 | 29 | 0 |
| F1-10 | A | collegemsg | primary | 300/300 | 0 | 0 | 300 | 0 | 0 | 0 | 0 |
| F1-11 | A | collegemsg | primary | 2/300 | 2 | 0 | 0 | 0 | 0 | 0 | 2 |
| F2-1 | A | collegemsg | primary | 300/300 | 0 | 11 | 289 | 0 | 0 | 0 | 0 |
| F2-2 | A | collegemsg | primary | 100/100 | 0 | 6 | 94 | 0 | 0 | 0 | 0 |
| **F2-3** | A | collegemsg | primary | 100/100 | 54 | 8 | 6 | **32** | 18 | 0 | 0 |
| F2-4 | A | collegemsg | primary | 300/300 | 201 | 57 | 42 | 0 | 27 | 0 | 18 |
| F2-5 | A | collegemsg | primary | 100/100 | 0 | 100 | 0 | 0 | 0 | 0 | 0 |
| F2-6 | A | collegemsg | primary | 300/300 | 174 | 18 | 108 | 0 | 21 | 0 | 0 |

### Before / after, the two rows that move

| cell | surface | old (2026-09-13) primary gate | new (D-160) primary gate |
|---|---|---|---|
| F1-9 | A | 29 correct / 0 safe-refusal / 0 explicit-failure / **271 silent-violation** | 29 correct / 0 safe-refusal / **271 explicit-failure** / **0 silent-violation** |
| F2-3 | A | 54 correct / 8 safe-refusal / 6 explicit-failure / 32 silent-violation | 54 correct / 8 safe-refusal / 6 explicit-failure / 32 silent-violation (**unchanged**) |

Every other cell in the table above is identical, count for count, to its
2026-09-13 primary-arm row — confirmed programmatically, not by
inspection (the 23-cell diff produced exactly one differing row, `F1-9`).

### Silent violations, verbatim (none rerun)

**F2-3, primary gate, non-headline (stated-assumption probe, §3/A2): 32/100.**
`per_case` indices (`benchmarks/faults-v1/d160-raw/F2-3-collegemsg.json`),
identical to the 2026-09-13 record's own F2-3 primary-arm indices (same
seed, same corpus, same force-classified-from-ground-truth mechanism,
independent of any gate):

```
0, 8, 11, 13, 14, 20, 23, 25, 26, 27, 29, 31, 37, 42, 43, 44, 49, 50, 53,
55, 59, 62, 64, 67, 73, 78, 80, 81, 83, 85, 89, 98
```

No cell outside F2-3 has a nonzero `silent-violation` count in this
record. There is nothing else to report verbatim.

### Shortfalls (n_cases < n_requested)

Identical to the 2026-09-13 record's own shortfall analysis (same corpus,
same seed, same structural limits — see that section above for the
per-mutator confirmation): `F1-6`/`F1-6b` 0/100 (no applicable operator-swap
site in the corpus), `F1-8` 0/300 (no paginated-count-over-`limit`-ed-input
plan shape in the corpus), `F1-11` 2/300 (only 2 of 116 tasks have a
paginated-count claim shape). None is a mutator-budget artifact; none
changed by re-running under the new gate, since none of these three cells
ever reaches the gate at all (0 trials) or reaches it after the claim is
already resolved (`F1-11`'s 2 trials are `weak_support: true` `correct`
under both gates, since they never carry an `unverifiable` claim).

### Companion raw files

The 23 raw per-(cell, surface) records live under
`benchmarks/faults-v1/d160-raw/`, **not** at the top level of this
directory — the original 2026-09-13 campaign's own raw files (e.g.
`F1-1-collegemsg.json`) share these exact base names, and this campaign's
copies must never overwrite them. sha256 of every file under
`d160-raw/` (unmodified copies of the sha256-verified, renamed-per-above
records; verified equal to the as-produced-on-iTiger bytes before commit):

```
3295e16e3a42d60caaa1fb79edd50a8bb725c0f6c508cb4de7803d20786f45ab  F1-1-collegemsg.json
238c64eeaa3244e73090052b405fd4f022e09a0eb757027ebc24bae6ca8cbc9e  F1-1-ldbc-fixture.json
3a72ce34f6cf97f52c52b91fa504347d81c94fd6dbb84dedf05016d621a31a7d  F1-10-collegemsg.json
6418a6fd35b0a9072bb25561eb4bb62470bd8457d7bcc9b92631d423b5e2c1b5  F1-11-collegemsg.json
dc925cba87b8aa2e251cb61296392c67623852a7a94aa819dc909799c67c17a0  F1-2-collegemsg.json
f1f769f9c0acdca9c2924c44b742c73cb00b4cdc86a2502b6ef0aace9d08ce1a  F1-3a-collegemsg.json
03caca8de60625db63fdebe3a62272fdd68af60f131fbc0f74d67f04cbc26a0d  F1-3a-ldbc-fixture.json
e14ccbf4eb02e9ef3d259b20697dec67aa85e78f32c13fe9435994ff2f2ee3dd  F1-3b-collegemsg.json
2802dc69ec4cea02452b72669021445f865289dc65c8cdfded6bc5df4292729a  F1-3b-ldbc-fixture.json
93eb82c599fc01ab53501df6920e895eeffed9d09f1c57de2044d99d660e43da  F1-4-collegemsg.json
3fcbb73370bdc54cc5bde664e5c1e6f36be8c191fe9541232e226fe8da4c902f  F1-5-collegemsg.json
b325447029c97f245b1163981be9cd2e5b5830910950390d590d92a4c0b3ca67  F1-5-ldbc-fixture.json
06816aacf300ae669f8ca68ba7355865f240bfc99e29edbc2b59c43dd6e59fb0  F1-6-collegemsg.json
c3cc0bcf0bdbcc15de680243e56af78f8270097379568ea73454c61ddb9b068c  F1-6b-collegemsg.json
84d46d0ba8e14fbfc84852434a81667088344341def166311e733909370cbfd7  F1-7-collegemsg.json
98071d19791cc44989325d3aaef6d76dc05baa5db3cb1db9267842e3fa7fd137  F1-8-collegemsg.json
357a2301757af016171efbc614fe7b94dd5865de6ed1cabc521be0dea576872d  F1-9-collegemsg.json
042fa1f99369defb264ed89aacf193caafaa7c23ed898b21b11c879000b329a0  F2-1-collegemsg.json
3d75e183b35d86e4810595d5a4a0905c718ee4aaf6c029149f297c16e4930c00  F2-2-collegemsg.json
7dbcf4b318b4b364e7766a3fc975979348878fbf25e0d732f5c2c2ceaa3c54f8  F2-3-collegemsg.json
c70d25dfc6758c9fd2c1236d47a36b9e7e9232ebd37d42af3defb05e330ea1b6  F2-4-collegemsg.json
9dd1dc6bc7304c87905128f3ec2ce5a46672f8f7d647cf72282ac2c96b1bda77  F2-5-collegemsg.json
21dce76822151e1a95af2eb21cb2d15d0036c266598553c0f95df55e102eef87  F2-6-collegemsg.json
```

### Data handling

The 23 raw per-task records, per-task stdout logs, and per-task
host/uname sidecars were staged at
`/project/xzhang12/faults-v1-work-d160/{records,logs,node_meta}/`
(a directory distinct from the 2026-09-13 record's own
`faults-v1-work/`, per this script's `TGMS_STAGE` parameterization),
`scp`'d down, sha256-verified identical to the remote copies (all 23
records matched — table above), renamed to strip the pre-fix `-d160`
filename suffix (see "A bug found mid-run" above; content unchanged,
verified by sha256 before and after), merged, and **the server-side
staging directory was then deleted**
(`rm -rf /project/xzhang12/faults-v1-work-d160` — confirmed gone). The
separate worktree `/project/xzhang12/tgms-d160` was left in place on
iTiger (it is code, not campaign data, and other Lane E work may reuse
it); it is not part of this repository's own commit. As with the
2026-09-13 record, `scripts/fault_matrix_campaign_merge.py` never
hand-types a number: every count in this section and in the merged
manifest is read back out of the 23 committed raw files.

### Regenerating

```sh
# 1. On iTiger: create a SEPARATE worktree off the target base commit
#    (do not touch the checkout the 2026-09-13 record depends on):
#    git -C /project/xzhang12/tgms worktree add /project/xzhang12/tgms-d160 4af2181
# 2. scp the D-160-owned Python files in and sha256-verify:
#    tgms/eval/harness.py, tgms/agent/reporter.py, tgms/eval/plan_faults.py
#    Copy in (do not rebuild) the existing engine .so and symlink stores/
#    from /project/xzhang12/tgms -- this change is Python-only.
# 3. Submit the primary-arm-only campaign:
sbatch --job-name=tgms-faultmatrix-d160 \
  --export=ALL,TGMS_REPO=/project/xzhang12/tgms-d160,\
TGMS_STAGE=/project/xzhang12/faults-v1-work-d160,TGMS_TAG=d160,\
TGMS_COMMIT=<local D-160 commit sha> \
  scripts/fault_matrix_campaign.slurm
# 4. Poll: sacct -j <jobid> --format=JobID,State,Elapsed,ExitCode -X
# 5. Once all 23 tasks show ExitCode 0:0, scp records/ + logs/ + node_meta/
#    down, sha256 verify, THEN delete the server-side copies.
# 6. Merge:
python scripts/fault_matrix_campaign_merge.py \
    --records-dir <pulled>/records --node-meta-dir <pulled>/node_meta \
    --commit <local D-160 commit sha> --array-job-id <jobid> \
    --out benchmarks/faults-v1/fault-matrix-campaign-<date>-d160.json \
    --raw-out-dir benchmarks/faults-v1/d160-raw
# 7. Validate:
python scripts/check_result_manifest.py \
    benchmarks/faults-v1/fault-matrix-campaign-<date>-d160.json
```

At the same base seed, corpus, and commit this reproduces bit-for-bit:
F1-9's 0 silent-violations (271 explicit-failure instead), F2-3's 32
(same 32 `per_case` indices), and the same three structural shortfalls.
