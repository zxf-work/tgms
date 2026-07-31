# TGMS roadmap: what remains after the native-engine campaign

State as of the phase3-native-engine merge (all claims have receipts in
`docs/eval/`, `docs/eval_phase0.md`, `docs/DECISIONS.md`). Three lists, in
decreasing order of "blocks a claim" and increasing order of "blocks a
user".

## 1. Evaluation plan remainder (plan phases/§, unstarted or partial)

| item | plan ref | why it matters | size |
|---|---|---|---|
| current-vs-bi-temporal overhead | §13 | the first number a reviewer asks: what do two clocks cost vs a current-state schema | M |
| working-set vs RAM, cold cache | §14.2, §15 | all published timings are warm; the mmap/page-cache story is untested under pressure | M |
| thread scaling curve | §14.3 | parallel scan exists; no curve. One flag, one chart | S |
| reader concurrency | §14.4 | single-client only so far; lock-free readers never measured concurrently | M |
| external datasets (LDBC SNB, FinBench, LSQB, TGB) | §5, Phase 4 | one synthetic family + CollegeMsg is thin topology diversity | L |
| stress/ablation sweeps | Phase 5 | correction density (started in §12), fragmentation, and **ablations of our own features** (compression, segment cache, parallel scan — each one flag) | M–L |
| operational metrics | Phase 6 | import/startup/store-open time, wheel/binary size — never formally measured | S |
| regression dashboard | §26.9 | substitute exists (harness exits nonzero on hash mismatch); a nightly native-vs-duckdb job closes it | S |

## 2. Engine: unsolved items

Correctness-adjacent
- **paths.k cost-model false positive** — refuses a measured-37 ms query at
  10M (motif's twin was fixed; same reprice pattern applies).
- **TCSR persistence** — rebuilt per process, invalidated by writes.

Robustness / ops
- **Event-log suffix replay** (recovery is full replay today).
- **`tgms store verify` CLI** — engine call exists, no subcommand.
- **Segment-cache memory ceiling** — unbounded; fine at 10M, real at 1e8.
  Needs a byte-budget LRU with the decode-once guarantee kept.

Performance (known, priced, not chased)
- ~740 ms serial residue in 10M full-window scans (selection or NumPy
  boundary; stage-timed, unattributed further).
- Group-commit for singleton writes (§7 of lessons: the fsync floor is the
  design; the lever is batching at the write API).

Design-level (decisions, not bugs)
- **vid = 12 incompressible B/row (49% of segments)** — revisiting derived
  identity is a semantics decision (D-028 #2), not a codec.
- **Single writer; no belief-state isolation** — blocks multi-writer and
  concurrent-correction stories.
- Name resolution is current-canonical + string-only (D-031) — historical
  alias lookup remains unoffered.

## 3. The user-facing stack

Today: Python API (`tgms.open`, typed writes, `call_operator`), the MCP/
agent surface (verified plans, guardrails, traces), CLI. **No declarative
query language; no server; no non-Python client.** Evidence that this is
the binding constraint: the independent-questions study (10/110 covered,
grouped aggregation the dominant gap).

Direction (D-038, `docs/design/query_facade.md`): an openCypher **read
façade compiled onto the operator algebra** — inheriting verification,
cost guardrails, and traces rather than bypassing them — plus a server
transport, with grouped-aggregation operators as the coverage
prerequisite. Writes stay typed: corrections are not updates, and Cypher
has no vocabulary for them.
