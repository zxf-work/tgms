# External Workload Freeze (D-129)

Committed BEFORE any model inference on external workloads. This
file fixes the universes, the mapping rules, and the system boundary
so that no external-benchmark decision can be conditioned on results.

Date of freeze: 2026-08-09.

## Universes

- **BIRD**: the complete 500-instance Mini-Dev SELECT-only SQLite
  split, pinned by repository commit, dataset revision, record
  digest, and per-database digests (MANIFEST.yaml). Fixed universe:
  no sampling after seeing results, no replacing failed instances,
  no excluding difficult or unsupported forms from any denominator,
  no claim-grammar change after inspecting coverage.
- **LDBC**: all 41 read templates — Interactive v1 complex reads
  1–13 plus 14-v1, short reads 1–7, and BI reads 1–20 — pinned by
  the three repository commits and the extracted-manifest digest.
  Inserts and deletes are excluded (read-only contract).

## Gold validation (BIRD)

Gold SQL executes VERBATIM on sqlite3 (the dialect it was written
for), read-only, per-query wall ceiling **600 s** (raised from an
initial 120 s before this freeze because one gold query,
question_id 701, completes in ~296 s; the ceiling is fixed here and
will not be tuned again). Result: 500/500 GOLD_VALID, 0 empty
results. Statuses are terminal; nothing is replaced.

## Claim-mapping protocol (BIRD)

Inputs per record: natural-language question, gold SQL AST
(sqlglot), gold result shape. Agent output and verifier results are
NEVER inputs to this stage.

**Interpretation 1 (declared):** the gold query — including its
ORDER BY, semantic LIMIT, grouping, and aggregates — is the trusted
semantic query Q under assumption A1. The claim form classifies the
ANSWER SHAPE relative to R\*(Q); the relational machinery used to
compute it is recorded separately as `semantic_property`
(plain / aggregate / grouped / ranked_extremal / top_k / set_op /
windowed).

Rules (first match; else adjudication queue):
- R1 single-row, single-column: COUNT projection with how-many
  wording → EXACT_COUNT; otherwise → SCALAR.
- R2 single-row, multi-column → SCALAR_TUPLE (one Scalar claim per
  cited column; in-fragment).
- R3 multi-row → COMPLETE_SET (set semantics; ordering assertions
  live in `semantic_property`).
- R4 yes/no wording with boolean shape → EXISTS / NOT_EXISTS.
- R5 wording/shape mismatches → QUEUE.

Adjudication: two research-team annotators resolve the QUEUE using
only the three declared inputs; disagreements and their resolution
are recorded in `claim_annotation.jsonl`. Auto stage: 448 of 500
classified, 52 queued.

## LDBC coverage protocol

Two independent dimensions per template, judged from the pinned
official specifications: `exec_coverage` (DIRECT_TGMS /
DECOMPOSABLE_TGMS / SQL_ONLY / UNSUPPORTED_EXECUTION) against the
frozen 15-operator TGMS surface, and `claim_full_contract`
(CURRENT_ECQR_FRAGMENT / REQUIRES_ORDERED_RESULT / REQUIRES_TOP_K /
REQUIRES_RANKING / REQUIRES_PATH_CERTIFICATE) for the FULL LDBC
result contract including its sort specification, plus
`set_projection_in_fragment` for the unordered projection. Same
Interpretation-1 rule as BIRD. Current state: single-annotator
draft with per-template rationale; second-annotator adjudication
before any paper number is derived.

## System boundary frozen for the agent stage

- Claim grammar: ECQR/0.1, evidence-fragment-v1.0 (EVIDENCE_MODEL
  v1.0.1). No grammar change in response to external results.
- Verifier, SQL adapter: repository state at commit
  `a3f2712` (MANIFEST.yaml `ecqr.freeze_base_commit`).
- Planned BIRD agent arm (stronger plan): one frozen model
  configuration (Qwen2.5-14B-Instruct-AWQ, vLLM 0.11.0, temperature
  0), one seed, explicitly labeled a coverage study separate from
  the multi-seed RQ2 experiment. The BIRD arm executes gold-dialect
  SQL on sqlite; the adapter's engine field records `sqlite`. The
  claim CONSTRUCTOR for the BIRD arm follows this file's mapping
  protocol (semantic typed claims: count→ExactCount,
  list→CompleteSet, yes/no→Exists/NotExists, scalar→Scalar) — a
  documented departure from the frozen SNAP arms'
  SQL-conservative constructor, declared here before any run.
- BIRD delivery semantics: the semantic BIRD SQL executes to
  completion (no artificial delivery pagination); the
  exact-cardinality certificate wraps the ORIGINAL semantic query
  (`SELECT COUNT(*) FROM (<bird sql>) t`), never a page of it.
- Prompt template and repair policy hashes are added here before
  the first agent run.

## Interpretation limits (declared)

BIRD and LDBC exercise the completeness, witness, and cardinality
dimensions of the contract, not the bi-temporal basis dimension;
the SNAP layer retains that role. Neither result claims population
representativeness — the role of the external workloads is to test
whether the contract extends beyond workloads designed with the
system in mind.
