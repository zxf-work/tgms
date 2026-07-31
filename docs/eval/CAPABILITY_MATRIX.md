# Capability matrix: six systems, one registry

Verdicts per `docs/eval_semantics.md`: **eq** = equivalent (canonical hash
matches the operators, verified before timed); **gr** = guardrailed (TGMS
declines by policy at that scale; baselines answer). No cell in the matrix
is `unsupported` or `approximated` — the expressiveness gap this evaluation
was designed to expose does not exist at the registry's scope, on any of
the five non-native systems. That is a finding, not a disclaimer.

| query | native | duckdb | postgres | clickhouse | neo4j | memgraph |
|---|---|---|---|---|---|---|
| hist.single | eq | eq | eq | eq | eq | eq |
| hist.asof | eq | eq | eq | eq | eq | eq |
| snap.hop2 | eq | eq | eq | eq | eq | eq |
| diff.global | eq | eq | eq | eq | eq | eq |
| reach.window | eq/gr¹ | eq/gr¹ | eq | eq | eq | eq |
| paths.k | eq/gr¹ | eq/gr¹ | eq | eq | eq | eq |
| series.count | eq | eq | eq | eq | eq | eq |
| burst.zscore | eq | eq | eq | eq | eq | eq |
| nbr.evolution | eq | eq | eq | eq | eq | eq |
| coactive.narrow | eq | eq | eq | eq | eq | eq |
| resolve.substr | eq | eq | eq | eq | eq | eq |
| motif.filtered | eq | eq | eq | eq | eq | eq |

¹ guardrailed at 10M only (`E_COST`); the PostgreSQL/ClickHouse answers at
that scale bracket the refused work at 44 s / 4 s (reach) and 37 ms / 243 ms
(paths — the paths refusal is a known cost-model false positive, tracked).

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
