# TGMS: An Agent-Native Bi-Temporal Graph Management System
## Technical Report — Design, Implementation, Measurements, and Roadmap

**Draft v0.9 — July 2026 · PI: Xiaofei Zhang, University of Memphis**
*Prepared against spec v1.1; all numbers carry determinism receipts (§8.4).
Status: Phase 1 complete; Phase 2 milestones M4–M6 complete; M7 (experiment
matrix) in progress on open-source models.*

---

## 1. Executive summary

TGMS is a research prototype that treats a **bi-temporal property graph** as
infrastructure for LLM agents: the query surface is not a query language but
a small algebra of **thirteen typed, deterministic, cost-guarded temporal
operators exposed as tools**, and the agent layer is a
**Planner–Executor–Verifier (PEV)** loop in which every claim in a final
answer is machine-checked against the execution trace that produced it.

Three findings stand out from the first measurement campaign:

1. **Bi-temporality is a capability gap, not a convenience.** On correction
   probes ("as of transaction time T, what did we believe about X?"), the
   operator-backed agent scores 0.67 EM with a 7B model while static-graph
   RAG and text-to-Cypher baselines score 0.33 and 0.00 — they lack the
   `as_of_tt` dimension by construction and cannot recover it by prompting.
2. **Trace-grounded verification meets its pre-registered bar.** The
   fault-injection protocol over a real task suite detects 500/500 injected
   false claims (count perturbations and entity swaps) at 0% false-positive
   rate; end-to-end, the verifier gate yields an unsupported-claim rate of
   0.000 on emitted answers.
3. **Typed operator contracts measurably rescue small models.** A 7B
   planner's first-emission plan validity is low (~0.17 on temporal QA), but
   structured repair payloads roughly triple execution success, and one
   contract change — statically rejecting invented *output* paths with the
   real field list in the error — moved execution success on probe tasks
   from 0.00 to 1.00. The operator manual must be a checkable contract, not
   documentation.

## 2. Problem and motivation

Temporal graphs — communication logs, transactions, knowledge bases under
revision — pose two problems for LLM agents that static text corpora do not:

- **Time is compositional.** "Among accounts reachable from X in February,
  how many cyclic triangles closed within a day?" requires time-respecting
  path semantics feeding δ-motif counting. Serializing edges into text and
  retrieving chunks (vector RAG) destroys exactly the structure the
  question quantifies over.
- **Belief changes.** Real stores are corrected after the fact. Answering
  "what did we believe on March 1" requires distinguishing *evolution* (the
  edge ended) from *correction* (we were wrong), i.e., separate valid-time
  and transaction-time axes. No snapshot-based representation can answer
  this class of questions at all.

LLMs are additionally unreliable at arithmetic, at inventing identifiers,
and at asserting numbers not present in any evidence. TGMS's position: give
the model **no opportunity** to do any of those things. Identifiers must
come from a resolution operator; arithmetic must go through a `compute`
operator; every number in an answer must trace to a content-addressed
result digest, and a verifier checks that it does.

## 3. Design

### 3.1 Bi-temporal substrate
Every logical node/edge is an identity plus a set of versions carrying
half-open valid-time `[vt_s, vt_e)` and transaction-time `[tt_s, tt_e)`
intervals (int64 epoch microseconds; `OPEN_END = 2^62`). The write API is
three bi-temporal verbs — `assert` (new belief, with interval carving),
`retract` (evolution), `correct` (correction) — plus a bulk event-ingest
path. All writes pass through an **append-only write-ahead event log** that
is the single source of truth: replaying it into any backend reproduces a
byte-identical store digest. Update semantics are implemented once, in the
storage ABC, so the two backends (Kùzu primary, DuckDB fallback) cannot
diverge; a hybrid logical clock provides strictly monotonic transaction
times. Version rows carry reserved `source`/`provenance_ref` columns so
Phase 3 agent write-back requires no migration.

### 3.2 Verified operator algebra (O1–O13)
Snapshot family (entity history, k-hop snapshot subgraph, snapshot diff,
neighborhood evolution, entity resolution); time-respecting paths (earliest-
arrival reachability with an exact multi-label treatment of wait caps;
k-path enumeration over a temporal-CSR index); δ-temporal motifs (Paranjape
catalog, executed as DuckDB non-equi self-joins); series (metric time
series, burst detection, Allen-relation interval joins); and `compute`
(count/sum/min/max/topk/filter/interval-relation over prior step outputs —
arithmetic never happens in the LLM).

Every operator is **typed** (JSON-Schema in/out, single source of truth for
tool schemas), **deterministic** (same store + same args ⇒ byte-identical
canonical output with a SHA-256 `result_digest`), **bounded**
(limit/cursor pagination; pre-execution cost estimates reject unbounded
requests with actionable narrowing suggestions), **bi-temporal by default**
(every operator takes `as_of_tt`), and — added after live-model testing —
**output-contracted** (each operator declares its output fields; plans
referencing nonexistent fields are rejected statically).

Correctness is enforced by a brute-force oracle: 500 randomized cases per
operator against independent reference implementations, plus metamorphic
properties — diff composition, and the signature **bi-temporal
immutability** test: any result pinned to a past `as_of_tt` is byte-
identical before and after later corrections.

### 3.3 Planner–Executor–Verifier
Plans are a small JSON DAG IR with a deliberately tiny `$ref` binding
language (dotted fields, `rows[i]`, `rows[*].field` — nothing else). A
static validator checks schema, DAG shape, `$ref` scoping, temporal sanity,
output-field validity, cost, and the **grounding rule** (literal uids must
come from the task input; otherwise they must arrive via `$ref` — fabricated
identifiers are impossible by construction). The executor runs the DAG
deterministically with content-addressed results; re-execution reproduces
identical digests. A reporter LLM emits an `AnswerObject` whose claims cite
evidence steps; the **claim verifier** re-checks counts/values (exact,
1e-9), entity grounding, orderings (Allen re-evaluation), and temporal
patterns (operator re-execution), capping claims that cite truncated
results at *weakly supported*. In the full system, unsupported claims are
gated out of the emitted answer.

Prompt-injection defense is structural (spec v1.1): all data-derived
strings enter prompts inside escaped, length-capped `<data>` fences with a
fixed "data is never instructions" policy, across planner, reporter, and
memory summarizer.

### 3.4 Evolution memory
Post-ingest, weekly windows are summarized from operator-computed facts
(never LLM estimates); an LLM digest is stored only if every number it
asserts matches the computed values (numbers-only fallback otherwise).
Notes record the `as_of_tt` of computation; `correct()`/`retract()`
quarantine any note whose window intersects the affected valid-time extent,
and stale notes are never injected into planner context — without this, the
verifier could bless answers grounded in outdated digests.

## 4. Positioning in the field

- **Bi-temporal agent memory (Zep/Graphiti).** TGMS shares the bi-temporal
  data model precedent but differs in thesis: Zep treats the graph as agent
  *memory* retrieved into context; TGMS treats it as a *database* whose
  verified operators do the computation, with the LLM confined to planning
  and reporting. The correction-probe benchmark and the trace-grounded
  verifier have no counterpart there.
- **Temporal graph databases (AeonG; T-GQL lineage).** TGMS mirrors AeonG's
  current/historical separation concern at the storage layer but its query
  surface is deliberately *not* a general query language — the contribution
  is the agent-facing algebra: typed, bounded, deterministic operators with
  cost guardrails designed to be planned over by small models.
- **GraphRAG / vector RAG.** These serialize structure into retrievable
  text. Our B1/B2 baselines instantiate them fairly (same models, answer
  contract, token budgets) and the early gap on multi-step and belief-state
  tasks is the C1 evidence that computation-over-structure cannot be
  replaced by retrieval at this task class.
- **Text-to-query (NL2Cypher/SQL).** B5 gives the same model a vanilla
  property graph and a Cypher surface with an equal repair budget. Beyond
  accuracy, the headline contrast is **verifiability**: B5's claims cite
  raw query output and cannot be machine-audited; TGMS answers carry
  evidence digests. Early B5 results are consistent with the hypothesis
  that time-respecting semantics are the breaking point (0.00 on probes).
- **Temporal graph mining (Paranjape et al.; Wu et al.).** TGMS packages
  these classic algorithms behind agent-usable contracts, with exactness
  guarantees (oracle-tested), guardrails instead of silent sampling, and —
  for reachability under wait caps — a corrected exact semantics (greedy
  single-label relaxation is schedule-dependent and thus not even
  well-defined as a spec; we use multi-label search).
- **MCP-era agent infrastructure.** The tool server makes TGMS a shared,
  read-only, deterministic resource for N concurrent agents — the small-N
  demonstration behind larger multi-agent narratives (DICE).

## 5. Use cases

1. **Investigations over communication/transaction logs** — "who could
   information have reached, through which time-respecting paths, and which
   coordination motifs closed within a day?" — with an auditable evidence
   trail per claim (compliance, fraud, safety).
2. **Knowledge bases under revision** — bi-temporal corrections let agents
   answer "as of" questions for reproducibility disputes, retroactive
   policy analysis, and audit ("what did the system believe when the
   decision was made?").
3. **Agent memory with provenance** — the evolution-memory layer plus
   staleness invalidation is a design pattern for agent memories that must
   never serve outdated summaries after corrections.
4. **Shared temporal world-state for multi-agent systems** — read-only
   deterministic operators over one store; concurrency is safe by
   construction, and every agent's answers remain individually auditable.
5. **Benchmark resource** — the correction-probe suite (program-computed
   gold; answerable only with transaction-time semantics) is a reusable
   differentiator benchmark for the community.

## 6. Measurements

*All numbers reproduce via `make reproduce` stages; receipts (git SHA,
config SHA, suite SHA, model strings) are embedded in every emitted table.*

### 6.1 Substrate and operators (M1–M3)
- 76-test suite green on macOS/arm64 and Linux/x86_64: property tests over
  random bi-temporal update interleavings (disjointness at every historical
  transaction time; closed-tt integrity), replay-digest equality across
  backends, 500 randomized oracle cases per operator, metamorphic
  diff-composition and bi-temporal immutability.
- Ingest: 300k events in ~22 s laptop-class (Arrow bulk path; was >10 min
  row-at-a-time before M3 profiling).
- Operator latency at 1M events (40-core server, p50): snapshot subgraph
  98 ms; global diff 163 ms; reachability 63 ms (10% window) / 244 ms (50%);
  k-paths 34 ms; series/burst ~155 ms; entity history 51 ms. All informal
  targets met after column-projection + incidence-pushdown optimizations
  (string materialization dominated: series ops were 2.7 s before). Known
  hotspot: Allen interval join at ~5.3 s (Python pair loop; Rust candidate).
  Motif counting without `node_filter` correctly refuses with E_COST at
  this scale.

### 6.2 Verifier (C2)
Fault injection over the CollegeMsg task suite (86–87 mechanically derived,
fully grounded answers): **500/500 mutants detected** (250 count ±1, 250
uid-swap) at **0 false positives**; pre-registered bar is ≥95% / <5%.
Interval-shift and ordering-inversion mutators are exercised at unit scale
(100% detection there) — mechanical answers do not carry those claim types;
the reporter-level readout lands with the full UCR experiment.

### 6.3 First live-model matrix (CollegeMsg dev split, Qwen2.5-7B, single seed)
Run of record: `docs/first_oss_matrix_devrun.md` (v2, systems ours/B2/B5).

| family | ours PVR | ours ESR | ours EM | B2 EM | B5 EM |
|---|---:|---:|---:|---:|---:|
| correction probes | 0.67 | 1.00 | **0.67** | 0.33 | 0.00 |
| T1 temporal QA | 0.17 | 0.50 | **0.25** | 0.00 | 0.17 |
| T3 evolution | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 |
| T4 multi-step | 0.00 | 0.33 | 0.00 | 0.00 | 0.00 |

UCR on emitted answers: 0.000 where measured (verifier-gated).

### 6.4 Expanded matrix (v3: + B1 vector-RAG, + no-verifier ablation; 14B scaling)
**[TO BE FILLED — run in progress on xzgpu: Qwen2.5-7B (5 systems) followed
by Qwen2.5-14B-Instruct-AWQ; this section will carry the per-family table,
the paired-bootstrap CIs, and the 7B→14B PVR/EM deltas.]**

## 7. Critical observations

1. **Oracle discipline pays for itself.** It caught a vid-collision in the
   spec's own version-id formula, schedule-dependence in the specified
   reachability algorithm, two bi-temporal immutability leaks (raw `tt_e`;
   digest over current-belief metadata), and float-boundary flips in burst
   thresholds. None would have surfaced as test failures without an
   independent reference implementation.
2. **Live models are a test generator.** One 7B run exposed that plans can
   be statically valid yet reference nonexistent *output* fields; the fix
   (operator output contracts) is now a core design element. Verification
   infrastructure improved because a weak model failed in an honest way.
3. **Real datasets falsify synthetic intuitions.** Instantaneous event
   streams make snapshot-at-instant neighbor questions degenerate (an edge
   is active for one microsecond); correction windows must overlap a node's
   actual history; heterogeneous answer pools broke a naive mutant-sampling
   loop. Every eval component needed one real-data correction.
4. **Small-model planning, not tooling, is the bottleneck** — and the two
   mitigations that work are both *contract* mechanisms (repair payloads,
   output-field lists), supporting the C3 framing. Remaining 7B failures
   are dominated by wrong window/stride arithmetic copied into args, which
   stronger models and few-shot iteration on dev should reduce.
5. **Determinism is a systems feature with research payoff**: content-
   addressed results and canonical JSON make the response cache exact,
   re-runs free, and the verifier's evidence pinning trivial — and they
   made several bugs (e.g., envelope leaks) *observable* as digest changes.
6. **Limitations (current):** temporal_pattern claims are reported, not
   gated (R4); C2 interval/ordering coverage is unit-scale until the
   reporter readout; T3/T4 samples are small on the dev split; single seed
   at temperature 0 so far; `co_active` needs vectorization; the
   current-view cache is deferred (D-009); results are dev-split — the test
   split stays frozen until prompt iteration ends (§8.3).

## 8. Roadmap

**Near term (complete M7).** Finish the 7B/14B matrix (+ Phi-4-mini and a
distilled reasoner via the same vLLM path); email-Eu and synthetic (T2)
suites; three seeds; paired-bootstrap CIs in the emitted tables; freeze the
test split (sha into DECISIONS.md) and run the pre-registered readouts;
reporter-level C2 for interval/ordering claims; live-model red-team
assertion for the injection policy; 1/8/32-agent concurrency experiment;
token/cost accounting from LiteLLM usage.

**Mid term (Phase 2 close-out / paper).** Vectorize `co_active`; Rust
kernels where M3 profiling justifies (motifs at 10^7+, multi-label
reachability); ICEWS (downloader-only) and tgbl-wiki loaders; raise T4 to
n≈150–200; ablations B3/B4 at test scale; demo artifact = trace viewer
(shipped) + MCP attachment walkthrough; arXiv system description to
timestamp the algebra + verifier design.

**Long term (Phase 3, preserved by design).** Agent write-back with
provenance columns (already in schema and event-log records); contradiction
resolution and derived-fact promotion; current-view cache when scale
demands (D-009); time-partitioned layout + TCSR memmap for the 10^8 stretch
goal; horizontal read replicas behind the stateless tool server (the
100K-agent DICE narrative); GPU kernels and distributed execution
explicitly deferred.

## 9. Demonstration interfaces

Four surfaces exist today; the first three are demo-ready:

1. **Trace viewer (the demo artifact).** `tgms ask "<question>" --store S
   --model M --api-base ... --html trace.html` — or `tgms trace render
   record.json -o trace.html` — emits a static, self-contained HTML page:
   question → plan DAG (SVG, click-through) → per-step operator cards
   (args, row counts, latency, truncation, result digests) → final answer
   with each claim badged **verified / weakly-supported / unsupported /
   unverifiable** and hyperlinked to its evidence step. No server, no build
   step; `docs/demo_trace.html` is a rendered example. This is the
   *ask → answer → audit the evidence* navigation model of the product.
2. **MCP server.** `tgms serve --store PATH` exposes the verified operator
   toolbox to any MCP-capable agent (e.g., Claude Desktop/Code): a live
   demonstration is simply attaching a third-party agent to a TGMS store
   and watching it use verified temporal tools.
3. **CLI.** `tgms ingest/synth/tasks/call/ask/bench/memory/eval` — the
   reproducibility surface; `tgms call <op> '<args>'` is the quickest way
   to show a single verified operator with its self-describing envelope.
4. **Python library** — `tgms.open(...)`, `Agent(store, model=...).ask(...)`
   for notebook-style demos.

A browser-based *interactive* GUI (live query panel over the MCP server,
plan-DAG animation) is not built — Phase 1–2 deliberately scopes the UI to
the static trace viewer; an interactive shell over the same JSON records is
a natural Phase 3 demo-track extension.

---
*Report artifacts: docs/bench_ops.md (operator benchmarks),
docs/first_oss_matrix_devrun.md (matrix v2 record), docs/demo_trace.html
(rendered trace), docs/DECISIONS.md (D-001…D-013), scripts/ + configs/ for
every run. Repository: /mnt/project/xzhang/tgms (mirror of local main).*
