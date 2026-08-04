# TGMS — Agent-Native Bi-Temporal Graph Management System

[![CI](https://github.com/zxf-work/tgms/actions/workflows/ci.yml/badge.svg)](../../actions)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Coverage: temporal/ 97%](https://img.shields.io/badge/coverage_(temporal)-97%25-brightgreen.svg)](#correctness)

**A temporal graph database whose query surface is built for LLM agents —
and whose answers can be audited claim by claim.**

**Project page & blog:** https://zxf-work.github.io/tgms/ · **Paper:** [paper/main.pdf](paper/main.pdf)

LLM agents are unreliable at exactly the things temporal graph analytics
requires: arithmetic, identifiers, and asserting only what the evidence
shows. TGMS's answer is architectural — give the model **no opportunity**
to do any of them:

- a **bi-temporal property graph** (valid time × transaction time) that
  distinguishes *evolution* ("the edge ended") from *correction* ("we were
  wrong"), so agents can answer *"what did we believe on March 1?"* —
  a question no snapshot or RAG system can express;
- **14 verified temporal operators** (reachability over time-respecting
  paths, δ-motifs, snapshot diffs, burst detection, interval joins, grouped
  aggregation over edge events, …) — typed, deterministic, bounded,
  cost-guarded, exposed as tools (MCP or in-process); identifiers must come
  from a resolver, arithmetic from a `compute` operator;
- a **Planner–Executor–Verifier** loop: the LLM only plans and reports;
  plans are statically validated (including a grounding rule that makes
  fabricated identifiers impossible and output-field contracts that reject
  invented result paths), executed deterministically with content-addressed
  traces, and every claim in the written answer is **machine-checked
  against the trace that produced it** — including truncation taint, so
  "correct arithmetic over incomplete evidence" is caught too;
- a **purpose-built native storage engine** (Rust, PyO3): bi-temporal
  columnar segments, a temporal-CSR traversal index, group commit, and a
  single-writer / many-reader concurrency mode — 24.6 bytes per edge
  version, versus 78.4 on ClickHouse and 549.7 on PostgreSQL for the same
  1M-event log.

## Does it work?

Three different questions, three different answers. All three are reported
because the third is the least flattering.

**1. Does the agent layer beat the alternatives?** Dev-split campaign
(CollegeMsg, open-source models served locally on one 24 GB GPU. "Answer
accuracy" is normalized typed-answer accuracy — counts and values scored
strictly, interval answers credited at IoU ≥ 0.5. Full receipts ship with
the paper and the eval records in `benchmarks/results-v1/`):

| pooled answer accuracy, Qwen2.5-14B | TGMS | vector-RAG | static-graph RAG | text-to-Cypher |
|---|---:|---:|---:|---:|
| all task families | **0.41** | 0.09 | 0.05 | 0.18 |
| correction probes ("as of tt…") | **0.67** | 0.00 | 0.00 | 0.00 |

- vs static-graph RAG: **+36 points**, paired-bootstrap 95% CI [0.18, 0.59]
- verifier fault injection: **500/500 injected false claims caught, 0 false
  positives**; on the frozen campaign, **0 of 199 emitted answers** contained
  an unsupported claim with gating (21 of 220 without it) — coverage is
  199/282, so some of that is bought by declining to answer
- accuracy tracks planner capability where baselines stay flat: **13.8% /
  34.0% / 62.8%** at Qwen2.5 7B / 14B / 32B fp16, correction probes
  saturating at 100% at 32B

**2. Is the engine competitive?** Six systems answer one 13-query registry
— TGMS native, TGMS-on-DuckDB, PostgreSQL, ClickHouse, Neo4j, Memgraph —
with **every cell hash-verified before it was timed**:

| query shape | TGMS native | best other |
|---|---:|---:|
| temporal reachability, 200k | **14.7 ms** | 3.9–7.3 s (Memgraph, Neo4j) |
| closed-triangle δ-motif, 200k | **28.7 ms** | 2.1–5.5 s (Memgraph, Neo4j) |
| grouped aggregation, 200k | **14.5 ms** | 32.6 ms (ClickHouse) |
| entity history by identity, 200k | **0.1 ms** | 0.3 ms (PostgreSQL) |
| whole-window bucketed count, 10M | 84.7 ms | **37.9 ms** (ClickHouse) |

The last row is the one we cannot close: ClickHouse keeps a factor of 2.2
on whole-window aggregation at both 1M and 10M, and it is a constant of the
shape rather than something that grows with scale. Three rounds of
profiling took that gap from 12× to 2.2× and each round found our own
implementation rather than the workload. Single latency cells reproduce to
about ±20% between days, which is stated everywhere they are quoted.

At 10M events the full query suite runs inside **1.76 GB** of peak RSS,
16 concurrent readers get **10.2×** the throughput of one, and a live
writer costs those readers **0–3%** of per-query latency.

**3. Can it answer the questions people actually ask?** This is the honest
one. 110 questions were written by people who saw a plain-language
description of two public datasets and **never saw the operator list**. Of
those, **72 are expressible today** — 10 were expressible when the study was
pre-registered. Of LDBC SNB's 41 read templates, **3**, and that number has
not moved in six sessions because 35 of the 38 misses need labelled
multi-way pattern matching, which is a deliberately deferred design
decision rather than a missing operator.

The store is good and the surface is narrow. Both instruments live in the
repo (`scripts/independent_questions.py`, `scripts/ldbc_fit.py`), they
re-run in seconds, and each capability shipped since has been scored
against a forecast made *before* it was built — delivered/predicted has run
14/30, 4/7, 10/13, 14/16, 15/15 and 4/8.

## What the operators can express

Fourteen operators, but the interesting growth since v0.4.0 happened
*inside* them, driven question by question by the study above:

| capability | where it lives | what it answers |
|---|---|---|
| grouped aggregation | `aggregate_events` | counts and distinct counts by time bucket, rel_type, endpoint or endpoint label |
| arithmetic | `compute` | mean/median over rows; ratio/diff/percent over two scalars — never in the LLM |
| typed properties | `aggregate_events` | predicates and min/max/mean over an edge property, where a value participates only if its JSON type fits |
| set operations | `compute`, `aggregate_events` | intersect/difference/union over uid lists, a cohort pre-filter, undirected and reciprocal pair modes |
| row arithmetic and joins | `compute` | `derive` adds one computed column; `join` aligns two grouped results on a key unique on both sides |
| ordered sequences | `aggregate_events` | longest gap between consecutive events, busiest sliding window of a given span, longest run with no gap over a threshold |

Every one of these is verified against the same brute-force oracle as the
operators themselves, and every one is measured in the session that shipped
it. What is *not* there is written down too, question by question, in the
re-audit tables of `scripts/independent_questions.py` and
`scripts/ldbc_fit.py` — both of which print the current blocked-capability
board on `report`.

## Quickstart

```bash
# macOS note: if this repo sits in an iCloud-synced folder, keep the venv
# outside it (iCloud sets the hidden flag on .pth files and Python 3.12+
# silently skips them):  export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/tgms
uv sync --extra agent
make test                     # 271 tests: property, oracle, metamorphic, e2e
```

```bash
# build a real store + task suite (downloads CollegeMsg from SNAP)
make data-collegemsg suite-collegemsg
```

```bash
# call one verified operator — no LLM needed
uv run tgms call temporal_reachability \
  '{"src": "n9", "window": {"t_a": 1082040961000000, "t_b": 1088000000000000}}' \
  --store stores/collegemsg
```

```bash
# verifier acceptance experiment (deterministic, no LLM)
uv run tgms eval c2 --store stores/collegemsg \
  --suite stores/suite-collegemsg/suite.json --mutants 500
```

With any OpenAI-compatible LLM endpoint (e.g. `vllm serve Qwen/Qwen2.5-7B-Instruct`):

```bash
uv run tgms ask "How many nodes can n9 reach between ... and ...?" \
  --store stores/collegemsg --model openai/Qwen/Qwen2.5-7B-Instruct \
  --api-base http://localhost:8000/v1 --html trace.html   # auditable trace page
```

```bash
bash scripts/run_webapp.sh    # interactive guided demo at localhost:8080
```

## Interfaces

| Surface | Entry point | What it's for |
|---|---|---|
| Python library | `tgms.open(...)`, `Agent(store, model=…).ask(…)` | research code, notebooks |
| MCP server | `tgms serve --store PATH` | hand the verified toolbox to any MCP-capable agent |
| CLI | `tgms ingest/synth/tasks/call/ask/bench/memory/eval` | reproducibility |
| Trace viewer | `tgms ask … --html trace.html` | *ask → answer → audit the evidence* (static, self-contained HTML) |
| Demo GUI | `tgms webapp …` / `scripts/run_webapp.sh` | guided tour: operators → agent → tamper demo → time travel |

## Correctness

Every operator is verified against an independent brute-force oracle (500
randomized cases per operator; 97% line coverage in `tgms/temporal/`
across both backends), plus
metamorphic properties — diff composition and **bi-temporal immutability**:
any result pinned to a past belief state is byte-identical before and after
later corrections. The same suite runs unmodified against **both backends**,
which is the whole acceptance argument for the native engine: it has to
satisfy the same human-owned ground truth that DuckDB does.

```bash
TGMS_TEST_BACKEND=native make test    # same tests, native engine
```

The write path is property-tested over random assert/retract/correct
interleavings, and the append-only event log replays into either backend
with identical store digests. Process rules are enforced in CI and are not
advisory: tests and the oracle may never share a commit with the
implementation they judge, and every number quoted on the project site is
resolved from `docs/site_facts.json` at build time, so a stale figure fails
the build rather than the review. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Layout

```
tgms/core       clock, bi-temporal data model, error taxonomy
tgms/storage    StorageAdapter ABC, native + DuckDB backends, event log, TCSR index
tgms/temporal   operator algebra O1–O14 + brute-force oracle
tgms/tools      tool schemas, MCP server / ToolRouter, trace viewer, demo GUI
tgms/agent      plan IR, planner, executor, verifier, reporter, memory
tgms/data       dataset loaders (SHA-256 pinned) + synthetic generator
tgms/eval       task suites, baselines, matrix harness, metrics, fault injection
crates/         the native engine: bi-temporal segments, TCSR, motif kernel
```

Datasets are never bundled: loaders download from source (SNAP) and pin
SHA-256 manifests. See [docs/eval/](docs/eval/)
for design, positioning, measurements, and roadmap.

## License

Apache-2.0 — see [LICENSE](LICENSE). Cite via [CITATION.cff](CITATION.cff).
