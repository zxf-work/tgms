# Design: the openCypher read façade (D-038)

## The problem, stated from evidence

TGMS's operator algebra is what makes verification, cost-gating, and
deterministic traces possible — and it covers 10 of 110 questions
independent users actually asked. The six-system evaluation supplied the
other half of the picture: our full registry is *expressible* in Cypher
(hash-verified on two engines), so a declarative read surface over our
semantics is demonstrably writable. What users lack is not expressiveness
of the store; it is a way to ask without learning twelve operator
signatures or being an LLM agent.

## The load-bearing decision: compile to plans, not to a new engine

The façade is a **frontend to the existing plan pipeline**. A Cypher query
compiles to the same JSON operator DAG the LLM planner emits, and from
there nothing is new: the static verifier checks grounding and contracts,
the cost model gates it (`E_COST` with narrowing suggestions), the
deterministic executor runs it, results carry digests and pagination, the
trace viewer renders it. One sentence of architecture: **Cypher in, plan
DAG out; everything downstream already exists.**

What this buys, concretely:
- The agent surface and the human surface stay *one* semantics — a Cypher
  query and an agent plan that ask the same thing produce byte-identical
  payloads (testable by canonical hash, our house oracle).
- Guardrails apply to humans too. A runaway traversal gets the same
  `E_COST`-plus-suggestions a runaway agent gets, not a hung server.
- The façade cannot silently widen semantics: anything it emits must pass
  the same verifier agents face.

## Scope: a closed shape grammar, not general Cypher

v1 compiles a **closed set of query shapes**, pattern-matched from the
AST, each mapping to one registry operator family. The conformance corpus
already exists: `scripts/neo4j_queries.py` / `memgraph_queries.py` are
twelve real Cypher formulations of the registry, hash-verified — the
compiler's acceptance tests are "these twelve shapes, compiled, reproduce
the operator payloads."

| Cypher shape (sketch) | operator |
|---|---|
| `MATCH (n {uid}) RETURN history(n)` / version pattern on NodeVersion | entity_history |
| `MATCH (a)-[r]-(b) VALID AT t [hops ≤ 3]` | snapshot_subgraph |
| `MATCH path = reach((a), window)` shape | temporal_reachability / temporal_paths |
| bucketed count over `r.vt_s` | graph_metric_timeseries / burst |
| two-pattern interval predicate | co_active |
| closed-triangle pattern with ordering | count_temporal_motifs |
| `CALL tgms.resolve('q')` | resolve_entities |
| two-instant diff shape | diff_snapshots |

Anything outside the grammar is **rejected loudly** with the nearest
operator and a rewrite sketch — the same structured-repair philosophy as
the plan verifier, and the honest generalization of what we learned
porting SQL/Cypher by hand: every shape has load-bearing details, so
shapes we have not verified do not run.

## Temporal syntax: a minimal dialect, declared as such

openCypher has no temporal vocabulary. Two clause-level extensions
(SQL:2011-inspired, applied per query, defaulting to current belief and
whole valid range):

```
MATCH ...  VALID AT <t> | VALID DURING [t1, t2)
           BELIEVED AT <tt>
```

Both compile to the operators' `t_valid` / `window` / `as_of_tt`
arguments. Property-level spellings (`WHERE r.vt_s <= t AND ...`) are
*also* accepted where they match a known shape — that is how our own
baseline ports were written — but the clause form is canonical and what
documentation teaches. The dialect is documented as a dialect; we do not
claim openCypher conformance.

## Explicitly out of scope

- **Writes.** `CREATE`/`MERGE`/`DELETE` do not exist here. Corrections are
  not updates and retractions are not deletions (eval_semantics §1);
  Cypher has no vocabulary for belief revision, and inventing one inside a
  compatibility surface would teach users the wrong model. Writes stay on
  the typed API.
- General subgraph isomorphism beyond the motif catalogue; APOC; schema
  DDL.
- `GROUP BY`-style aggregation until grouped-aggregation operators exist
  (the 110-question study's dominant gap — prerequisite work item, own
  operator design, not façade work).

## Delivery plan

1. **M1 — embedded:** `store.cypher(q, params)` in-process; parser
   (openCypher grammar subset via an existing Rust/Python parser), shape
   matcher, plan emitter; the twelve-shape conformance suite green by
   canonical hash against `call_operator`.
2. **M2 — server:** HTTP endpoint (JSON in/out, the operator envelope as
   the response body) so non-Python clients exist; auth story minimal
   (loopback + token).
3. **M3 — bolt:** the neo4j-driver-compatible transport, making TGMS a
   drop-in *read* endpoint for existing tooling — feasibility already
   half-proven by our own baselines speaking bolt from Python.
4. Coverage re-measurement against the 110-question set after M1 and
   after grouped aggregation lands, so the façade's value is a measured
   delta, not a vibe.

## Risks, named

- Shape-grammar creep toward "almost general Cypher" — resisted by the
  rejection contract and by requiring every new shape to arrive with its
  hash-verified conformance case.
- Dialect confusion with real openCypher — mitigated by loud dialect
  documentation and by rejecting, never reinterpreting, unsupported
  syntax.
- Double semantics drift between façade and agents — structurally
  prevented by compiling to the same plans; any drift is a failing hash.
