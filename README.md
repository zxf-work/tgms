# TGMS — Agent-Native Bi-Temporal Graph Management System

[![CI](https://github.com/OWNER/tgms/actions/workflows/ci.yml/badge.svg)](../../actions)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Coverage: temporal/ 96%](https://img.shields.io/badge/coverage_(temporal)-96%25-brightgreen.svg)](#correctness)

**A temporal graph database whose query surface is built for LLM agents —
and whose answers can be audited claim by claim.**

LLM agents are unreliable at exactly the things temporal graph analytics
requires: arithmetic, identifiers, and asserting only what the evidence
shows. TGMS's answer is architectural — give the model **no opportunity**
to do any of them:

- a **bi-temporal property graph** (valid time × transaction time) that
  distinguishes *evolution* ("the edge ended") from *correction* ("we were
  wrong"), so agents can answer *"what did we believe on March 1?"* —
  a question no snapshot or RAG system can express;
- **13 verified temporal operators** (reachability over time-respecting
  paths, δ-motifs, snapshot diffs, burst detection, interval joins, …) —
  typed, deterministic, bounded, cost-guarded, exposed as tools (MCP or
  in-process); identifiers must come from a resolver, arithmetic from a
  `compute` operator;
- a **Planner–Executor–Verifier** loop: the LLM only plans and reports;
  plans are statically validated (including a grounding rule that makes
  fabricated identifiers impossible and output-field contracts that reject
  invented result paths), executed deterministically with content-addressed
  traces, and every claim in the written answer is **machine-checked
  against the trace that produced it** — including truncation taint, so
  "correct arithmetic over incomplete evidence" is caught too.

## Does it work?

Dev-split campaign (CollegeMsg, open-source models served locally on one
24 GB GPU; details + receipts in [docs/TECHNICAL_REPORT.md](docs/TECHNICAL_REPORT.md)):

| pooled EM, Qwen2.5-14B | TGMS | vector-RAG | static-graph RAG | text-to-Cypher |
|---|---:|---:|---:|---:|
| all task families | **0.41** | 0.09 | 0.05 | 0.18 |
| correction probes ("as of tt…") | **0.67** | 0.00 | 0.00 | 0.00 |

- vs static-graph RAG: **+36 points**, paired-bootstrap 95% CI [0.18, 0.59]
- verifier fault injection: **500/500 injected false claims caught, 0 false
  positives**; emitted answers carry an unsupported-claim rate of 0.000
- operators meet all latency targets at 1M events (snapshot 98 ms, diff
  163 ms, reachability 63–244 ms)

## Quickstart

```bash
# macOS note: if this repo sits in an iCloud-synced folder, keep the venv
# outside it (iCloud sets the hidden flag on .pth files and Python 3.12+
# silently skips them):  export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/tgms
uv sync --extra agent
make test                     # 81 tests: property, oracle, metamorphic, e2e

# build a real store + task suite (downloads CollegeMsg from SNAP)
make data-collegemsg suite-collegemsg

# call one verified operator — no LLM needed
uv run tgms call temporal_reachability \
  '{"src": "n9", "window": {"t_a": 1082040961000000, "t_b": 1088000000000000}}' \
  --store stores/collegemsg

# verifier acceptance experiment (deterministic, no LLM)
uv run tgms eval c2 --store stores/collegemsg \
  --suite stores/suite-collegemsg/suite.json --mutants 500
```

With any OpenAI-compatible LLM endpoint (e.g. `vllm serve Qwen/Qwen2.5-7B-Instruct`):

```bash
uv run tgms ask "How many nodes can n9 reach between ... and ...?" \
  --store stores/collegemsg --model openai/Qwen/Qwen2.5-7B-Instruct \
  --api-base http://localhost:8000/v1 --html trace.html   # auditable trace page

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
randomized cases per operator; 96% line coverage in `tgms/temporal/`), plus
metamorphic properties — diff composition and **bi-temporal immutability**:
any result pinned to a past belief state is byte-identical before and after
later corrections. The write path is property-tested over random
assert/retract/correct interleavings, and the append-only event log replays
into either backend with identical store digests. Process rules (test
ownership, decision log, determinism receipts) are enforced in CI — see
[CONTRIBUTING.md](CONTRIBUTING.md) and [docs/DECISIONS.md](docs/DECISIONS.md).

## Layout

```
tgms/core       clock, bi-temporal data model, error taxonomy
tgms/storage    StorageAdapter ABC, Kùzu + DuckDB backends, event log, TCSR index
tgms/temporal   operator algebra O1–O13 + brute-force oracle
tgms/tools      tool schemas, MCP server / ToolRouter, trace viewer, demo GUI
tgms/agent      plan IR, planner, executor, verifier, reporter, memory
tgms/data       dataset loaders (SHA-256 pinned) + synthetic generator
tgms/eval       task suites, baselines, matrix harness, metrics, fault injection
```

Datasets are never bundled: loaders download from source (SNAP) and pin
SHA-256 manifests. See [docs/TECHNICAL_REPORT.md](docs/TECHNICAL_REPORT.md)
for design, positioning, measurements, and roadmap.

## License

Apache-2.0 — see [LICENSE](LICENSE). Cite via [CITATION.cff](CITATION.cff).
