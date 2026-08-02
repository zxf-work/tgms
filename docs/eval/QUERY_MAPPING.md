# Query mapping: registry → per-system implementations

The registry (`scripts/eval_harness.py::registry`) defines thirteen logical
queries; parameters derive from each dataset's extent (windows in its own
valid-time range, belief probe from its first batch tt, node filter from
uids that exist — plan §11.5). One implementation per system:

| system | implementation | notes |
|---|---|---|
| native / duckdb | `tgms/temporal/ops_*.py` via `call_operator` | the reference semantics; two storage backends |
| postgres | `scripts/pg_queries.py` | COLLATE "C" everywhere; belief spelled `tt_e = OPEN_END` to reach partial indexes; recursion via temp-table rounds |
| clickhouse | `scripts/ch_queries.py` | Memory-engine working tables for iterative shapes; node sets cross via INSERT, never query text |
| neo4j | `scripts/neo4j_queries.py` | openCypher; Python-driven rounds (no APOC); names promoted to a property (D-031) |
| memgraph | `scripts/memgraph_queries.py` | Neo4j implementation by import; one override (degree series) |

The thirteenth query, `agg.rel_bucket` (O14 `aggregate_events`, D-044), is
the only one written *after* its baselines: the SQL and Cypher twins were
written first and the operator raced against them, rather than the reverse.
Each twin implements the registry's flagship shape only — count and
distinct-dst by rel_type × time bucket — and raises `NotImplementedError`
for other shapes, which the harness records as "no implementation written"
rather than as a verdict about the system.

Load-bearing cross-system details (each one produced a wrong-but-plausible
answer somewhere before being pinned): `clamp_tt`; half-open intervals with
`OPEN_END = 2^62`; totals counted before LIMIT; `entity_history.tt_e`
censored to OPEN_END; strict Allen `overlaps`; motif ordering strict in the
composite `(vt_s, eid)`, span bound inclusive; row order part of every
answer. Full derivations: `docs/eval_semantics.md`.
