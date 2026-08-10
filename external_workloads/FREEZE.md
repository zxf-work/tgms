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

Adjudication state (2026-08-09): annotator-1 proposals for all 52
queued items are in `bird/adjudication_proposed.jsonl` (23 SCALAR,
18 COMPLETE_SET, 7 SCALAR_TUPLE, 4 EXACT_COUNT; final distribution
pending confirmation: 205/160/80/55). Items whose gold result shape
diverges from the question's wording (e.g. yes/no questions whose
gold enumerates per-row flags) are classified BY GOLD SHAPE under
Interpretation 1 with the quirk recorded in the rationale — the gold
query is the trusted semantic query, so its answer shape is the
claim's subject. Annotator-2 (PI) confirmation pending; any label
the PI changes will be recorded as a disagreement in the same file
BEFORE results are read against those items.

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
- Prompt template and repair policy (frozen, hashes over the
  verbatim template strings in
  `external_workloads/scripts/run_bird_agent.py`; reprintable via
  `--print-freeze`):
  - prompt_template_sha256 =
    `1790a36a0dc2a386ac4485cd4406974e88320d217f61d76325ba96a58cc287b4`
  - repair_template_sha256 =
    `0bd398b4ff27c457ef2efef45ffe7dbbb344b6b7f8e0ac9b1068b21deccb2637`
  - schema prompt = the database's verbatim `sqlite_master` CREATE
    statements + BIRD external-knowledge string + question; at most
    3 repair rounds, each fed only the sqlite error text of the
    failed attempt; temperature 0, seed 0, max 1024 new tokens;
    agent SQL executes read-only under the same non-tunable 600 s
    ceiling as gold validation.

## Claim constructor for the agent arm (frozen before the first run)

The predeclared per-question claim form (auto annotation +
adjudication) is applied to the agent's OWN executed result:

- SCALAR: requires a 1x1 result; `Scalar(path="rows[0][0]")`.
- SCALAR_TUPLE: requires a single-row result; one `Scalar` per
  returned column.
- EXACT_COUNT: requires a 1x1 integer-valued result whose outer
  projection is a single aggregate with no outer LIMIT and no GROUP
  BY; `ExactCount(n)` over the COUNTED DOMAIN (see the post-run
  correction below).
- COMPLETE_SET: any row count; `CompleteSet` over canonical-JSON
  row serializations (set semantics, per rule R3; ordering
  assertions remain in `semantic_property`).

Any shape divergence from the predeclared form exits the funnel at
`shape_mismatch` and the item is uncertified — no form is re-fitted
to what the agent returned. For non-count forms, delivery
completeness follows the adapter rule: a completed sqlite statement
with no outer LIMIT delivers its complete result by construction;
when the semantic query DOES carry an outer LIMIT (semantic under
Interpretation 1 — gold top-k queries carry LIMIT), the runner
executes `SELECT COUNT(*) FROM (<agent sql>) t` over the FULL
semantic query and delivery is complete iff the delivered page
equals that count. A certificate execution failure leaves the
descriptor without a cardinality and the verifier withholds
completeness-bearing claims — never a crash, never a guess.
Exact-match (EM) is order-insensitive SET equality of result rows
against the re-executed gold — the official BIRD evaluator's
comparison — computed for every executed item independently of
certification.

Pipeline validation (declared): before the first model call, the
full runner path (constructor + adapter + verifier + EM) is
exercised once with gold SQL substituted for the agent ("oracle
smoke"); this validates infrastructure only and conditions nothing
on model results. Result (receipt
`benchmarks/results-v1/eval-bird-oracle-smoke.json`): 500/500
certified and 500/500 exact-match, across all four claim forms
(210 SCALAR, 160 COMPLETE_SET, 75 EXACT_COUNT, 55 SCALAR_TUPLE),
median descriptor 617 bytes. The funnel therefore has no
infrastructure floor: every exit the agent run shows is a property
of the agent's SQL or of the contract, not of the harness.

### ExactCount typing correction (post-run, 2026-08-10)

Prompted by review of the formal definition, not by results.
`ExactCount(n)` asserts |R*(Q)| = n for the descriptor's OWN domain
Q. The first run recorded the agent's aggregate query as Q while
setting `exact_cardinality = n` — but that query's result has ONE
row holding n, so the descriptor misdescribed its own domain's
cardinality and the certificate was of the wrong type, even though
the number was right.

The correction: for an EXACT_COUNT item the domain is the COUNTED
domain, derived from the agent's query by a projection-only rewrite
(`COUNT(*)` -> `SELECT *`; `COUNT(x)` / `COUNT(DISTINCT x)` -> the
x-projection with the NULL and DISTINCT semantics COUNT itself
applies; `SUM(0/1 expr)` -> the rows where the summand is nonzero),
and the certificate is an unlimited count over THAT domain. Each
rewrite is validated against the database — the count over the
derived domain must equal n, and for the SUM form the summand must
be 0/1-valued — and an item whose rewrite cannot be validated falls
back to a `Scalar` over the one-row aggregate result, which is the
honest claim about a result whose cardinality is 1. Delivered rows
for these descriptors are zero: the answer is the cardinality, and
the rows were never delivered, which is exactly the certificate-
without-delivery case the adapter exists for.

Applied by replaying the recorded SQL (`--replay-from`); no model
inference. Result: all 72 certified EXACT_COUNT items carry a
validated counted domain, 0 fell back. Every funnel count, every
by-form count, and every EM value is IDENTICAL to the original run
(500/495/440/440/224, EM 228) — the repair changes descriptor
typing, not results.

### R1a refinement erratum (pre-run, 2026-08-09)

The oracle smoke exposed that rule R1a (COUNT projection + how-many
wording -> EXACT_COUNT) is coarser than the certificate shape the
constructor requires: five gold queries are count-VALUED but not
domain cardinalities — two arithmetic-over-count monthly averages
(qids 47, 665: `COUNT(..)/12`), and three counts of a
grouped/ranked-selected entity (qids 687, 951, 1003). R1a is
refined: EXACT_COUNT additionally requires the outer projection to
BE a single Count/Sum aggregate over an ungrouped, unlimited outer
query; count-valued queries failing this are SCALAR (machinery in
`semantic_property`). The five relabels are recorded in
`bird/annotation_errata.jsonl`. Decided from gold SQL ASTs only —
declared annotation inputs — before any model inference; the frozen
auto-stage file is left byte-identical, the erratum layers on top.
Without this refinement, qid 47 would have produced a formally
certified "exact count" claim for a monthly average whose
count-over-twelve happened to be integral — precisely the
constructor unsoundness the certificate-shape rule exists to
prevent.

## Interpretation limits (declared)

BIRD and LDBC exercise the completeness, witness, and cardinality
dimensions of the contract, not the bi-temporal basis dimension;
the SNAP layer retains that role. Neither result claims population
representativeness — the role of the external workloads is to test
whether the contract extends beyond workloads designed with the
system in mind.
