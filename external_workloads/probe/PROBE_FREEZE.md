# Established-Interface Truncation Probe — pre-registration (D-142)

Committed BEFORE any model inference for this probe. Purpose:
validate that the motivating failure class (page-derived counts and
page-derived complete sets under truncating interfaces) occurs on
ESTABLISHED database engines accessed through standard tool shapes,
independent of TGMS. The probe measures the PROBLEM without ECQR;
no ECQR component participates.

## Justification of the imitated tool shape (facts verified from
## primary sources before this freeze)

- LangChain's SQL agent defaults to `top_k = 10` and its system
  prompt instructs "always limit your query to at most {top_k}
  results" (langchain-community, agent_toolkits/sql/base.py and
  prompt.py, main branch, read 2026-08-21). Standard tooling
  therefore TEACHES truncation and exposes no completeness signal.
- REST list endpoints paginate by default (e.g. GitHub REST, 30
  items/page) and expose totals only out-of-band, if at all.
The probe's tool is a REST-style list endpoint over real database
tables: `list_records(page)` returning at most k rows per page.

## Universes (frozen; from existing pinned artifacts only)

Source data: the pinned BIRD Mini-Dev SQLite databases and frozen
question set (external_workloads/MANIFEST.yaml). No new data.
Two question families, selected by mechanical rule:

- SET family: the 69 questions whose final encoding is a complete
  set and whose reference result has more than k=10 rows
  (multiplicity_audit.jsonl, reference_rows > 10).
- COUNT family: the 76 auto-annotated count questions relabelled
  SCALAR+CARDINALITY_VALUE by the D-132 errata, further filtered by
  two mechanical rules applied AT SETUP, before inference:
  (a) the counted domain is derivable by the D-131 projection-only
  rewrite (COUNT(*) -> SELECT *; COUNT(x)/COUNT(DISTINCT x) -> the
  x-projection under COUNT's NULL/DISTINCT semantics; SUM of a 0/1
  expression -> rows where it is nonzero), validated against the
  database (count over derived domain equals the gold value); and
  (b) the gold count value exceeds k=10. Questions failing (a) or
  (b) are excluded by rule and listed in the setup receipt.

No sampling, no replacement, no post-hoc exclusion. If the union
exceeds 150 questions, the probe still runs ALL of them (cost is
bounded; a cap is NOT applied).

## Endpoint construction (mechanical)

For a SET question, the endpoint lists the rows of the gold query,
in the gold execution order. For a COUNT question, it lists the
rows of the validated counted domain. Page size k=10; `page` is
zero-based; a page beyond the end returns an empty record list.

## Conditions (SQLite leg; all questions x all three)

- C0 bare: response is {"records": [...]} only.
- C1 flag: adds "truncated": true|false (true iff more pages exist).
- C2 total: adds "truncated" and "total": N.

## Engine-diversity leg (PostgreSQL)

The relevant tables for the two smallest eligible databases are
loaded verbatim into an embedded PostgreSQL instance (pgserver,
verified functional on the cluster 2026-08-21); the same endpoints
run backed by Postgres, condition C0 only. Purpose: show the
phenomenon is not tied to one engine, at small cost.

## Agent loop (no ECQR anywhere)

Model: Qwen/Qwen2.5-14B-Instruct-AWQ, vLLM 0.11.0, temperature 0,
seed 0, max 1024 new tokens per call — the same fixed configuration
as every other evaluation run. ReAct-style loop: the system prompt
describes the tool and the answer format; the model emits either a
fenced JSON tool call {"tool": "list_records", "page": p} or a
fenced JSON final answer ({"count": n} for COUNT questions,
{"values": [...]} for SET questions). Budget: at most 10 tool calls
per question; one re-prompt on a malformed emission, then the run
is terminal with class no_commitment. The prompt gives no guidance
about counting strategy, pagination, or truncation.

## Scoring (deterministic; computed by script from transcripts)

Let seen = number of distinct records retrieved before the final
answer; N = the true cardinality.
- COUNT family: page_derived iff answer == seen and seen < N;
  correct iff answer == N; no_commitment iff no parsable final
  count; otherwise other_wrong.
- SET family: page_derived iff the answered value multiset equals
  exactly the projected records seen and seen < N; correct iff it
  equals the full gold projection; no_commitment / other_wrong as
  above.
- paginated_fully is recorded (all pages retrieved before
  answering) wherever N <= 100.
Headline metrics per condition: fraction page_derived, fraction
correct, fraction no_commitment, over all eligible questions.

## Reporting

Receipts: benchmarks/results-v1/eval-trunc-probe.json (per-item
records under external_workloads/probe/runs/). Paper numbers bind
through pn-macros only. The probe reports motivation validation; it
is not an RQ and changes no existing result.
