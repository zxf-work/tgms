# Response to the v3 deep-coherence review (2026-07-24)

All 21 issues addressed. Mapping (review item → change):

**I.1 Contradictory evaluation status** → intro rewritten with the
"Evaluation status and chronology" paragraph (dev → freeze → campaign →
post-campaign); contribution 4 now describes the frozen suites;
limitations and conclusion rewritten to the same status; abstract and
body share one timeline.

**I.2 Missing controlling thesis** → trust-boundary thesis stated before
the contributions ("conventional interfaces assume a competent client…
TGMS places the LLM outside the trusted computing boundary"); the four
contributions are now explicitly subordinate to it.

**I.3 Architecture after storage semantics** → new §1.1 "System overview
and trust boundary" + Figure 1 moved into the introduction; figure
redrawn with an explicit dashed TRUSTED region (verifier, engine, store,
trace, claim verifier) and the LLM column outside it.

**I.4.1 Quantization not isolated** → replaced with "consistent with
quantization-related degradation… does not separate quantization from
model size and serving effects"; matched-pair experiment named as the
requirement.
**I.4.2 Interface not sole cause** → "benefits more strongly from
increasing model size than the two tested baselines… suggests the
constrained plan representation makes additional planning capability
more usable."
**I.4.3 No representational impossibility from k=20** → "increasing
retrieval breadth… did not improve accuracy; the main result is not
explained solely by the small k"; unexplored alternatives listed.
**I.4.4 No capability threshold** → "portability to weaker model
families is not automatic; determining whether a family-independent
threshold exists requires matched-size experiments."

**I.5 Safety vs abstention** → new metrics reported: coverage 199/282
(0.706), overall accuracy 0.408, conditional accuracy 0.548,
answer-level UCR with denominators; the T2 zero is framed as a coverage
failure ("abstention is the designed failure mode, but it is a coverage
failure, not a success"); scope of the guarantee explicitly excludes
ungated pattern claims, approximation, write-back, adversarial tools;
"safety lives inside…" replaced by "gated claim support remained stable
while accuracy and coverage varied."

**I.6 UCR imprecise** → defined answer-level: 21/270 raw answers
(7.8%) contain ≥1 unsupported gated claim; 0/268 after gating; cost
1.0 percentage point (0.418→0.408). Same numbers in abstract and §5.3.

**I.7 Abstract-only verifier results** → full mutation table
(Table: 9 classes, cases/detected/without-mechanism) added to §5.4 with
the truncation reasoning chain and both deliberate negatives explained;
"deferred" claim removed.

**I.8 Dev results before frozen** → §5 reorganized: 5.1 hypotheses
(H1–H4), 5.2 data/systems/metrics, 5.3 primary frozen results,
5.4 mechanism ablations + verifier, 5.5 model/serving sensitivity,
5.6 operator performance. Dev split reduced to a one-sentence
replication note (0.409 ≈ 0.408).

**II.9 Benchmark under-specified** → task-construction paragraph
(17 templates × 3 paraphrases, parameter instantiation, filtering,
probe-pair construction, 20/80 stratified split) + dataset table with
per-family counts and answer-type distribution; closed-world alignment
acknowledged here and in limitations.

**II.10 Probe unit unclear** → defined: pairs of separately scored
questions; the CollegeMsg test split contains 13 probe questions
(7 before-correction, 6 current-belief halves; the split cuts across
pairs).

**II.11 Baseline information** → baseline-information table (input
representation / valid time / transaction history / operators /
bounded); capability-vs-planning distinction drawn in §5.3 and
limitations; "two latest-state baselines score zero, vector-RAG 0.154
from current-belief halves" made explicit.

**II.12 "Exact match" misnomer** → metric renamed *normalized
typed-answer accuracy*; interval IoU rule disclosed; entity
recursive-collection laxity disclosed.

**II.13 Two k=20 configs conflated** → named "original chunking target"
(20×256, not runnable) vs "long-context control" (20×24, runnable);
same-run comparison identified (0.362 vs 0.021); "intended" no longer
applied to the control.

**II.14 Model labels inconsistent** → every results table caption now
carries model, quantization, engine, hardware, seeds; warning added
that scale-study absolutes are not comparable to the AWQ primary
(within-configuration trends only); 14B fp16 vs 14B AWQ difference
addressed explicitly.

**III.15 Agent-native undefined** → definition in the introduction.
**III.16 "Verified" overstated** → subtitle now "Validated Temporal
Operators…"; §3 renamed "A fixed operator algebra"; "verifier" reserved
for plan/claim checking.
**III.17 "Closed algebra"** → "fixed"; closure disclaimed; why-13
paragraph added (four workload families + grounding + compute; coverage
measured, completeness not claimed).
**III.18 Bounding conflated** → cardinality (pagination) vs work
(cost estimation and refusal) separated, with the motif-count example.
**III.19 Update semantics** → worked 2-D example (assert at tt1,
correct at tt2, current vs pinned snapshot) + same-identity disjointness
assumption + coexisting-facts-as-distinct-identities note.
**III.20 Source of truth vs skipped batches** → log defined as committed
logical update log + audit log of attempts; replay reproduces both.
**III.21 Peripheral components** → prompt-injection and evolution memory
moved to §4.x "Extensions outside the evaluated pipeline"; memory
mechanism corrected (quarantined notes excluded from context, never
citable; claims resolve only to operator traces).

**IV Section 6** → renamed "Design lessons and their experimental
support"; each lesson in the failure→property→mechanism→test→consequence
form; N stated (0/3→3/3 probes, 0/22→12/22 overall); stale "will add"
text replaced by the completed long-context control.

**V Limitations** → rewritten from scratch with all eight items;
engine-recycling anecdote removed from claims (ops material remains in
the technical report).

**VI/VII Abstract & conclusion** → adopted the suggested versions with
exact denominators filled (21/270, 0/268, 1.0pp) plus one
association-framed scale sentence.
