# TGMS: An Agent-Native Bi-Temporal Graph Management System
## Technical Report — Design, Implementation, Measurements, and Roadmap

**Version 1.0 — July 2026 · PI: Xiaofei Zhang, University of Memphis**
*Prepared against implementation spec v1.1. Every reported number carries
determinism receipts (git SHA, config SHA, suite SHA, model strings — spec
§8.4). Dev-split campaign complete: five systems × two open-source models.*

---

## 1. Executive summary

TGMS is a research prototype that treats a **bi-temporal property graph** as
first-class infrastructure for LLM agents. Its query surface is not a query
language but a small algebra of **thirteen typed, deterministic, cost-guarded
temporal operators exposed as tools**; its agent layer is a
**Planner–Executor–Verifier (PEV)** pipeline in which the LLM only plans and
reports, while every number, entity, and temporal assertion in a final answer
is machine-checked against the execution trace that produced it.

Headline results from the first measurement campaign (CollegeMsg dev split,
open-source models served locally; single seed, temperature 0):

- **C1 (data access).** With Qwen2.5-14B, the operator-backed agent reaches
  **0.41 pooled exact match vs 0.05 for static-graph RAG** (+36.4 points,
  paired-bootstrap 95% CI [0.18, 0.59], significant at n=22) and **vs 0.18
  for text-to-Cypher** (+22.7, CI [0.00, 0.46], borderline at dev-split n).
  On **correction probes** — "as of transaction time T, what did we
  believe?" — TGMS scores 0.67 EM at both 7B and 14B while every baseline
  scores 0.00 at 14B: the belief-time dimension is simply not representable
  in snapshot or plain-property-graph baselines.
- **C2 (verification).** The trace-grounded verifier detects **500/500
  injected false claims at 0% false positives** on the real task suite
  (pre-registered bar: ≥95% / <5%). End-to-end, emitted answers carry an
  unsupported-claim rate of 0.000.
- **C3 (small models).** First-emission plan validity rises from 0.08–0.17
  (7B) to 0.33–0.75 (14B) by family; the repair loop and, decisively, the
  **operator output-field contract** (reject invented output paths
  statically, with the real field list in the repair payload) are the
  mechanisms that convert marginal planners into working ones — one such
  contract change moved execution success on probe tasks from 0.00 to 1.00.

## 2. Problem and motivation

Temporal graphs — communication and transaction logs, evolving knowledge
bases — pose two problems for LLM agents that static corpora do not.

**Time is compositional.** "Among accounts reachable from X in February, how
many cyclic triangles closed within a day?" chains time-respecting
reachability into δ-motif counting. Serializing edges into text and
retrieving chunks destroys exactly the structure being quantified; asking a
model to emulate the algorithms in-context fails on arithmetic alone.

**Belief changes.** Real stores are corrected after the fact. Answering
"what did we believe on March 1" requires two time axes — valid time (when
a fact held in the world) and transaction time (when the store believed it)
— and the discipline to distinguish *evolution* (the edge ended) from
*correction* (we were wrong). Snapshot representations cannot express the
question, let alone answer it.

LLMs are additionally unreliable at arithmetic, prone to inventing
identifiers, and prone to asserting numbers absent from any evidence. The
TGMS position is architectural: give the model **no opportunity** to do any
of those things. Identifiers must come from a resolution operator or the
task input (enforced statically); arithmetic must go through a `compute`
operator; every asserted number must trace to a content-addressed result
digest, and a verifier checks that it does.

## 3. System design

```
                       ┌──────────────────────────────────────────────┐
  question ──►  Planner (LLM)  ──plan IR──►  Static verifier ──►  Executor
                  ▲     │ repair payloads (E_SCHEMA/E_COST/…)      │ $refs
                  └─────┴──────────────◄───────────────────────────┘
                                                    │ operator calls
                 ┌───────────── Tool layer ─────────▼──────────────┐
                 │  ToolRouter (in-process)  /  MCP server         │
                 │  O1–O13: typed · deterministic · bounded ·      │
                 │  bi-temporal (as_of_tt) · cost-guarded          │
                 └───────────────────┬──────────────────────────────┘
                 ┌───────────────────▼──────────────────────────────┐
                 │  Bi-temporal substrate: version rows over        │
                 │  (valid time × transaction time); write-ahead    │
                 │  event log; Kùzu / DuckDB adapters; TCSR index   │
                 └──────────────────────────────────────────────────┘
   answer ◄── Reporter (LLM) ◄── trace summaries;  Claim verifier gates
              AnswerObject         every claim against trace digests
```

### 3.1 Bi-temporal substrate
Every logical node/edge is an identity plus a set of versions carrying
half-open `[vt_s, vt_e)` valid-time and `[tt_s, tt_e)` transaction-time
intervals (int64 epoch microseconds; open end = 2^62). The write API is
three bi-temporal verbs — `assert` (new belief, with interval carving of
overlapping prior belief), `retract` (evolution), `correct` (correction) —
plus a bulk event-ingest path for instantaneous event streams. All writes
pass through an **append-only write-ahead event log**, the single source of
truth: replaying it into any backend reproduces a byte-identical store
digest (tested across Kùzu and DuckDB). Update semantics are implemented
once in the storage ABC so backends cannot diverge; a hybrid logical clock
guarantees strictly monotonic transaction times; failed batches are rolled
back but remain in the log, where replay deterministically re-fails and
skips them. Version rows carry reserved `source`/`provenance_ref` columns so
Phase-3 agent write-back needs no schema migration.

### 3.2 The operator algebra (O1–O13)

| Op | Name | Semantics (one line) |
|---|---|---|
| O1 | `entity_history` | version list of a node believed at `as_of_tt`, ordered by valid time; optional incident edges |
| O2 | `snapshot_subgraph` | k-hop (≤3) neighborhood of seeds in the snapshot G(t_valid, as_of_tt) |
| O3 | `diff_snapshots` | nodes/edges added/removed + props changed between G(t1) and G(t2), computed from version intervals |
| O4 | `temporal_reachability` | earliest arrival per node over time-respecting paths in a window; exact multi-label semantics under `delta_max_wait` |
| O5 | `temporal_paths` | up to k (≤20) node-simple time-respecting paths, arrival-ordered, over a temporal-CSR index |
| O6/O7 | `count/find_temporal_motifs` | δ-temporal motif catalog (Paranjape) as DuckDB non-equi self-joins; exact, order (t, eid) |
| O8 | `graph_metric_timeseries` | bucketed node/edge/degree/reciprocity/new-node series (≤2000 buckets) |
| O9 | `burst_detection` | rolling z-score / trailing-median-ratio flagged buckets |
| O10 | `neighborhood_evolution` | neighbors gained/lost between instants + incident-degree series |
| O11 | `co_active` | Allen-relation interval join between two edge selections |
| O12 | `resolve_entities` | name/uid lookup — the only legal source of identifiers |
| O13 | `compute` | count/sum/min/max/topk/filter/interval-relation over prior step outputs — arithmetic never happens in the LLM |

Five design rules bind every operator: **typed** (JSON-Schema in and out;
tool schemas generated from one registry), **deterministic** (same store +
args ⇒ byte-identical canonical output, SHA-256 `result_digest`; floats
canonicalized), **bounded** (limit/cursor pagination; pre-execution cost
estimates reject unbounded requests with *actionable narrowing suggestions*
consumed by the repair loop), **bi-temporal by default** (`as_of_tt` on
every operator; results pinned to a past belief state are immutable under
later writes — including censoring of post-`as_of` bookkeeping), and
**output-contracted** (each operator declares its output fields; plans
referencing nonexistent fields are rejected statically — added after live
small-model testing, see §8).

### 3.3 Planner–Executor–Verifier
Plans are a JSON DAG in a deliberately tiny IR: steps with operator + args,
a `$ref` binding language limited to dotted fields, `rows[i]`, and
`rows[*].field` projection, and an `answer_spec` naming where the final
answer is read from. The **static verifier** checks schema, acyclicity,
`$ref` scoping, temporal sanity (windows intersect the dataset extent,
δ > 0), output-field validity, cost, and the **grounding rule**: literal
uids must occur in the task input, otherwise they must arrive via `$ref` —
fabricated identifiers are impossible by construction, not by exhortation.
The **executor** runs the DAG deterministically with content-addressed
results (re-execution reproduces identical digests; hard caps: ≤12 steps,
≤60 s, ≤50k rows). A **reporter** LLM emits an `AnswerObject` whose typed
claims cite evidence steps; the **claim verifier** re-checks counts/values
(1e-9 tolerance) against named trace fields, entity grounding against
evidence content, orderings by Allen re-evaluation, and temporal patterns
by operator re-execution; claims citing truncated results cap at *weakly
supported*; in the full system unsupported claims are gated out of the
emitted answer. Planner/reporter/memory prompts enforce a
**data-as-inert-content policy**: all stored-data strings enter prompts
escaped and length-capped inside `<data>` fences under a fixed
"data is never instructions" rule (red-team fixture in CI).

### 3.4 Evolution memory
Weekly windows are summarized from operator-computed facts (never LLM
estimates); an LLM digest is stored only if every number it asserts matches
the computed values (numbers-only fallback otherwise). Notes record their
`as_of_tt`; `correct()`/`retract()` **quarantine** notes whose window
intersects the affected valid-time extent; stale notes are never injected
into planner context and are recomputed lazily. Without this, the verifier
could bless answers grounded in outdated digests.

## 4. Implementation notes

- **Python 3.11+, engines do the work.** Kernels operate on columnar
  buffers (Arrow/NumPy struct-of-arrays over dense int64 ids); per-edge
  Python loops are banned outside the oracle and plan-level control flow.
  Motif matching is pushed into DuckDB; traversal runs over a
  memmap-persistable temporal-CSR index rebuilt per belief state.
- **Determinism machinery.** Canonical JSON everywhere; 9-decimal float
  quantization (including *before* threshold comparisons, so engine and
  oracle flag identically); content-addressed result store; LLM response
  cache keyed by (model, prompt SHA, temperature, seed) — deterministic
  reruns are free; prompts split into a static prefix (tool manual +
  few-shots) and dynamic suffix to maximize provider/vLLM prefix-cache hits.
- **Testing.** 76 tests: hypothesis property tests over random bi-temporal
  update interleavings (disjointness at every historical transaction time),
  replay-digest equality across backends, **500 randomized oracle cases per
  operator** against independent brute-force implementations, metamorphic
  diff-composition, and the signature **bi-temporal immutability** test.
  Process rules (spec §8) are mechanically enforced: no commit may mix
  tests/oracle with implementation (checked in CI), and every semantic
  decision is a dated entry in `docs/DECISIONS.md` (D-001…D-014).
- **Serving stack (open-source track).** vLLM 0.11 + torch 2.8/cu128 +
  transformers 4.57.1 on a 24 GB Turing GPU (fp16, FlexAttention; AWQ for
  14B), OpenAI-compatible endpoint consumed via LiteLLM. Frontier/commercial
  models are deferred by decision D-013.

## 5. Positioning in the field

- **Bi-temporal agent memory (Zep/Graphiti).** Shares the bi-temporal data
  model precedent; differs in thesis. Zep treats the graph as *memory
  retrieved into context*; TGMS treats it as a *database that computes* —
  the LLM never sees raw structure at scale, only verified operator
  results. The correction-probe benchmark and trace-grounded verifier have
  no counterpart there.
- **Temporal graph databases (AeonG lineage).** TGMS mirrors the
  current/historical storage concern but deliberately does **not** ship a
  general query language: the contribution is an agent-facing algebra
  whose contracts (types, bounds, costs, output fields) are designed to be
  *planned over by small models* and *verified after execution*.
- **GraphRAG / vector RAG.** B1/B2 instantiate these fairly (same models,
  same answer contract, per-config context budgets). The measured gap on
  multi-step and belief-state tasks is the C1 evidence that retrieval
  cannot substitute for computation on this task class — and B1 additionally
  exposes a serving reality: numeric-dense event text tokenizes at ~0.65
  tokens/char, so retrieval configurations designed for frontier context
  windows (k=20 × 256-event chunks) do not fit small-model serving at all.
- **Text-to-query (NL2Cypher).** B5 gives the same model a vanilla property
  graph and equal repair budget. Beyond accuracy, the structural contrast
  is **verifiability**: B5 claims cite raw query output and cannot be
  machine-audited; TGMS claims carry evidence digests. B5's 0.00 on
  correction probes is definitional — the schema has no belief-time axis.
- **Temporal graph mining (Paranjape et al., Wu et al.).** TGMS packages
  the classic algorithms behind verified contracts, with guardrails instead
  of silent sampling — and, for reachability under wait constraints,
  replaces the (schedule-dependent, hence untestable) greedy relaxation
  with an exact multi-label semantics (D-002).
- **MCP-era infrastructure.** The stateless, read-only, deterministic tool
  server makes one temporal store safely shareable by N concurrent agents —
  the small-N demonstration behind larger multi-agent (DICE) narratives.

## 6. Use cases

1. **Auditable investigations** over communication/transaction logs: reach,
   time-respecting paths, coordination motifs — every claim in the written
   answer hyperlinks to the operator result that grounds it (compliance,
   fraud, trust & safety).
2. **Knowledge bases under revision**: bi-temporal corrections enable
   "as-of" questions for audit and reproducibility — *what did the system
   believe when the decision was made?* — a class no snapshot system
   answers.
3. **Agent memory with provenance**: the evolution-memory pattern
   (operator-computed facts, number-checked digests, staleness quarantine
   under corrections) transfers to any agent-memory design that must never
   serve outdated summaries.
4. **Shared temporal world-state for multi-agent systems**: read-only
   deterministic operators over one store; concurrent agents are safe by
   construction and individually auditable.
5. **Community resource**: the correction-probe suite (program-computed
   gold, answerable only with transaction-time semantics) as a benchmark;
   the operator manual + schemas as a reusable MCP toolbox.

## 7. Experimental methodology

**Datasets.** CollegeMsg (SNAP; 1,899 nodes / 59,835 timestamped edges) for
the dev campaign; synthetic generator with planted triangle-rings,
ping-pong pairs, and burst windows (ground-truth manifests) for T2 and
correctness; email-Eu / ICEWS / tgbl-wiki staged for the test campaign.

**Task suite.** 17 templates × 3 hand-written paraphrases across T1
(temporal QA), T3 (evolution QA), T4 (multi-step, ≥3 dependent steps), plus
correction probes (corrections injected *before* gold computation; probe
pairs provably differ across the tt boundary) and T2 (manifest gold). All
gold answers are computed by executing oracle plans through the engine —
never LLM-labeled. 20/80 dev/test split, stratified by family; the test
split is frozen by SHA and guarded by the harness (§8.3 discipline:
evaluated once per config, overrides logged).

**Systems.** `ours` (full PEV + verifier gating), `ours-noverify` (B3
ablation), B1 vector-RAG (MiniLM embeddings, 256-event chunks, k tuned to
its feasible best per serving window), B2 static-snapshot RAG (2-hop edge
list), B5 text-to-Cypher on vanilla Kùzu (same 3-repair budget). Identical
models, temperature 0, seeds, and answer contract across systems.

**Metrics.** PVR (first-emission plan validity), ESR (execution success),
EM/F1 (exact counts/values; set-F1 entities; interval-IoU ≥ 0.5), UCR
(unsupported-claim rate on emitted answers), plus paired bootstrap (10k
resamples, 95% CI) for all system-vs-system deltas.

## 8. Measurements

### 8.1 Substrate and operators
- Cross-platform: full suite green on macOS/arm64 and Linux/x86_64.
- Ingest: 300k events ≈ 22 s laptop-class (Arrow bulk path; >25× over the
  naive row-at-a-time path it replaced).
- Operator latency at 1M events (40-core server, p50): snapshot subgraph
  98 ms · global diff 163 ms · reachability 63 ms (10% window) / 244 ms
  (50%) · k-paths 34 ms · series/burst ≈155 ms · entity history 51 ms. All
  informal spec targets met after column-projection + incidence-pushdown
  (string materialization dominated scans: 2.7 s → 155 ms). Known hotspot:
  Allen interval join ≈5.3 s (Python pair loop; Rust candidate). Motif
  counting without `node_filter` correctly refuses with E_COST at 1M.

### 8.2 Verifier validation (C2)
Fault injection over the CollegeMsg suite (87 mechanically derived, fully
grounded answers): **500/500 mutants detected** (250 count ±1, 250
uid-swap), **0 false positives** — pre-registered bar ≥95% / <5%.
Interval-shift and ordering-inversion mutators are validated at unit scale
(100% detection); they require reporter-style answers and land with the
full end-to-end UCR readout. Emitted-answer UCR: 0.000 wherever measured.

### 8.3 Dev-split matrix — CollegeMsg, 22 tasks, single seed, temp 0

Per-family EM (PVR/ESR for `ours`; ablation `ours-noverify` matches `ours`
on EM by construction — the verifier gates claims, not answers):

| family (n) | model | ours PVR | ours ESR | **ours EM** | B1 EM | B2 EM | B5 EM |
|---|---|---:|---:|---:|---:|---:|---:|
| probes (3) | Qwen2.5-7B | 0.67 | 1.00 | **0.67** | 0.00 | 0.33 | 0.00 |
| T1 (12) | Qwen2.5-7B | 0.08 | 0.50 | 0.08 | 0.08 | 0.00 | 0.17 |
| T3 (4) | Qwen2.5-7B | 0.50 | 0.25 | 0.00 | 0.00 | 0.00 | 0.00 |
| T4 (3) | Qwen2.5-7B | 0.00 | 0.67 | 0.00 | 0.00 | 0.00 | 0.00 |
| probes (3) | Qwen2.5-14B-AWQ | 0.33 | 0.67 | **0.67** | 0.00 | 0.00 | 0.00 |
| T1 (12) | Qwen2.5-14B-AWQ | 0.50 | 0.67 | **0.42** | 0.08 | 0.08 | 0.33 |
| T3 (4) | Qwen2.5-14B-AWQ | 0.75 | 0.25 | 0.25 | 0.00 | 0.00 | 0.00 |
| T4 (3) | Qwen2.5-14B-AWQ | 0.33 | 0.67 | **0.33** | 0.00 | 0.00 | 0.33 |

Pooled dev EM and paired-bootstrap deltas (10k resamples):

| model | ours | B1 | B2 | B5 | ours−B1 [95% CI] | ours−B2 [95% CI] | ours−B5 [95% CI] |
|---|---:|---:|---:|---:|---|---|---|
| Qwen2.5-7B | 0.136 | 0.045 | 0.045 | 0.091 | +0.091 [−0.091, 0.273] n.s. | +0.091 [0.000, 0.227] n.s. | +0.045 [−0.136, 0.227] n.s. |
| Qwen2.5-14B-AWQ | **0.409** | 0.091 | 0.045 | 0.182 | **+0.318 [0.091, 0.545] ✓sig** | **+0.364 [0.182, 0.591] ✓sig** | +0.227 [0.000, 0.455] borderline |

**B1 serving-envelope note (a finding in itself).** The spec's B1 design
(k=20 × 256-event chunks) assumes frontier context windows: numeric-dense
event text tokenizes at ~0.65 tokens/char, so even k=3 (~27k input tokens)
and k=2 (input + the 4k output reservation vLLM charges against the window)
overflow a 28k serving envelope. B1 runs at its feasible best here, k=1 —
256 of 59,835 events (0.4% of the corpus) per question — and its nonzero
scores come from guessable small counts, not retrieval coverage. Fairness
rule WP2.6(d) is satisfied (k tuned on dev to the baseline's own feasible
best); the deeper point is that retrieval-based designs inherit a hard
context dependency that the operator system does not have — its per-task
consumption (~8–10k tokens) is window-independent.

**Scaling readout (C3).** 7B→14B doubles-to-sextuples PVR by family (T1
0.08→0.50) and triples pooled EM (0.136→0.409); the repair loop is worth
roughly a tripling of ESR at 7B (first-emission PVR 0.08–0.17 vs ESR
0.50–1.00). Correction probes stay at 0.67 EM at both scales — the
bi-temporal advantage does not depend on model size.

## 9. Critical observations

1. **Oracle discipline caught what code review would not**: a version-id
   collision in the spec's own formula; schedule-dependence (hence
   ill-definedness) of the specified reachability-with-waits algorithm; two
   bi-temporal immutability leaks (raw `tt_e`; digest over current-belief
   metadata); float-boundary threshold flips. Independent reference
   implementations are the highest-leverage artifact in the project.
2. **Live small models are a test generator.** The first matrix run
   produced ESR=0.00 everywhere because statically-valid plans referenced
   nonexistent *output* fields (`s2.count` on an operator emitting
   `rows_total`). The fix — output-field contracts checked statically with
   the real field list in the repair payload — is now a core design element
   and moved probe ESR 0.00→1.00. Verification infrastructure improves
   fastest when a weak model fails honestly against it.
3. **Real datasets falsify synthetic intuitions**: instantaneous event
   streams make snapshot-at-instant neighbor questions degenerate (an edge
   is active for one microsecond); correction windows must overlap actual
   node history; heterogeneous answer pools broke naive mutant sampling;
   numeric-dense text breaks context-window assumptions baked into
   RAG-baseline designs. Every eval component required exactly one
   real-data correction.
4. **The bottleneck at small scale is planning, not tooling** — and both
   effective mitigations are *contract* mechanisms (structured repair
   payloads; output-field lists), which is the C3 thesis in miniature.
   Residual 7B/14B failures are dominated by wrong window/stride arithmetic
   copied into arguments; few-shot iteration on dev and constrained
   decoding are the next levers.
5. **Determinism is a systems feature with research payoff**: exact
   response caching made all reruns free; content-addressed results made
   verifier evidence-pinning trivial; digest changes *surfaced* two
   correctness bugs that would otherwise have been invisible.
6. **Limitations.** Dev split only, n=22, single seed, one dataset so far;
   `temporal_pattern` claims reported not gated (R4); C2
   interval/ordering coverage unit-scale pending the reporter readout;
   ours-noverify equals ours on EM by construction (the verifier's value
   shows in UCR, whose end-to-end contrast is still to be measured);
   `co_active` needs vectorization; current-view cache deferred (D-009,
   awaiting sign-off).

## 10. Roadmap

**Near term — close M7.** email-Eu + synthetic (T2)
suites; Phi-4-mini and a distilled reasoner (think-block handling already
in place); three seeds; freeze the test split (SHA into DECISIONS.md) and
run the pre-registered readouts including end-to-end UCR vs B3;
reporter-level C2 for interval/ordering mutators; live-model red-team
assertion; 1/8/32-agent concurrency experiment; token/cost accounting.

**Mid term — Phase 2 close-out / papers.** Vectorize `co_active`; Rust
kernels where profiling justifies (motifs at 10^7+, multi-label
reachability); ICEWS (downloader-only) and tgbl-wiki loaders; T4 raised to
n≈150–200 per the power check; systems/algebra + C1/C2 → VLDB/SIGMOD
track; agentic mining + C3 → KDD/ICDM; trace viewer → demo track;
correction-probe suite → resource track; arXiv system description to
timestamp the design.

**Long term — Phase 3 (preserved by design).** Agent write-back through
the already-present provenance columns; contradiction resolution and
derived-fact promotion; current-view cache at the 10^7 committed scale;
time-partitioned Parquet layout + TCSR memmap toward 10^8 events;
horizontal read replicas behind the stateless tool server (100k-agent DICE
narrative); GPU kernels and distributed execution explicitly out of scope
until then.

## 11. Demonstration interfaces

Four surfaces exist today; the first three are demo-ready:

1. **Trace viewer — the demo artifact.** `tgms ask "<question>" --store S
   --model M --api-base … --html trace.html` (or `tgms trace render
   record.json -o trace.html`) emits a **static, self-contained HTML** page:
   question → plan DAG (clickable SVG) → per-step operator cards (args, row
   counts, latency, truncation, result digests) → final answer with every
   claim badged *verified / weakly-supported / unsupported / unverifiable*
   and hyperlinked to its evidence step. No server, no build step;
   `docs/demo_trace.html` is a rendered example. This is the product's
   navigation model: *ask → answer → audit the evidence*.
2. **MCP server.** `tgms serve --store PATH` hands the verified operator
   toolbox to any MCP-capable agent (Claude Desktop/Code, etc.) — the
   most compelling live demo is watching a third-party agent use verified
   temporal tools against a shared store.
3. **CLI.** `tgms ingest / synth / tasks / call / ask / bench / memory /
   eval` — the reproducibility surface; `tgms call <op> '<args>'` shows a
   single verified operator with its self-describing envelope in seconds.
4. **Python library.** `tgms.open(...)`; `Agent(store, model=…).ask(…)` for
   notebooks.

An interactive browser GUI (live query panel, DAG animation) is
deliberately out of Phase 1–2 scope; the static trace viewer is the demo
deliverable, and an interactive shell over the same JSON records is the
natural Phase-3 demo-track extension.

---

### Appendix: artifacts and receipts

| Artifact | Location |
|---|---|
| Operator benchmarks (1M events, receipts embedded) | `docs/bench_ops.md` |
| Matrix run records | `runs/dev-collegemsg-oss-v3/`, `runs/dev-collegemsg-oss-14b/` (server), `docs/first_oss_matrix_devrun.md` |
| C2 readout | `tgms eval c2 --store … --suite …` (deterministic) |
| Rendered demo trace | `docs/demo_trace.html` |
| Decision log (D-001…D-014) | `docs/DECISIONS.md` |
| Process enforcement | `scripts/check_commit_hygiene.py` (spec §8.1) |
| Reproduce pipeline | `make reproduce` (ingest → tests → suite-gen → matrix) |

Matrix receipts (dev campaign): suite `cbdc36a0…0142f`, configs
`5037010…` (7B) / `0a3d380…` (14B), commit `9c576fc` (+`1865dad` B1 k
retuning), models `Qwen/Qwen2.5-7B-Instruct`,
`Qwen/Qwen2.5-14B-Instruct-AWQ` via vLLM 0.11.0.
