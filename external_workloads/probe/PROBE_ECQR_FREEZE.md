# Truncation probe under ECQR gating: pre-registration (D-170, lane D)

Committed BEFORE any model inference for this leg. Purpose: measure,
on the SAME questions and the SAME paginated endpoint as the
established-interface truncation probe (PROBE_FREEZE.md, sha256
recorded in the receipt), whether ECQR gating makes the user-visible
answers of a tool-using agent reliable under incomplete execution
evidence, and at what abstention cost. The baseline conditions C0
bare, C1 flag and C2 total are NOT rerun for their rows: their numbers
come from benchmarks/results-v1/eval-trunc-probe.json as committed.

## Universe, endpoint, agent loop (unchanged from PROBE_FREEZE.md)

- Universe: probe_manifest.jsonl as committed, 111 eligible questions
  (69 SET with more than k=10 reference rows, 42 COUNT with gold
  count above k). No sampling, no exclusion.
- Endpoint: `list_records(page)`, at most k=10 records of the
  question's result list per page, zero-based, empty past the end,
  executed read-only on the pinned BIRD Mini-Dev SQLite databases.
- Model and loop: Qwen/Qwen2.5-14B-Instruct-AWQ on vLLM 0.11.0,
  temperature 0, seed 0, 1024 new tokens per call, served context
  32768; fenced-JSON tool calls and answers; at most 10 tool calls
  per question; one re-prompt after a malformed emission. Parsing,
  paging, record construction and the retry message are the probe's
  own code (run_probe.py, setup_probe.py), imported, not copied.

## Condition E (ECQR-gated)

Each tool reply is passed through the SQL evidence adapter
(tgms/evidence/adapter_sql.py, `build_sql_ecqr`, unmodified) in
pagination-aware mode: `limited=True`, `total_count=N`, where N comes
from an unlimited `SELECT COUNT(*) FROM (<endpoint query>)` executed
by the runner on the same read-only database file (adapter trust
assumption A2: the certificate is a count over the same query, never
a page). The descriptor's domain is the endpoint query, not the page.
Consequently every page descriptor states: execution complete,
delivery incomplete (no page of this universe holds all N records),
exact cardinality N.

The basis is the read-only database file. No snapshot helper is used:
pages and the count execute on an immutable file, so they observe the
same state. The descriptor's basis is therefore the adapter default
(unpinned current state of `bird:<db_id>`), as in the BIRD agent arm.

The model sees the reply `{"records": [...], "evidence": {...}}`,
where `evidence` carries the descriptor's four scope fields
`execution_complete`, `delivery_complete`, `rows_returned`,
`exact_cardinality`. The domain SQL, digests and basis stay on the
runner side: the endpoint query would give the model information that
no baseline condition had. The system prompt is byte-identical to the
probe's (C0 to C2): it neither explains the `evidence` object nor
mentions gating, so condition E differs from C2 only in the metadata
carried by the reply (`evidence` in place of `truncated` and `total`)
and in the gate applied after the answer.

### Evidence the answer cites (mechanical)

Steps s1..sm are the tool calls. At the final answer the runner forms
the assembled step s*: the records at every distinct offset retrieved
during the run, in offset order, passed through the same adapter with
the same `limited=True, total_count=N`. Its delivery is certified
complete exactly when the retrieved offsets cover the whole result
list (`rows_returned == exact_cardinality`), that is, when the agent
paginated to completion; a single page can never be complete in this
universe. Every claim of the answer cites s*. A run with no tool call
has no evidence and every claim is withheld.

### Claim construction (as the BIRD agent arm)

Records and answered values are canonicalised with the probe's
`canon` (canonical JSON after integral-float normalisation); a bare
value answered for a single-column endpoint is the record
`{column: value}` (the probe scorer's symmetric leniency).

- COUNT answer `{"count": n}`: claim c1 = ExactCount(n).
- SET answer `{"values": [...]}`: claim c1 = CompleteSet(members = the
  canonicalised answered records), plus one Membership(value=record)
  claim m1..mj per distinct answered record.

Each claim is judged by the generic verifier
(tgms/evidence/verify.py, `verify`, unmodified) against s* and its
bound rows. ExactCount is supported only through the cardinality
certificate (n == N); CompleteSet only when s* is delivery-complete
and the answered set equals the delivered set; Membership when the
record is witnessed in s*.

### Certified rendering

Deterministic, no model call, sentence forms of the certified
renderer (tgms/agent/reporter.py `certified_answer`): every supported
claim renders as a sentence prefixed by its claim id; an unsupported
claim renders as an explicit statement that it could not be certified
against its execution evidence, with its verdict, and is not
asserted. For a SET answer whose c1 is withheld, the supported
Membership claims render as a partial list, labelled as such.
`certified_answer` itself is bound to TGMS plan and trace objects and
maps no CompleteSet claim, so the probe applies the same verifier
directly, as the BIRD arm does, with those sentence forms.

## Sub-condition E-aware (evidence-aware agent)

Identical to E except that the following paragraph is appended,
verbatim, after the probe's system prompt (one blank line between):

```
Each tool reply also carries an "evidence" object. "exact_cardinality" is the certified total number of records in the full result list for this question. "delivery_complete" is false when the records in that reply are not the whole list.

Your answer is checked against this evidence before it is shown to the user. A count is shown only if it equals the certified total. A list of values is shown as the complete answer only if every record of the result list has been retrieved; otherwise it is withheld, and only the listed records that were actually retrieved are shown, marked as a partial list.
```

Question measured: does abstention fall and page enumeration rise
when the agent knows the gate.

## C0 cost reference (C0-replay)

The per-question transcripts of the committed C0 run are not retained
on any reachable host, and its receipt records classes only, not the
number of tool calls. To compare enumeration cost, condition C0 is
run once more with the probe's own runner (`run_probe.run_item`,
condition C0, unmodified). Its call counts are the C0 cost
reference. Its class rows are reported only as a reproducibility
check (per-item agreement with the committed C0 classes), never in
place of the committed baseline rows.

## Scoring (deterministic, from transcripts)

Per item, two independent labels.

1. Proposed answer class: the probe's four classes, by the probe's
   own scorer (`score_probe.classify`): page_derived, correct,
   no_commitment, other_wrong (plus error for an endpoint failure).
2. Gate outcome (user-visible), disjoint:
   - certified: c1 supported and rendered;
   - withheld: the agent committed an answer and c1 was not
     supported, so the renderer states an abstention for it;
   - abstained: the agent made no commitment (malformed twice or the
     tool budget exhausted), so the renderer has no claim to render;
   - error: the model endpoint failed (for example a context
     overrun), reported separately and never folded into another
     outcome.
   User-visible abstention = withheld + abstained.

Recorded per item besides: tool calls, distinct records retrieved,
whether s* reached certified-complete delivery, verdict and reason
per claim, the number of supported and unsupported Membership claims,
and whether a certified answer is correct under the probe's
definition (multiset equality with the gold list for SET; n == N for
COUNT). The scorer recomputes the gate from the transcript with the
same function and fails on any disagreement with the runner.

Headline, per condition (E, E-aware) and family: page_derived %,
correct %, certified %, certified-and-correct %, withheld %,
abstained %, user-visible wrong % (certified and not correct), share
of items reaching certified-complete delivery (overall and among
N <= 100, the only items a 10-call budget can enumerate), and mean
and median tool calls per question, with C0-replay's calls as the
reference. Baseline rows (C0, C1, C2) are copied from the committed
receipt; under them every committed answer is user-visible.

## Oracle validation (no model, before the model job)

Two scripted pseudo-agents drive the same loop through the same
fenced-JSON channel in condition E:

- page_only: reads page 0, then answers. COUNT: the descriptor's
  `exact_cardinality`. SET: the records seen. MUST score COUNT as
  proposed correct and certified; SET as proposed page_derived and
  withheld, with every listed record supported as Membership and s*
  never delivery-complete.
- enumerating: reads pages 0, 1, ... until an empty page or the
  10-call budget, then answers COUNT = records seen and SET = all
  records seen. MUST score every item with N <= 100 as proposed
  correct, certified, s* delivery-complete; every item with N > 100
  as proposed page_derived and withheld, s* not complete.

(The original probe's `enumerating` oracle is this leg's page_only
and its `diligent` oracle is this leg's enumerating.)

Any oracle mismatch blocks the model job.

## Reporting

Receipt: benchmarks/results-v1/eval-trunc-probe-ecqr.json, with the
commit, host and node, date, this file's sha256, the probe freeze's
sha256, the baseline receipt's sha256, prompt hashes, the oracle
results, the aggregates and per-item records. Transcripts stay under
external_workloads/probe/runs/ecqr/ (not committed).
