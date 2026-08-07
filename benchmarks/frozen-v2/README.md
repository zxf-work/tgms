# frozen-v2 — the scale-ladder task suites (D-091)

> **Status (2026-08-07, D-094): legacy development / regression corpus —
> known guardrail-conditioned task-selection bias.** Gold for these
> suites was generated through the production guardrail, so the system
> itself influenced which hard tasks received gold (the
> `generation_census` in each suite records the per-template losses; the
> discarded draws themselves were not retained). These suites remain
> valid for reproducing the Session-4 results, regression checks, and
> mechanism validation on the surviving tasks — the surviving gold is
> not wrong; the defect is selection. They must NOT support population
> claims ("answers X% of the workload at scale"), refusal-behavior
> measurements, or cross-scale coverage comparisons. The successor is
> the oracle-lane corpus (oracle-v3, M1 of the evidence-semantics
> program): full task inventory, every draw a row, oracle-lane gold
> where obtainable, explicit `oracle_status` and admission labels.
> This directory is preserved permanently and never overwritten.

Canonical task suites for the agent-interface campaign, generated
2026-08-07 on the iTiger canonical stores at commit `b2207e4`, seed 0:

| dataset | events | dev/test | test_split_sha |
|---|---:|---|---|
| sx-mathoverflow | 506,550 | 22/94 | `836463af92a7…` |
| sx-superuser | 1,443,339 | 22/94 | `e35f26f32709…` |
| wiki-talk | 7,833,140 | 22/94 | `932f3901dffc…` |

Full SHAs are recorded in the decision log (D-091). Each suite carries a
`generation_census` — per-template counts of kept / gold-failed /
empty-set outcomes. **Read it before comparing across scales**: gold is
executed through the same guardrail the evaluation uses, so templates
whose plans are inadmissible at a given scale drop out of that suite,
and the composition shift is data, not noise (four reachability/motif
templates generate nothing at 7.8M).

The canonical stores (probe corrections included) live on iTiger at
`/project/xzhang12/tgms/stores/`. Unlike frozen-v1, the event logs are
not committed here — wiki-talk's alone is ~7.8M records — so the suites
must be evaluated against those stores or replicas replayed from their
event logs (never independently re-ingested; transaction times and
derived ids differ, D-023).
