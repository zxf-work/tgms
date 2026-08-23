# Audit an answer

TGMS never asks you to trust an agent's prose. Every answer an agent gives
comes with a trace of exactly what ran, and a claim-by-claim verification
report checking the answer's text against that trace. This tutorial reads
a real trace end to end: what's in it, what gets checked, what
"complete" actually means, and — just as important — what none of this
guarantees.

## 0. Producing a trace

`tgms ask QUESTION --store STORE --model MODEL [--api-base URL]
--save-record record.json --html trace.html` is the normal path: it runs a
real LLM to plan a query, executes the plan, has the (same or another) LLM
write the answer text with citations, verifies those citations against the
trace, and writes both a JSON record and a rendered HTML view.

This tutorial doesn't require a live model endpoint. Everything below was
produced by driving the exact same internal path `tgms ask` uses —
`Agent.ask()`, `Reporter.report()`, `ClaimVerifier.verify()` — with a
scripted stand-in for the network LLM call, the identical technique
`tests/test_agent.py` uses to test this machinery. Every trace field, every
digest, and every verifier verdict below is real output from that run
against the store built in [Bring your own data](bring-your-own-data.md);
only the *model* is fake, not the query engine, the trace, or the
verifier. Then `tgms trace render record.json -o trace.html` — a real,
unmodified CLI call — turned it into HTML.

The question: **"How many services can svc-gateway reach in the observed
window?"**

## 1. The plan

An LLM planner's job is to emit one JSON object like this (this is the
actual plan that ran):

```json
{
  "plan_id": "p1",
  "steps": [
    {"id": "s1", "op": "resolve_entities", "args": {"query": "svc-gateway"}, "depends_on": []},
    {"id": "s2", "op": "temporal_reachability",
     "args": {"src": {"$ref": "s1.rows[0].uid"},
              "window": {"t_a": 1767225600000000, "t_b": 1767228000000001}},
     "depends_on": ["s1"]},
    {"id": "s3", "op": "compute", "args": {"fn": "count", "input": {"$ref": "s2.rows"}}, "depends_on": ["s2"]}
  ],
  "answer_spec": {"kind": "count", "from": "s3.value"}
}
```

Three steps, chained by `$ref` (`s2` reads the uid `s1` resolved; `s3`
counts the rows `s2` returned). `answer_spec` names exactly which field of
which step *is* the answer — the mechanical fallback the verifier can fall
back to if the LLM's prose answer ever can't be trusted (more in §5).

## 2. The trace

Executing that plan produces this trace — one record per step, plus a
top-level summary:

```json
{
 "plan_id": "p1",
 "steps": [
  {"step_id": "s1", "op": "resolve_entities", "resolved_args_sha": "806b1f8710e2e98a",
   "wall_ms": 1.787, "status": "ok",
   "result_digest": "aa48b3a6d60dc85ec878bdb5a833fff0c43c11d0f30fd0b518950aceb58859a1",
   "rows_returned": 1, "truncated": false, "upstream_truncated": false},
  {"step_id": "s2", "op": "temporal_reachability", "resolved_args_sha": "f735ff4754494c7e",
   "wall_ms": 2.802, "status": "ok",
   "result_digest": "b6033562a3e9fe9f9db3d507ac562e3260a806b558dfaa35c3069ef2bdfd413b",
   "rows_returned": 3, "truncated": false, "upstream_truncated": false},
  {"step_id": "s3", "op": "compute", "resolved_args_sha": "95a77a46346bced1",
   "wall_ms": 5.19, "status": "ok",
   "result_digest": "e7981ec31eef8f3c5494c3d5cf68bda7187cec9ad3f99363f9adaef47bbf317f",
   "rows_returned": 0, "truncated": false, "upstream_truncated": false}
 ],
 "answer": 3,
 "answer_error": null,
 "wall_ms": 10.7,
 "ok": true
}
```

Every field here is checkable independently, not just asserted:

- **`result_digest`** is a content hash of the step's output. Step `s2`'s
  digest, `b6033562...`, is *exactly* the `result_digest` you'd get
  running the same `temporal_reachability` call yourself with `tgms call`
  (see [Bring your own data](bring-your-own-data.md), step 4) — same
  store state, same arguments, byte-identical result. That's what
  "deterministic" buys you: the trace isn't TGMS's word for what
  happened, it's reproducible.
- **`resolved_args_sha`** is a hash of the *fully resolved* arguments
  (after `$ref`s were substituted with real values) — so you can tell
  whether two steps that look different in the plan actually ran the same
  query.
- **`truncated`** vs. **`upstream_truncated`**: did *this* step's own
  result get cut off by a page limit, vs. did an input it consumed come
  from a truncated upstream step. Both matter, and they're not the same
  thing — see §4.
- **`status`**: `"ok"` or `"failed"`, with an `error` payload attached to
  failed steps (the same structured `{"error": "E_...", ...}` shape you'd
  get calling the operator directly).

## 3. The answer object and the verifier report

The reporter (a second LLM call, or the mechanical fallback) writes prose
plus a list of typed **claims**, each citing the trace step(s) it's
grounded in:

```json
{
 "text": "svc-gateway can reach 3 services (svc-auth, svc-orders, svc-notify) within the observed window.",
 "claims": [
  {"id": "c1", "type": "count", "value": 3, "from": "s3.value", "evidence": ["s3"]},
  {"id": "c2", "type": "entity", "uids": ["svc-auth", "svc-orders", "svc-notify"], "evidence": ["s2"]}
 ]
}
```

The `ClaimVerifier` then checks every claim against the *actual stored
results* of its cited steps — not against the prose, against the data:

```json
{
 "schema_valid": true,
 "claims": [
  {"id": "c1", "type": "count", "verdict": "supported", "reason": "matched 3"},
  {"id": "c2", "type": "entity", "verdict": "supported", "reason": "all uids grounded"}
 ],
 "metrics": {
  "n_claims": 2, "ucr": 0.0, "ucr_gated": 0.0,
  "coverage": 0.8, "uncovered_assertions": ["svc-gateway"]
 }
}
```

Both claims check out — but look at `coverage: 0.8` and
`uncovered_assertions: ["svc-gateway"]`. The prose mentions
`svc-gateway`, but no claim cites evidence for *that* — it's the query's
own input, not a computed result, so it's correctly flagged as an
assertion nothing in the trace backs up. `coverage` is measured, not
assumed: it's the fraction of numbers and entity ids appearing in the
answer text that a claim actually covers. This example shows a real
answer that is fully verified (`ucr: 0.0` — zero unsupported claims) and
*still* not 100% covered — a genuinely informative distinction the
verifier makes for you rather than collapsing into one pass/fail bit.

### The verdict vocabulary

Every claim gets exactly one of four verdicts (from `tgms/agent/verifier.py`):

| verdict | meaning |
|---|---|
| `supported` | the claim matches what the cited evidence actually contains |
| `unsupported` | the claim contradicts the cited evidence, or asserts something not found in it — a caught error |
| `weakly_supported` | the claim matches, but the evidence it cites was truncated (a page limit cut off rows) — downgraded because a truncated page can't prove a claim about the *whole* result |
| `unverifiable` | the claim can't be checked at all — bad citation (references a step it doesn't actually depend on), missing/failed evidence, or a malformed claim |

`ucr` (unsupported claim rate) and `ucr_gated` (same, excluding
`temporal_pattern` claims, which use a different re-verification path) are
the headline soundness metrics: the fraction of claims caught actively
contradicting their own evidence.

## 4. What happens when evidence is truncated

Re-run the same plan with `s2`'s `temporal_reachability` capped at
`"limit": 1` (an artificially small page) — real output:

```json
{"step_id": "s3", "op": "compute", "status": "failed", "upstream_truncated": true,
 "error": {"error": "E_LIMIT",
           "message": "compute count would reduce a truncated result to one number, which is a wrong answer rather than a partial one. Page through with `cursor` and combine, or narrow the window or grouping so the result fits one page."}}
```

TGMS doesn't silently count 1 row and report "1" as if that were the true
answer — `compute` **refuses to run** on truncated input, because
reducing a partial page to a single number produces a confidently wrong
number, not a partial-but-honest one. This is the same soundness bias you
saw in the verdict table: when TGMS can't be sure, it says so (a refusal,
or `unverifiable`/`weakly_supported`) rather than guessing.

## 5. What "completeness" means

TGMS distinguishes three separate notions of complete, and conflating
them is a common way to over-trust a result:

- **Delivery completeness** — did you get back every row the *computed*
  result actually has (`rows_returned` vs. `rows_total`; `truncated`
  tells you). A page limit can make this false even when execution ran
  fine.
- **Execution completeness** — did the computation itself run to
  completion over its declared input, with no timeout or partial
  partition. This can be true even while delivery is truncated (the
  engine computed the whole thing, then handed you a page of it) — which
  is exactly why an *exact count* can still be certified from `rows_total`
  even when the *rows themselves* were paginated (see
  [the paper](../../paper/main.pdf) if you want the precise rule).
- **Domain completeness** — completeness is always relative to the query
  you actually declared (this window, these seeds, this filter) — never
  to the world. "Complete" never means "every fact that exists"; it means
  "every fact matching the query you asked, inside the store you asked
  it of."

## 6. Is this answer still fresh?

Everything above audits a trace against the store it was computed on. But a
belief can be corrected *after* you already have the answer — so TGMS ships
a second question, answerable without recomputing anything:
**`tgms trace check`** reads a saved record's dependency scope against the
event log and tells you whether anything written since could have changed
it.

Save the trace from §§1–3 above to `record.json` and check it, with nothing
else having happened yet — real output (your own timestamp will differ, the
shape won't):

```
$ tgms trace check record.json --store my_store
This answer was produced on 2026-08-23 13:27:17 UTC. Nothing written since could have changed it.
$ echo $?
0
```

Now land a correction — the same `call-09` latency fix
[Bring your own data](bring-your-own-data.md) walks through in its step
5 — and check the *same saved record* again, without re-running the query.
Real output:

```
$ tgms trace check record.json --store my_store
This answer may be stale.
  s1: A write received on 2026-08-23 13:27:39 UTC corrected the edge svc-orders→svc-notify (CALLS) over a valid-time region this computation read. Reconsider.
  s2: A write received on 2026-08-23 13:27:39 UTC corrected the edge svc-orders→svc-notify (CALLS) over a valid-time region this computation read. Reconsider.
$ echo $?
1
```

`--json` shows the same verdict per step instead of as one flattened bit:

```json
{
 "verdict": "possibly-stale",
 "steps": {
  "s1": {"verdict": "possibly-stale", "total": 1, "witnesses": [
    {"batch_id": "89e1254e0478d8f3", "tt": 1787491659282287, "kind": "correct",
     "identity": {"src": "svc-orders", "dst": "svc-notify", "rel_type": "CALLS", "disc": "call-09"},
     "vt": [1767227100000000, 1767227100000002]}
  ]},
  "s2": {"verdict": "possibly-stale", "total": 1, "witnesses": ["…same correction…"]},
  "s3": {"verdict": "fresh"}
 }
}
```

`s3` — the `compute count` step — is still `fresh`: the correction changed a
property (`latency_ms`) that a count never reads. `s1` and `s2` (resolving
`svc-gateway` and scanning reachability) flip to `possibly-stale` because
both read the edge that was corrected, inside the window they scanned. Two
verdicts on one trace, not one bit for the whole thing — the same
per-claim-not-per-answer discipline you saw in §3.

Read the three verdicts this way, and no other way:

- **`FRESH` ⇒ sound.** If `tgms trace check` says fresh, nothing written
  since could have changed the recorded result — a claim about the event
  log, not a guess. This direction is the one that is a promise
  ([`docs/STABILITY.md` §4](../STABILITY.md)).
- **`POSSIBLY_STALE` may be conservative.** It means a write happened that
  the *recorded dependency scope* could intersect, not that the answer
  actually changed — and in this example it didn't: the reachable-service
  count is still 3 after the correction, because `temporal_reachability`
  never reads `latency_ms`. TGMS flagged it anyway, because the scope
  recorded for this step is the coarse "matches anything on this edge"
  term most operators carry today, not a precise one. That's a measured
  trade, not a hand-wave: on real workloads, only 13.2% of
  `POSSIBLY_STALE` verdicts turn out to be a true stale answer on
  recomputation (`docs/design/M4_MEASURED_REPORT.md` §4) — the mechanism
  is *sound*, not *precise*, and it buys that soundness with some false
  alarms. What the soundness direction is worth, measured: across two
  independent injection campaigns and 3,354 corrections, **0 false-fresh**
  verdicts over 898 changed trials, where the obvious cheap check — "did
  the correction touch a row in the stored result?" — is wrong on **47.4%**
  of the same trials, and is structurally blind to a correction on an
  identity the stored result never mentions at all.
- **`UNDECIDABLE` is not a third answer.** It means the check itself
  couldn't run — the record was produced against a different store, the
  event log has been rewritten since, or the record's dependency-scope
  version predates this build. Every caller treats it exactly like
  `POSSIBLY_STALE` — same exit code, same "reconsider" framing — because
  "I don't know" must never look like "yes." Real output, checking the
  same record against an unrelated store:

```
$ tgms trace check record.json --store other_store
This answer may be stale.
  s1: This answer may be stale and could not be checked: this answer was not produced against this store. Treat it as unverified.
$ echo $?
1
```

`tgms trace check` only reads the event log: no database lock, no optional
backend extra, and it runs safely beside a live writer. It writes nothing —
asking the question changes none of the saved record's bytes. See
[`docs/STABILITY.md` §4](../STABILITY.md) for the full contract, including
what is *not* stable (the witness list's contents, the rendered prose).

## What TGMS does not guarantee

Stated as plainly as the implementation states them — this project
reports what it doesn't do as deliberately as what it does:

1. **That the agent understood you.** A verified claim is verified against
   the query that actually ran — not against what you meant. If the agent
   planned the wrong query, every claim about its result can be perfectly
   `supported` and still answer a different question than you asked.
2. **That it's the best query for your goal**, only that the one it ran is
   faithfully reported.
3. **That the database contains every fact in the world.** Only
   domain-relative completeness (§5) is claimed, ever.
4. **That the underlying engine is formally verified.** `result_digest`
   proves the recorded result is what was cited — never that the
   computation that produced it was semantically correct (a digest is a
   content hash, not a proof of correctness).
5. **That arbitrary free-form claims are certifiable.** Only the typed
   claim language (`count`, `value`, `entity`, `ordering`,
   `temporal_pattern`, …) is checked; prose outside that language is not
   independently verified.
6. **That a refused query was actually impossible.** The cost guardrail
   refuses queries estimated to exceed a time/resource budget; the
   estimator is calibrated, not exact, and a refusal is not proof the
   query couldn't have completed.
7. **That a "repaired" plan still answers your original question.** If a
   planner rewrites a failing plan into one that runs, the new plan's
   claims are verified against *its own* query — which may no longer be
   the one you asked.

The sharpest concrete illustration of point 1 comes from this project's
own evaluation, not a hypothetical: running TGMS's agent against 500
questions from the BIRD text-to-SQL benchmark, 440 answers were certified
— fully verified against their own trace — but only 224 of those 440 also
matched the independently re-executed gold answer. **216 certified answers
were confidently wrong**: TGMS correctly confirmed that the reported value
was exactly what the executed query computed, in full, with nothing
hidden — the query itself just answered a different question than the one
intended. That gap is the whole reason this section exists: certified
means *trace-faithful*, and trace-faithful is not the same claim as
*correct*.

## Where to go next

- [Bring your own data](bring-your-own-data.md) — build a store and run
  the queries this tutorial's trace is drawn from.
- [Give TGMS to an agent](agent-setup.md) — the MCP surface an agent calls
  to produce the steps this tutorial audits.
- [The paper](../../paper/main.pdf) — the full formal treatment (claim
  types, verification semantics, soundness) plus the measured evidence,
  if you want the precise version of everything above; the semantics
  receipts live in [`docs/eval_semantics.md`](../eval_semantics.md).
