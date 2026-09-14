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
