# Changelog

## v0.4.0 — 2026-08-04

Engine release, and the release in which the operator surface stopped
growing while what it can express nearly tripled.

**A native storage engine (D-028…D-035).** The runtime now stands on a
purpose-built Rust core (PyO3) rather than a third-party embedded database:
bi-temporal columnar segments, a temporal-CSR traversal index, group commit,
compaction and generation collection, and a motif kernel. 24.6 bytes per
edge version against 78.4 on ClickHouse and 549.7 on PostgreSQL for the same
1M-event log. `TGMS_TEST_BACKEND=native` runs the *entire* existing suite —
invariants, the 500-case operator oracle, metamorphic properties, replay —
against it unmodified, which is the whole acceptance argument.

**Evaluated against five other systems (D-036…D-047, D-050).** One 13-query
registry answered by TGMS native, TGMS-on-DuckDB, PostgreSQL, ClickHouse,
Neo4j and Memgraph, with every cell hash-verified before it was timed:
temporal reachability 14.7 ms at 200k against 3.9–7.3 s on the graph
engines, closed-triangle motifs 28.7 ms against 2.1–5.5 s, point lookups
0.1 ms against 0.3 ms on indexed PostgreSQL. ClickHouse keeps a factor of
2.2 on whole-window aggregation at 1M and 10M, and three rounds of
profiling found our implementation rather than the workload each time. The
external-benchmark gate was decided (D-050) and declined, with the price of
a yes written down.

**Concurrency, measured (D-049).** Single-writer / many-reader: 16
concurrent readers get 10.2× the throughput of one at 10M, a live writer
costs readers 0–3% of per-query latency and readers cost the writer 1.0% of
commit latency, and group commit gives 39.3× more rows per second at 16
writers. Opening a store as a reader had been opening it as a *second
writer*, truncating a live writer's durable tail — found by the
measurement, not by review.

**Capabilities, driven by questions nobody here wrote (D-051…D-056).** Six
sessions against the 110-question independent study, each building the
smallest thing the blocked questions asked for and scoring the delivery
against a forecast published beforehand. **Expressible coverage 24 → 72 of
110, with no fifteenth operator:**

- `compute` arithmetic — `mean`, `median`, `ratio`, `diff`, `percent`
  (D-051), under one blessed rule so a quotient hashes identically wherever
  it is formed;
- typed property predicates and aggregates on `aggregate_events` (D-052,
  D-053) — a value participates only if its JSON type fits, text is never
  parsed into a number, and every excluded row is counted in
  `prop_coercion`;
- set operations over uid lists, a cohort pre-filter, and undirected /
  reciprocal pair modes (D-054);
- `derive` (one computed column) and `join` (two prior steps on a key
  unique on both sides, inner or left with a fill) (D-055);
- sequence aggregates — longest gap, busiest sliding window of a given
  span, longest gap-bounded run (D-056), plus `is_null` / `not_null` in
  `filter`, without which a null cell made a whole column unreducible.

**And what the study says we still cannot do**, because that is the more
useful half: 36 of 110 questions and 38 of LDBC SNB's 41 read templates,
the latter unmoved in six sessions because 35 of its misses need labelled
multi-way pattern matching — a deferred design decision (D-038), not a
missing operator. Both instruments ship in the repo and print the current
board on `report`.

**Process.** Published site numbers are resolved from `docs/site_facts.json`
and gated in CI, so a stale figure fails the build; commit hygiene keeps
tests and the oracle out of any commit that touches the implementation they
judge; a retired capability tag now carries an assertion that fails if it
reappears.

**Decisions D-025…D-056.**

## v0.3.0 — 2026-07-24

Post-campaign studies release: model scale, fair baselines, portability.

- Results (frozen splits, canonical store; TECHNICAL_REPORT 8.2c): the
  operator-backed advantage grows with model capability (EM 0.138 -> 0.340
  -> 0.628 across Qwen2.5 7B/14B/32B fp16; probes saturate at 1.000 at
  32B); 72B-AWQ regression isolates quantization as the planning
  bottleneck; vector-RAG at its intended k=20 breadth scores 0.021 vs ours
  0.362 in the same run; unsupported-claim rate 0.000 in every measured
  cell (4 scales, 3 families, 2 quantizations, 2 clusters).
- `tgms replay`: rebuild byte-identical stores from a recorded event log
  (preserves transaction times); canonical CollegeMsg event log + memory
  vaulted under benchmarks/frozen-v1/ (D-023 — a fresh ingest does NOT
  reproduce the store the frozen gold was computed on).
- Robustness from live campaigns: Kùzu buffer pool bounded (physical-RAM
  default OOMs in cgroups); b5 Cypher attempts execute in killable child
  processes with a hard wall-clock bound (cooperative query timeouts
  cannot interrupt every generated query); b1 chunk size plumbed
  (b1_chunk_events); 60-min LLM request budget; HF offline serving mode.
- Slurm tooling for HPC clusters (scripts/itiger_job.slurm): serve+eval
  in one right-sized allocation, per-job ports and store copies, fresh
  per-job b5 DB rebuilds.
- Decisions D-022..D-024.

## v0.2.0 — 2026-07-20

Frozen-test campaign release: the pre-registered evaluation is complete.

- Results (frozen splits, D-018; receipts in runs artifacts and
  docs/TECHNICAL_REPORT.md): CollegeMsg 94 tasks x 3 seeds — TGMS 0.408
  exact match vs 0.106 / 0.064 / 0.152 for vector-RAG, static-graph RAG,
  text-to-Cypher; all paired-bootstrap deltas significant. Correction
  probes 0.897 (CollegeMsg) and 0.846 (email-EU) vs zero for latest-state
  baselines. email-EU 0.309, synthetic 0.314. End-to-end verification:
  raw unsupported-claim rate 7.8% -> 0.000 gated, costing one EM point.
- Verifier hardening: claims whose `from` provenance pointer names an
  uncited step are unverifiable; malformed claims can never crash
  verification (verdict unverifiable instead).
- Extended fault-injection classes (eval/faults_ext.py): wrong belief
  state, truncated-page counts, entity add/drop, ordering swaps, unit
  confusion, wrong-step citation — per-class detection tables; documented
  known-negative for entity under-claiming.
- E1/E2 ablation flags (`ablate_output_contracts`,
  `ablate_truncation_taint`) and guided-decoding support (rejected at 14B
  after A/B: D-019).
- Campaign infrastructure: phase driver with self-healing passes
  (infra-failure rows are never treated as results), vLLM watchdog for
  long-run serving on sm_75, per-task token accounting, frozen suites
  vaulted under benchmarks/frozen-v1/.
- Decisions D-018..D-021; 10M-event operator benchmarks.

## v0.1.0 — 2026-07-10

First public release: Phase 1–2 research prototype.

- Bi-temporal substrate: valid-time x transaction-time version rows,
  append-only write-ahead event log (replay-identical digests across the
  Kùzu and DuckDB backends), hybrid logical clock, interval-carving
  assert/retract/correct semantics.
- Verified operator algebra O1–O13: typed, deterministic, bounded,
  cost-guarded, bi-temporal by default, output-contracted; 500 randomized
  oracle cases per operator; bi-temporal immutability metamorphic tests.
- Planner–Executor–Verifier agent layer: constrained plan IR with $ref
  binding, static validation (grounding rule, output-field contracts,
  temporal sanity, cost pre-check), deterministic executor with
  content-addressed traces, trace-grounded claim verifier with truncation
  taint (fault-injection acceptance: 500/500 detected, 0 false positives).
- Evolution memory with number-checked digests and staleness quarantine
  under corrections.
- Task-suite generation with program-computed gold (incl. bi-temporal
  correction probes), baselines (vector-RAG, static-graph RAG,
  text-to-Cypher), matrix harness with paired-bootstrap statistics and
  determinism receipts.
- Interfaces: Python library, MCP server, CLI, static trace viewer,
  interactive guided demo GUI.
- Dev-split results (CollegeMsg, Qwen2.5-7B/14B): see
  docs/TECHNICAL_REPORT.md.
