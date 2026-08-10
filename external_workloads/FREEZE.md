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

## LDBC coverage protocol (D-134, second-annotator adjudicated)

Two independent axes, each with a mechanical rule.

EXECUTION, against the frozen registry (O1-O15 as returned by
`tgms.temporal.algebra.REGISTRY`): DIRECT_TGMS = one registered
operator invocation computes the required relation, with field
selection or renaming for presentation not counting as a second
operator; DECOMPOSABLE_TGMS = an explicit DAG over registered
operators and the deterministic `compute` vocabulary only, with no
hidden database reads and no unregistered recursion, graph
algorithm, or group-by; SQL_ONLY = no such DAG but the SQL path
expresses it; UNSUPPORTED_EXECUTION = neither surface implements a
needed primitive. Every DIRECT/DECOMPOSABLE row carries an
`exec_witness` plan naming operators, every SQL_ONLY row names the
missing TGMS capability, and every UNSUPPORTED row names the
primitive missing from both. Witnesses are CONSTRUCTED against the
frozen operator schemas and are not executed against an LDBC
instance; they make the classification auditable, not benchmarked.

Four draft labels were reclassified to SQL_ONLY on second review
because the frozen registry does not expose what the draft assumed:
IC13 (no static variable-length shortest path --- snapshot_subgraph
stops at hops<=3, temporal_reachability is earliest-arrival over
time-respecting paths, temporal_paths enumerates bounded
time-respecting paths), IS2 and IS6 (no unbounded replyOf root
closure, and LDBC does not bound thread depth), and BI1 (three
grouping dimensions including a derived content-length category,
while `aggregate_events` groups edge events by at most two
dimensions from the closed set time_bucket / calendar_unit /
rel_type / endpoint / label). Totals: 4 direct, 5 decomposable, 28
SQL-only, 4 unsupported.

CLAIMS, a mutually exclusive partition by the FIRST contract
feature the grammar lacks, in the precedence path certificate >
groupwise extremum > top-k > ordered result > in fragment.
REQUIRES_TOP_K means a global sort or rank followed by a finite
semantic LIMIT selecting k rows of a larger relation;
REQUIRES_ORDERED_RESULT means every qualifying row is returned but
the contract fixes their sequence; REQUIRES_GROUPWISE_EXTREMUM
(renamed from REQUIRES_RANKING, which overlapped top-k almost
entirely) is reserved for an extremum per group before global
presentation, which is BI14 alone. Totals: 7 in fragment, 5
ordered, 27 top-k, 1 groupwise extremum, 1 path.

The audit classifies the RETURNED CONTRACT, not every operation
inside trusted Q. A scalar produced by a trusted shortest-path
query is a Scalar and ECQR does not independently establish path
optimality (IC13, BI15); a returned path sequence is outside the
vocabulary because the user-visible object itself has path
structure (IC14). The two readings are not mixed.

PROJECTION, Policy A: a complete-set projection carries a scalar
atom or a flat tuple of scalar atoms, never a nested list, set, or
path sequence. `flat_projection_in_fragment` is 38/41; the three
exceptions are IC1 and IC12 (nested collections: universities,
companies, tagNames) and IC14 (a path sequence). Execution
difficulty is irrelevant to this axis, so BI19 and BI20 --- whose
execution is unsupported but whose returned rows are the flat
tuples (id, id, weight) and (id, weight) --- are projection-
compatible. `duplicate_safe` records why the set comparison is
faithful per template: 25 guaranteed by an entity key, 6 by the
grouping, 7 single-row.

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

### D-133 review repairs (2026-08-10)

**Projection generality.** `CompleteSet(S,f)` and `Membership(v,f)`
take f to be a deterministic projection from a canonical row to a
canonical value that may be scalar OR TUPLE-valued, so
pi_f(R) = {f(r) : r in R} may be a set of tuples. This legitimizes
the multi-column complete sets the adjudication already uses
without adding a claim form; the paper's formal text is corrected
to match.

**Bag versus set (multiplicity audit).** pi_f(R) is compared as a
SET, so a complete-set claim certifies an unordered,
DUPLICATE-INSENSITIVE projection. `bird/multiplicity_audit.jsonl`
records, for all 151 set-encoded items, whether the reference
result is duplicate-free: 124 are, 27 are not, and in the worst
case a set projection would drop 11,446 rows to 548 distinct ones.
Those claims stay true of the result but do not carry multiplicity
the contract may need, so the items keep
`full_question_contract_covered = false` --- the same treatment
q128 already gets for demanding an order the fragment cannot
certify. Duplicate-sensitive bags remain outside the fragment; no
`CompleteBag` form is added.

**q83 relabelled.** "how many schools ... for each city" is
collection-valued by intent. Actual result cardinality must not
turn a set-valued question into a scalar one, so the encoding is
`CompleteSet (tuple projection)` with `GROUPED_RESULT`, and the
item is a question/gold mismatch because the gold answers only one
part of a three-part request.

**Mismatch kinds.** Each of the 11 question/gold disagreements now
carries `mismatch_kind` (ENTITY_AMBIGUITY, MULTIPART_INCOMPLETE,
PREDICATE_SEMANTICS, SEMANTIC_SCOPE, TEMPORAL_AMBIGUITY,
GROUPED_VS_COUNT) with a per-item note, so they are not described
as if they were one kind of annotation defect. q1205 is recorded as
PREDICATE_SEMANTICS: BIRD's own evidence field defines "within a
normal range" as the value EXCEEDING the sex-specific threshold, so
its gold is consistent with its declared evidence while the
natural-language reading is the opposite. Annotated, not repaired.

**Chronology, stated precisely.** The universes, claim vocabulary,
mapping rules, system boundary, and prompt/repair hashes were fixed
and committed before any model inference. The FINAL labels were
not: after the single frozen run, ambiguous items were adjudicated
by a second annotator and two mapping rules were corrected, so the
final adjudication is a POST-RUN SEMANTIC AUDIT. Every relabelling
is justified from benchmark source fields alone (question, gold SQL
AST, gold result shape); no system component was changed to improve
a measured outcome; the agent's stored SQL is independent of the
labels, so affected records were replayed without further
inference. The pre-run proposal, the final adjudication, and BOTH
result sets ship with the artifact.

**q32.** Kept as a complete set: its five reference rates are
verified distinct. The projection carries the rate but not school
identity, so the general contract would lose multiplicity; recorded
as a limitation on the item.

**LDBC duplicate-insensitivity.** Of the 38 templates whose
unordered projection is in the fragment, 27 carry an entity
identifier in the result schema and the remaining 11 are grouped or
scalar results whose group key is itself projected, so uniqueness
follows from the grouping. This is derived from the pinned result
schemas and is part of the pending second-annotator review of the
LDBC annotation.

### D-132 adjudication protocol (PI, 2026-08-10)

The governing principle, adopted on PI adjudication of the 52-item
queue: **natural-language intent determines the requested claim
contract**; gold SQL and result shape disambiguate that intent but
do not override an unambiguous question where the benchmark is
internally inconsistent. RQ3 measures coverage of independently
authored *questions*, not of BIRD's result shapes. Every record
therefore carries two contracts and their disagreement:

    intent_contract | gold_result_contract | question_gold_mismatch

The primary external-validity result is defined on
`intent_contract`; the gold-shape reading is retained as a
sensitivity view in the artifact. No item is removed from the fixed
500 denominator.

**Rule 1 (supersedes the D-130 R1a erratum and the counted-domain
construction).** `ExactCount(n)` asserts |R*(Q,B)| = n for the
descriptor's own domain. `SELECT COUNT(*) ...` has a logical result
of ONE row holding n, so its cardinality is 1: a count-VALUED
aggregate is a **Scalar** claim tagged `CARDINALITY_VALUE`, never an
ExactCount. The same holds a fortiori for `SUM(CASE ...)`. R1a is
retired as "single aggregate => ExactCount" and reinterpreted as
"a single aggregate may denote a count-valued scalar; only an
explicit counted-domain mapping could produce ExactCount, and that
machinery is deliberately NOT introduced." The counted-domain
rewrite built in D-131 is removed. All 76 auto-annotated
EXACT_COUNT items and 4 queued ones (q92, q260, q479, q977) become
SCALAR + CARDINALITY_VALUE.

ExactCount survives where it is formally earned: q1187 asks how
many patients AND to list them, its result IS the 63 patient rows,
so |R*(Q,B)| = 63 and the record carries a claim bundle
`CompleteSet(IDs) + ExactCount(63)`. The contrast with q92 --- a
one-row result holding a count, encoded as Scalar --- is the
clearest statement of why count-valued data and result cardinality
are different concepts.

**Rule 2.** `SCALAR_TUPLE` is a workload answer-shape category, not
a seventh ECQR claim form: it is implemented as a bundle of Scalar
claims over the same single result row. The formal vocabulary stays
at six forms.

**Rule 3.** `CompleteSet(S,f)`'s projection f is a deterministic
map from a row to a canonical value, **possibly tuple-valued**, so
pi_f(R) may be a set of canonical tuples (q27, q128, q518, q587,
q978, q1457). This is a clarification of the existing form, not a
new one, and it is what makes the LDBC unordered-projection result
well defined too.

**Rule 4.** Where a question demands an ordering the fragment
cannot certify (q128, "by descending order, from the highest to the
lowest"), the record certifies the unordered projection and sets
`full_question_contract_covered = false` with
`missing_semantics = ORDERED_TOP_K` --- the same distinction the
LDBC analysis already draws between a certifiable unordered
projection and the full ordered contract. Where the fragment cannot
express the answer at all (q1338: per-row booleans with no key in
the projection, so set semantics collapse {YES, NO}), the encoding
is `OUTSIDE_CURRENT_FRAGMENT` and no claim is constructed.

**Rule 5 (EXISTS shape).** For existence-intent questions (q469,
q1399) the claim is about witnesses, so a single boolean-valued
cell is a shape mismatch: an agent that computes a truth value
rather than exhibiting evidence has not answered in the requested
form.

Adjudication of the 52 (annotator-1 proposal, PI as annotator-2) is
in `bird/adjudication_final.jsonl`: 32 Scalar, 7 Scalar bundles, 7
CompleteSet, 2 Existence, 1 CompleteSet+ExactCount, 1 outside the
fragment; 10 items carry `question_gold_mismatch = true` and 3 have
`full_question_contract_covered = false`.

### ExactCount typing correction (post-run, 2026-08-10) --- SUPERSEDED

Retained for the record; the counted-domain construction described
here was removed by D-132 rule 1 above.

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
