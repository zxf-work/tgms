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
