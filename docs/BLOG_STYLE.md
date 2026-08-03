# TGMS blog: editorial standard

This is the contract every post on <https://zxf-work.github.io/tgms/blog/>
follows. It exists because an external editorial audit (2026-08-02) found
that the posts were strong as engineering records and weak as explanations:
terms arrived before the problems that motivate them, results before their
workloads, and — worst — the same fact appeared with different values on
pages that link to each other. Most of that is fixable by a template and a
pipeline rather than by better prose, which is what this document is.

Read this before writing or revising a post. Where it disagrees with the
audit that produced it, the disagreement is recorded in §9 with a reason.

---

## 1. Who the blog is for, and what it is not

The reader is technically literate — databases, data systems, or AI
applications — and has **not** read the paper or the README. They are
deciding whether TGMS is relevant to them.

The blog sits between the project page (what this is) and the technical
report (everything, precisely). It duplicates neither. Its job:

1. understand the problem,
2. understand the design choice,
3. see one clear measurement,
4. understand the practical implication,
5. understand the limitation,
6. know where to inspect or reproduce the evidence.

The identity to write toward is **"we make the system's design decisions,
evidence, mistakes, and workload boundaries understandable and
inspectable"** — not "we achieved another benchmark result."

## 2. Tracks

Every post declares one track, shown as a label on the card and in the
kicker. Keep the genres separate; mixing them is what made posts read as
logs.

| Track | Purpose |
|---|---|
| **Understand TGMS** | Explain a concept or architectural choice in plain language. |
| **Evidence & capability** | What experiments show about quality, coverage, or system fit. |
| **Engineering case study** | How measurement changed an implementation or a conclusion. |

A post answers **exactly one reader question**, stated in its subtitle. If
you cannot state it in one sentence, it is two posts. Detailed profiling
tables and misdiagnosis chronologies belong in an engineering note or in
`docs/engine_lessons.md`, linked — not in the main narrative.

## 3. Required structure

```
kicker:  Post #N · <track>
title:   descriptive, names the subject and not only the incident
subtitle: one sentence stating the practical question
meta:    author · date first published · last updated · TGMS version ·
         benchmark snapshot · reading time · status
```

`status` is one of **Current**, **Updated result**, or **Historical
snapshot**. A post that is not being kept current must say so at the top.

Body sections, in this order:

1. **In one sentence** — the answer, no jargon. A reader who reads only
   this, the first figure, and the takeaway must get the point.
2. **Why this matters** — a concrete situation, decision, or failure, and
   the consequence of not solving it.
3. **The idea in plain language** — at most two new terms, one conceptual
   figure.
4. **How TGMS handles it** — the design choice at architectural level.
   Internal field names only when they carry the lesson.
5. **What we measured** — the evidence box (§5).
6. **What we found** — one primary chart; state the result in words first.
7. **What this means in practice** — the user or deployment implication.
   A "good fit when / consider another approach when" box where relevant.
8. **What this result does not show** — the most important limitation,
   confound, or boundary. Not optional.
9. **Takeaway** — restate the one conclusion. Never introduce a new
   experiment or incident here.
10. **Evidence and reproduction** — report, raw records, code, snapshot,
    commit.
11. **Continue reading** — one prerequisite and one next post, named by
    topic rather than by number.
12. **Changelog** — dated entries when a result changed (§7).

Length: **1,000–1,600 words**; one conceptual figure; one or two result
charts; tables only when the reader must compare exact values.

## 4. Language

**Define a term by the problem it solves, never as a definition.** Not
"transaction time is when the store believed a fact", but: a correction
should not erase what the system previously believed, so we record both
when a fact was true and when the database held that version — the second
clock is transaction time.

First use in the main narrative uses the reader-facing form; the technical
term may follow and be used thereafter.

| Internal term | Reader-facing first use |
|---|---|
| operator | a verified data operation |
| operator DAG | a plan connecting several verified operations |
| exact match | the share of answers exactly matching the reference answer |
| correction probe | a question whose answer changes after a correction |
| canonical hash | a fingerprint confirming two systems returned the same result |
| cost guardrail | a limit that refuses work predicted to be too expensive |
| truncation taint | a marker that a result depends on incomplete data |
| edge version | one recorded version of a relationship over time |
| belief state | what the database believed at a specified past moment |

**Standardised forms** (pick one, everywhere): *bi-temporal* (hyphenated);
*current belief*; *the TGMS native engine* (not "the backend" or "the
kernel" interchangeably); *answer accuracy* with the exact metric named in
parentheses on first use; *past belief state* rather than "time travel"
where precision matters.

**Percentages in the narrative, decimals in the evidence table.** 0.408
becomes "40.8% exact-answer accuracy". Define any metric at first use.

**Calibrate every claim to the experiment.** The failure mode is a true
measurement stated as a universal property:

| Do not write | Write |
|---|---|
| graph engines lose their home turf | general graph engines were slower on these bi-temporal traversal workloads |
| quantization, not scale, is the bottleneck | the 72B result suggests 4-bit quantization may hurt structured planning; a same-model comparison is still needed |
| the baselines did not move | the baselines did not improve materially in this experiment |
| no snapshot or RAG system can express this | latest-state snapshots and the evaluated RAG configurations do not retain the belief history this needs |
| zero unsupported claims | 0 unsupported claims among the 268 evaluated answers, on this task set |

**Label what kind of fact each result is** — the same sentence reads very
differently depending on which it is:

- *architectural guarantee* — true by construction;
- *tested property* — pinned by tests or an oracle;
- *observed benchmark result* — measured, on a stated workload;
- *current implementation limitation*;
- *hypothesis or future direction*.

`docs/site_facts.json` carries a `kind` on every fact for this reason.

## 5. The evidence box

Every post that reports a measurement carries one, compactly:

- question or hypothesis;
- dataset and workload, with size;
- systems or configurations compared;
- metric, in plain language, and whether higher or lower is better;
- what was held constant;
- median/mean/single run, and the reproducibility band;
- benchmark snapshot id and a link to the raw record.

Latency cells on the measurement host reproduce to about **±20% between
days** (`reproducibility_band`). Do not present a difference inside that
band as a win; call it a tie.

## 6. Figures

Order: **conceptual figure first** (what problem is being explained), then
the result figure, then an optional trade-off figure. Several posts opened
directly with benchmark numbers; that is the most common accessibility
failure in the series.

Palette carries meaning, site-wide:

| Colour | Means |
|---|---|
| blue | TGMS |
| gray | general baselines |
| amber | a specialist winning its shape, or TGMS slower |
| green | a guarantee met, a check passing, valid time |
| purple | transaction / belief time |
| red | risk, invalid state, explicit failure, retracted claim |

**Red is not for "TGMS is slower."** A slower result is amber or gray.

Every result figure states: metric and direction; dataset and size; model
and precision where applicable; number of tasks or trials; median/mean/single
run; uncertainty or reproducibility band; snapshot id in the caption.

Captions state **the conclusion and the boundary**:

> On this 200k-event bi-temporal reachability workload, TGMS and DuckDB are
> close while the SQL and graph systems are substantially slower. This does
> not imply TGMS is faster for ordinary non-temporal graph traversal.

Alt text conveys the *result*, not the chart type. Bars must start at zero
unless the caption says otherwise; an 18% difference must look like 18%.

## 7. Corrections, and the one rule that is easy to get wrong

This project publishes its mistakes; that is a differentiator and it stays.
But **honesty is not the same as leaving stale numbers in the headline
position.** The rule:

1. **The body always shows the current result.** Regenerate the figure, the
   table, the prose, the alt text, and the index card.
2. **The correction is a dated changelog entry** at the foot of the post,
   and a `status: Updated result` in the meta.
3. Where a wrong *interpretation* was load-bearing — it aimed a decision —
   keep it in the body as a clearly framed correction box **after** the
   corrected explanation, not before it.
4. Never leave an old chart as the visual headline with a correction
   paragraph below asking the reader to mentally patch it.

Rule 4 was violated by post #9's "grouping is free" pull quote, which is
exactly why it is written down here. The claim was retracted the next day
by D-046; the fix is to state the corrected finding — exact distinct
counting dominates the remaining kernel cost — and put the retraction in a
correction box and the changelog.

Retired claims are enforced mechanically: `docs/site_facts.json` carries a
`retired_phrases` list with a reason each, and `scripts/site_facts.py check`
fails CI if one reappears anywhere on the site.

## 8. The facts pipeline

Numbers are not typed into pages. `docs/site_facts.json` holds every fact
the site asserts, with its kind, its prose form, and the snapshot it came
from. Pages quote them as:

```html
<span data-fact="operator_count">14</span> verified temporal operators
```

- `uv run python scripts/site_facts.py check` — CI gate: every marked span
  matches the source, no retired phrase appears.
- `uv run python scripts/site_facts.py apply` — rewrite marked spans after
  a fact changes.
- `uv run python scripts/site_facts.py list` — what is available to quote.

Changing a measured fact is therefore: update the snapshot and value in
`site_facts.json`, run `apply`, regenerate any figure that draws it, and
add the changelog entry. If a fact has no key, add one before quoting it.

## 9. Where this standard departs from the audit that produced it

- The audit proposed generating charts from the benchmark JSON. Worth doing
  and not yet done: the SVGs remain hand-authored, so **figure values are
  covered by `retired_phrases` and caption review rather than by
  generation.** Until a chart generator exists, any figure carrying a number
  must also carry that number in a `data-fact` span or in its caption, so
  the checker can see it.
- The audit proposed reader-persona landing paths ("I care about
  auditability…"). Deferred: nine posts do not yet justify the navigation
  surface. The Start Here order and track labels ship instead.
- The audit proposed dropping incident-led titles. Adopted only partly:
  the title must name the subject, but an incident may lead the *opening*.
  The incidents are the most-read part of this blog and removing them would
  trade the series' voice for symmetry it does not need.

## 10. Checklist before publishing

**Purpose** — one reader question answered; title names the subject; the
opening explains why it matters before any implementation detail; the
conclusion restates the same message.

**Accessibility** — every specialised term defined at first use; decimals
translated to percentages in the narrative; code is evidence, not the only
explanation; a concrete example precedes every abstraction.

**Evidence** — metric, dataset, model, sample size and snapshot stated;
comparisons semantically equivalent; claims no broader than the experiment;
limitations explicit; current and historical results never mixed.

**Figures** — one claim each; axes, units and direction clear; captions
give takeaway and boundary; colours follow §6; alt text states the result;
values traceable to the same record as the report.

**Maintenance** — TGMS version and last-updated visible; index card matches
the post; project page matches the post; reproduction links point at an
immutable commit; `scripts/site_facts.py check` passes.
