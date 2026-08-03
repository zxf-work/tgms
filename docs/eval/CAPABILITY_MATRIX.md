# Capability matrix: six systems, one registry

Verdicts per `docs/eval_semantics.md`: **eq** = equivalent (canonical hash
matches the operators, verified before timed); **gr** = guardrailed (TGMS
declines by policy at that scale; baselines answer). Thirteen queries since
D-044 added grouped aggregation — whose twins were written *before* the
operator raced them, in SQL twice and in Cypher once. No cell in the matrix
is `unsupported` or `approximated` — the expressiveness gap this evaluation
was designed to expose does not exist at the registry's scope, on any of
the five non-native systems. That is a finding, not a disclaimer.

**And it is a finding about this registry, which we wrote.** Measured
against a workload nobody here chose, the same comparison runs the other
way: of LDBC SNB's 41 read templates, the operator algebra expresses 3, and
35 of the 38 it cannot express are blocked by labelled pattern matching and
property projection — capabilities every system in the table above has and
TGMS deliberately does not. `docs/eval/EXTERNAL_BENCHMARKS.md` has the
query-by-query classification. Both facts are true, and quoting the first
without the second would be the kind of scope error this project keeps
finding in its own claims.

| query | native | duckdb | postgres | clickhouse | neo4j | memgraph |
|---|---|---|---|---|---|---|
| hist.single | eq | eq | eq | eq | eq | eq |
| hist.asof | eq | eq | eq | eq | eq | eq |
| snap.hop2 | eq | eq | eq | eq | eq | eq |
| diff.global | eq | eq | eq | eq | eq | eq |
| reach.window | eq/gr¹ | eq/gr¹ | eq | eq | eq | eq |
| paths.k | eq | eq | eq | eq | eq | eq |
| series.count | eq | eq | eq | eq | eq | eq |
| burst.zscore | eq | eq | eq | eq | eq | eq |
| nbr.evolution | eq | eq | eq | eq | eq | eq |
| coactive.narrow | eq | eq | eq | eq | eq | eq |
| resolve.substr | eq | eq | eq | eq | eq | eq |
| agg.rel_bucket | eq | eq | eq | eq | eq | eq |
| motif.filtered | eq | eq | eq | eq | eq | eq |

¹ `reach.window` is guardrailed at 10M only (`E_COST`); PostgreSQL and
ClickHouse bracket the refused work at 44 s / 4 s, which is why that
refusal is the guardrail working rather than a defect. **`paths.k` is no
longer guardrailed at any scale** — the cost-model false positive this
footnote used to track was closed by pricing the DFS frontier (D-039);
native answers at 10M in 16.2 ms, fastest of the four.

Structural capabilities, for the comparison's scope:

| capability | native | duckdb | postgres | clickhouse | neo4j | memgraph |
|---|---|---|---|---|---|---|
| implements write semantics (corrections, retractions) | yes | yes | no — loader only | no — loader only | no — loader only | no — loader only |
| bi-temporal visibility in every query | yes | yes | yes (SQL threads it) | yes | yes | yes |
| cost guardrails (`E_COST`) | yes | yes | n/a | n/a | n/a | n/a |
| byte-identical replay digest | yes | yes | n/a | n/a | n/a | n/a |
| compression at rest | yes (D-032/33) | no | no (heap) | yes (lz4) | no | n/a (in-memory) |

Where TGMS is *narrower* than the baselines (recorded for symmetry):
string-only current-canonical name resolution (D-031), single writer, a
fixed five-shape motif catalogue, and a closed scan signature — see
`docs/eval_semantics.md` §6.
