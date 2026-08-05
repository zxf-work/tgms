#!/usr/bin/env python3
"""Independent-question study (CIDR review §10): classification, gold, suite.

110 questions were written by students who saw only a natural-language
dataset description (benchmarks/independent-v1/raw_questions.txt) — never
the operator list, the repo, or existing benchmark tasks.

Each question is classified against the fixed 13-operator algebra:
  1 directly expressible (single operator + answer readout)
  2 expressible by operator composition
  3 requires an unimplemented capability (see `need` tags)
  4 ambiguous / mistaken presupposition as written
  5 not a computation-over-the-log question
`need` tags: G grouped/distinct aggregation over accounts or pairs;
CAL calendar semantics (weekday, hour-of-day, same-calendar-unit);
AR arithmetic beyond count/sum/min/max/topk (diff, ratio, avg, median, %);
SET set operations over uid sets; PROP property-filtered pattern operators
(rating sign in motifs/reachability); GLOB global event/version scan-select
(argmax over all events, correction records); NEG absence conditions.

Class 1-2 questions with a scoreable answer kind and no compound answer
run verbatim (run=True); gold is computed two independent ways (SQL over
the canonical DuckDB store; pure Python over the event rows) and must
agree.

`C` is the *pre-registered* 13-operator classification (D-026, hand-audited
2026-07-26, before any operator was built to close a gap). It is never
rewritten. `C14` is the re-audit after D-044 added the 14th operator,
`aggregate_events`, and holds only the entries whose class or need-tags
change; everything absent from `C14` keeps its `C` verdict verbatim. Keeping
both tables side by side is the point: the coverage delta is a diff against
a table nobody could tune after the fact. Class 4 (ambiguous) and class 5
(not a computation) entries never appear in `C14` — a new operator cannot
repair a question's presupposition. `C14` re-audits the *whole* need string
of every entry it touches, so a residual capability the pre-registration had
folded into `G` can surface under its own tag; one tag is new, `SEQ`
(per-group ordered-sequence work: gaps between consecutive events,
sessionization, arbitrary sliding windows, first-reply latency), and under
`C14` `SET` also covers pair-set joins (reciprocity), which the
pre-registration counted as `G`. `G` survives in `C14` only where grouped
aggregation itself is still out of reach — chiefly groupings needing more
than the operator's two dimensions.

`C15` is the third table, the re-audit after D-051's session extended the
existing O13 `compute` operator with the `AR` arithmetic — `mean` and
`median` over a prior step's rows, and `ratio`/`diff`/`percent` over two
scalars from earlier steps. It chains onto `C14` exactly as `C14` chains
onto `C`: an entry absent from `C15` keeps its effective 14-operator
verdict. Two tags are new, and they exist because `AR` turned out to be
three capabilities wearing one label:
  ROW  row-wise arithmetic over a prior step's rows — a derived column
       (`max_vt_s - min_vt_s` per group, a per-row ratio of two fields, a
       floor-divide to a calendar unit), or a mean taken *per group* rather
       than over the whole result. `compute` reduces a row set to one
       number; it cannot yet add a column to one.
  PCT  rank or percentile selection beyond `topk` — "top 1% of accounts",
       "the bottom 50% of senders", "top 10% most active".
`AR` itself is retired by `C15`: after this session no class-3 entry is
blocked by arithmetic that the operator now performs, and every entry that
still needs a number computed needs it in one of the two shapes above.
That split is the session's main finding and it is why the measured delta
(4 questions) came in below the +7 the handoff projected from the
sole-blocker count.

`C16` is the fourth table, the re-audit after D-052's property typing shipped
in `aggregate_events`: `prop_filter` (a predicate on an edge property) and
min/max/mean over `of: "prop"`, each under the rule that a value participates
only if its JSON type fits, that text is never parsed into a number, and that
excluded rows are counted per property. **Ten questions moved, 28 -> 38 of
110** — nine of the thirteen `PROP` was the sole blocker of, plus bo-Q8. One
tag is new:
  PROJ projecting a property *value* into output rows so two rows' values can
       be compared with each other. An aggregate reduces a property to one
       number per group and a predicate compares it to a literal; neither
       hands the value back.
`PROP` survives on only five entries, and all five want the predicate *inside
another operator* — the motif catalogue or the path operators, which this
work did not touch. That is a far more specific claim than the tag carried
before, and it is the third time in a row that building a capability showed
its tag had been naming the first obstacle rather than the set.

`C17` is the fifth table, after D-054 shipped the `SET` capability: set
operations over two uid lists in `compute` (intersect/difference/union), an
`endpoint_filter` cohort pre-filter on `aggregate_events`, and `pair_mode`
(undirected / reciprocal) over an (src, dst) grouping. **Fourteen questions
moved, 38 -> 52 of 110** — the closest any prediction has come, 14 against
16. One tag is new:
  JOIN aligning two grouped results on their key so fields from both rows can
       be used together ("A's count minus B's count, per account"). The set
       operations answer *which uids*; they cannot carry a value across from
       one result to the other.
`SET` survives on four entries. That is the fourth tag to split on contact —
G hid SET and SEQ, AR hid ROW and PCT, PROP hid PROJ, SET hid JOIN — and the
board is now flat: ROW 18, SEQ 14, JOIN 13, then a long tail.

`C18` is the sixth table, after D-055 shipped `derive` and `join`. Fifteen
questions moved, 52 -> 67, against fifteen predicted — the first exact
forecast of the campaign. It re-audited **no class-3 entry**, and C19 below
is where that shows.

`C19` is the seventh, after D-056's sequence aggregates: `max_gap`,
`max_in_window` over a sliding span, and `max_session_span`. Five questions
moved, 67 -> 72 — but only **four** on the capability. The fifth, cm-Q41,
moved because the earlier verdict was wrong, and `REREAD` below keeps that
kind of correction out of any capability's delta. Two tags are new and two
retire:
  EGO  *new* — an account's events **in either role, in one group**. D-054's
       `endpoint_filter` unions the two roles in the population; grouping is
       still by `src` or `dst`, so "no messages sent or received" cannot be
       asked of every account at once. cm-Q14 and cm-Q24 carried `SEQ` and a
       gap aggregate was never their obstacle.
  GMEAN *new* — a reducer taken per group over a prior step's rows. `ROW`
       has covered this since C15 ("or a mean taken per group") and only its
       derived-column half shipped.
  ROW  retired, and JOIN with it: both named capabilities that shipped in
       D-055, and both kept appearing on the board for a whole session
       because C18 re-audited nothing. `ROW` read as blocking 3 questions
       and `JOIN` 1 when the honest counts were 0.
`SEQ` survives on seven entries and is four distinct residual shapes, none
of them what shipped: a lag between events of *opposite directions* within a
pair, a *distinct* count inside a sliding window, a first-k/last-k slice per
group, and a gap whose two ends must both be sends.

`C20` is the eighth, after D-057's `calendar_unit` dimension: three cyclic
units (`hour_of_day`, `day_of_week`, `month_of_year`) at a fixed offset from
UTC. Seven questions moved, 72 -> 79, of which **five** are the capability;
the other two are `REREAD`. The per-question forecast was pre-registered in
the `[tests]` commit and came in **8 of 8 correct**, including two cells
where reading the question contradicted its tag — cm-Q53 moved despite
`NEG`, and cm-Q19 stayed for a reason (`PAT`: the motif catalogue has no
2-edge delta-path at all) that has nothing to do with calendars.

The two re-reads are the finding. They are tags left behind by **D-054 and
D-055**, not by the session immediately before — cm-Q40's absence condition
is a cohort pre-filter plus `difference`, and bo-Q11's "lowest average among
accounts with at least 10 ratings" contains no percentile at all. Three
consecutive re-audits have now cleaned up after their predecessors, so
`NO_CLASS3_AUDIT` below turns that lesson into a check.

`CAL` survives on three entries and means something narrower: a calendar
predicate *inside* another operator (cm-Q19, bo-Q32), or an **absolute**
calendar unit rather than a cyclic one (bo-Q41's date). LDBC's five `CAL`
templates are all the absolute kind, which is why none of them moved.

`C21` is the ninth, after D-058 shipped O15 `version_history` — the belief
log as rows — and `compute filter`'s `field2`. Four questions moved,
79 -> 83, and **all four are the capability**: two each, exactly as the two
were forecast separately. The per-question forecast was 7 of 7, the second
session running to be right in every cell.

**`GLOB` retires completely, and it is the campaign's cleanest split.** The
tag named three unrelated things: a scan over the version log, a row-wise
`src == dst`, and a longest time-respecting chain. Two shipped here; the
third is `CHAIN` *new*, and is not built. bo-Q41 keeps neither half — the
version log answers which corrections exist, and what remains is an
absolute calendar date plus regrouping a result.

The board is now 25 blocked with nothing above 7 and nothing sole above 3.
Seven of the twelve surviving tags name a capability *inside another
operator* or a residual shape rather than a missing operator, which is a
different kind of board than the one this campaign started against.

Usage:
  python3 scripts/independent_questions.py report  # tables only, no stores
  python3 scripts/independent_questions.py build   # writes suites + gold;
                                # needs the canonical stores on this host
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

MICROS_PER_DAY = 86_400_000_000
OPEN_END = 2**62
RAW = Path("benchmarks/independent-v1/raw_questions.txt")

# --------------------------------------------------------------------------- #
# classification (hand-audited against the operator registry, 2026-07-26)      #
# --------------------------------------------------------------------------- #
# value = (class, need/ops, run, note)
C: dict[tuple[str, int], tuple[int, str, bool, str]] = {
    # ---- CollegeMsg (extent 2004-04-15 .. 2004-10-26) ----
    ("cm", 1): (3, "G", False, "distinct senders in window"),
    ("cm", 2): (3, "G", False, ""),
    ("cm", 3): (3, "G", False, "empty month (data start Apr 15)"),
    ("cm", 4): (3, "G", False, "partial window vs data extent"),
    ("cm", 5): (2, "graph_metric_timeseries+topk", True, "daily buckets"),
    ("cm", 6): (3, "GLOB", False, "global longest cascade"),
    ("cm", 7): (3, "G", False, "empty week (Jan)"),
    ("cm", 8): (2, "entity_history+filter+min", False,
                "compound answer (who + date) not scoreable"),
    ("cm", 9): (3, "G", False, "per-account sliding window"),
    ("cm", 10): (3, "G,SET", False, ""),
    ("cm", 11): (3, "AR,G", False, "average"),
    ("cm", 12): (2, "entity_history+filter", True, "incoming on one day"),
    ("cm", 13): (3, "G", False, "distinct pairs; near count_temporal_motifs"),
    ("cm", 14): (3, "G", False, "argmax gap per account"),
    ("cm", 15): (3, "CAL,AR", False, "hour-of-day grouping, %"),
    ("cm", 16): (3, "G", False, "per-contact first-in/first-out compare"),
    ("cm", 17): (3, "G", False, ""),
    ("cm", 18): (2, "graph_metric_timeseries+sum", True, "first-week burst"),
    ("cm", 19): (3, "CAL", False, "same-calendar-day constraint; near CTM"),
    ("cm", 20): (3, "CAL,G", False, "weekday grouping"),
    ("cm", 21): (3, "G,CAL", False, ""),
    ("cm", 22): (3, "AR,G", False, "median"),
    ("cm", 23): (3, "G", False, "top-3 distinct recipients"),
    ("cm", 24): (3, "G", False, "per-account gap pattern"),
    ("cm", 25): (3, "GLOB", False, "src==dst predicate unsupported"),
    ("cm", 26): (2, "entity_history+min+interval_relation", False,
                 "categorical answer (sent-first/received-first)"),
    ("cm", 27): (3, "AR", False, "difference of two counts"),
    ("cm", 28): (3, "G,SET,NEG", False, ""),
    ("cm", 29): (3, "AR,G", False, "ratio"),
    ("cm", 30): (3, "G,CAL", False, "empty months Jan-Mar"),
    ("cm", 31): (3, "G", False, "per-account 1h window argmax"),
    ("cm", 32): (3, "CAL", False, "weekday + hour-of-day"),
    ("cm", 33): (3, "G,AR", False, "median over top-10 group"),
    ("cm", 34): (3, "G,AR", False, ""),
    ("cm", 35): (3, "G", False, "per-pair sessionization"),
    ("cm", 36): (3, "SET", False, "uid-set difference; near entity_history"),
    ("cm", 37): (3, "GLOB", False, "global argmax event; motif rows nested"),
    ("cm", 38): (3, "GLOB", False, "duplicate of Q25"),
    ("cm", 39): (3, "G,CAL", False, ""),
    ("cm", 40): (3, "G,NEG", False, ""),
    ("cm", 41): (3, "G,AR,CAL", False, "weekly new-contact average"),
    ("cm", 42): (3, "SET,CAL", False, "join on (dst, same-day)"),
    ("cm", 43): (3, "AR,CAL", False, "compare two buckets; Feb empty"),
    ("cm", 44): (3, "G,AR", False, "average reply time argmax"),
    ("cm", 45): (3, "G,SET", False, "Jan empty -> degenerate"),
    ("cm", 46): (2, "graph_metric_timeseries+filter", False,
                 "yes/no answer kind not scoreable"),
    ("cm", 47): (3, "G", False, "second-highest rank"),
    ("cm", 48): (3, "G,CAL", False, ""),
    ("cm", 49): (3, "G", False, "per-recipient join of two accounts"),
    ("cm", 50): (3, "G", False, "pairs with exactly one message"),
    ("cm", 51): (3, "G", False, ""),
    ("cm", 52): (3, "G,AR", False, "ratio; March empty"),
    ("cm", 53): (3, "G,CAL", False, ""),
    ("cm", 54): (3, "G,AR", False, "top-1% share"),
    ("cm", 55): (2, "entity_history+max+interval_relation", False,
                 "categorical answer (before/after)"),
    # ---- Bitcoin-OTC (extent 2010-11-08 .. 2016-01-25) ----
    ("bo", 1): (3, "G", False, ""),
    ("bo", 2): (3, "G", False, ""),
    ("bo", 3): (2, "graph_metric_timeseries+sum", True, "total rating events"),
    ("bo", 4): (3, "GLOB,PROP", False, "global selection by rating value"),
    ("bo", 5): (2, "graph_metric_timeseries+topk", True, "daily buckets"),
    ("bo", 6): (3, "AR,PROP", False, "percentage"),
    ("bo", 7): (3, "G,SET", False, ""),
    ("bo", 8): (3, "AR,CAL,G", False, "yearly averages"),
    ("bo", 9): (3, "G", False, ""),
    ("bo", 10): (3, "G", False, ""),
    ("bo", 11): (3, "G,AR", False, ""),
    ("bo", 12): (3, "G,AR", False, ""),
    ("bo", 13): (3, "GLOB,PROP", False, "global min over rating property"),
    ("bo", 14): (3, "G,AR,PROP", False, ""),
    ("bo", 15): (3, "G,PROP", False, ""),
    ("bo", 16): (3, "G", False, "per-account first rating"),
    ("bo", 17): (3, "G", False, ""),
    ("bo", 18): (3, "G", False, "any-account burst; burst_detection is per-uid"),
    ("bo", 19): (3, "G,CAL", False, ""),
    ("bo", 20): (3, "G,AR", False, "median"),
    ("bo", 21): (2, "graph_metric_timeseries+filter", False,
                 "yes/no answer kind not scoreable"),
    ("bo", 22): (3, "G,CAL", False, ""),
    ("bo", 23): (3, "G,AR", False, ""),
    ("bo", 24): (3, "G", False, "distinct reciprocal pairs; near CTM"),
    ("bo", 25): (3, "AR,PROP,G", False, ""),
    ("bo", 26): (3, "G,PROP", False, ""),
    ("bo", 27): (3, "G", False, "re-rating detection per pair"),
    ("bo", 28): (3, "G", False, ""),
    ("bo", 29): (3, "G,PROP", False, ""),
    ("bo", 30): (3, "G,PROP", False, ""),
    ("bo", 31): (3, "NEG,PROP,G", False, "absent-edge condition"),
    ("bo", 32): (3, "PROP,CAL", False, "sign filter in path motif"),
    ("bo", 33): (3, "G,PROP", False, ""),
    ("bo", 34): (3, "PROP,GLOB", False, ""),
    ("bo", 35): (3, "PROP,G", False, ""),
    ("bo", 36): (3, "PROP", False, "signed directed 2-hop; snapshot is "
                                   "undirected, unsigned"),
    ("bo", 37): (3, "PROP", False, "sign filter in cyclic motif"),
    ("bo", 38): (4, "", False, "clock conflation: presupposes the database "
                 "held beliefs on 2013-01-01; transaction time begins at the "
                 "2026 ingest, so the belief state at that tt is empty"),
    ("bo", 39): (5, "", False, "presupposes a specific correction (n77, "
                 "2012-03-15) that does not exist in the data"),
    ("bo", 40): (3, "GLOB", False, "global version/correction scan"),
    ("bo", 41): (3, "GLOB,CAL", False, ""),
    ("bo", 42): (3, "G,AR", False, "also clock conflation (2012 tt)"),
    ("bo", 43): (3, "G,AR", False, "also clock conflation (2011 tt)"),
    ("bo", 44): (3, "G", False, "also clock conflation"),
    ("bo", 45): (3, "GLOB", False, "event-time vs record-time comparison"),
    ("bo", 46): (3, "G,PROP,AR", False, ""),
    ("bo", 47): (3, "G,AR", False, ""),
    ("bo", 48): (3, "G,PROP", False, ""),
    ("bo", 49): (3, "G,PROP", False, ""),
    ("bo", 50): (3, "G,PROP,AR", False, ""),
    ("bo", 51): (3, "G,CAL", False, ""),
    ("bo", 52): (3, "G", False, "depends on top-10 group-by"),
    ("bo", 53): (3, "G,SET", False, ""),
    ("bo", 54): (3, "G,AR", False, ""),
    ("bo", 55): (3, "AR", False, "near-miss: sum and count expressible for "
                                 "the named account, but no division"),
}

# --------------------------------------------------------------------------- #
# re-audit against the 14-operator algebra (D-044 `aggregate_events`)          #
# --------------------------------------------------------------------------- #
# Only entries whose class or need-tags change; `C` stays as pre-registered.
# value = (class, need/ops, justification)
#
# What the 14th operator does and does not give (D-044, re-read per question):
#   groups events (believed edge versions, t_a <= vt_s < t_b) by AT MOST TWO
#   dimensions from {time_bucket(stride), rel_type, endpoint(src|dst),
#   label(src|dst)} and computes from {count, count_distinct(src|dst),
#   min|max|mean(vt_s|duration)}; `rows_total` is the pre-limit group count.
#   No aggregate over JSON props (so nothing about ratings); no calendar units
#   (buckets are fixed strides anchored at t_a — days and weeks yes, months,
#   years, weekday and hour-of-day no); no uid pre-filter (recovered only
#   post-hoc by `compute` filter on the emitted src/dst field); no set ops, no
#   joins, no absence, and no arithmetic past the listed aggregates.
# Readout chains use the existing O13 `compute`
#   {count, sum, min, max, topk(field,k), filter(field,cmp,value)} over a
#   prior step's rows via $ref. Page ceiling: limit <= 10,000 and $ref list
#   projections truncate at 10,000 items, which matters once per table below.
C14: dict[tuple[str, int], tuple[int, str, str]] = {
    # ---- CollegeMsg ----
    ("cm", 1): (1, "aggregate_events",
                "one call: group_by [], count_distinct src, window = April "
                "2004; the answer is the row's distinct_src"),
    ("cm", 2): (1, "aggregate_events",
                "one call: group_by [], count_distinct dst over the full "
                "window; the answer is distinct_dst"),
    ("cm", 3): (2, "aggregate_events+topk",
                "group_by [endpoint src] count over March 2004, then compute "
                "topk(count,1); March precedes the data extent (starts Apr "
                "15) so the chain runs and correctly returns no account"),
    ("cm", 4): (2, "aggregate_events+topk",
                "group_by [endpoint dst], count_distinct src over the window, "
                "then compute topk(distinct_src,1)"),
    ("cm", 7): (2, "aggregate_events+filter+count",
                "group_by [endpoint src] with count and min vt_s over the "
                "whole log, compute filter(count eq 1), two filters bounding "
                "min_vt_s to the week, then count; the week precedes the data "
                "extent so the answer is 0"),
    ("cm", 9): (3, "SEQ",
                "per-(day, sender) counts are expressible, but buckets are "
                "fixed strides anchored at t_a, not the arbitrary 24-hour "
                "periods asked for"),
    ("cm", 10): (3, "SET",
                 "both uid sets are enumerable by endpoint grouping; their "
                 "difference is not an operator"),
    ("cm", 11): (3, "AR",
                 "group_by [time_bucket, endpoint src] count gives the "
                 "per-account-per-day counts; the mean over them needs "
                 "division, which compute does not have"),
    ("cm", 13): (3, "SET,SEQ",
                 "pair grouping is expressible; matching A->B against B->A "
                 "within an hour is a pair-set self-join under a time bound"),
    ("cm", 14): (3, "SEQ,SET",
                 "min/max vt_s give an account's endpoints, not its largest "
                 "inter-event gap; 'sent or received' also unions the two "
                 "endpoint roles"),
    ("cm", 16): (3, "SET",
                 "per-counterpart first-in and first-out are two aggregate "
                 "results; comparing them needs a keyed join"),
    ("cm", 17): (3, "SET",
                 "'exchanged' intersects the pair set with its transpose "
                 "before any per-account distinct count"),
    ("cm", 20): (3, "CAL",
                 "weekday is not a dimension and not a fixed stride"),
    ("cm", 21): (3, "SET,AR",
                 "per-src and per-dst min vt_s are both expressible; matching "
                 "them per account is a join, and 'different day' needs a "
                 "floor-divide on the two minima"),
    ("cm", 22): (3, "AR,SET",
                 "reciprocal-pair join plus a median"),
    ("cm", 23): (2, "aggregate_events+topk",
                 "group_by [endpoint src], count_distinct dst over the final "
                 "two weeks, then compute topk(distinct_dst,3)"),
    ("cm", 24): (3, "SEQ,SET",
                 "consecutive-gap detection over the union of an account's "
                 "sent and received events"),
    ("cm", 28): (3, "SET,NEG",
                 "count_distinct dst per src settles the >=5 side; 'never "
                 "received' is a set difference"),
    ("cm", 29): (3, "AR,SET",
                 "sent and received counts per account are two groupings; the "
                 "ratio needs a join and a division"),
    ("cm", 30): (3, "CAL,SET",
                 "months are not fixed strides, and 'every month' is an "
                 "intersection across buckets"),
    ("cm", 31): (3, "SEQ",
                 "argmax over arbitrary 1-hour windows inside each account's "
                 "event sequence"),
    ("cm", 33): (3, "AR",
                 "one call gives count and count_distinct dst per sender and "
                 "topk(count,10) the cohort; only the median is missing"),
    ("cm", 34): (3, "SET,AR",
                 "pair-set transpose join, then a 'more than double' ratio"),
    ("cm", 35): (3, "SEQ",
                 "sessionization by 60-minute gaps inside each pair"),
    ("cm", 37): (2, "aggregate_events+aggregate_events",
                 "s1 group_by [] max vt_s over the log; s2 group_by [endpoint "
                 "src, endpoint dst] count with window "
                 "[$ref s1.rows[0].max_vt_s, OPEN_END) — exactly the last "
                 "event, with both endpoints"),
    ("cm", 39): (3, "G,SET",
                 "needs three dimensions (day x src x dst) against a cap of "
                 "two, and sums both directions of the pair"),
    ("cm", 40): (3, "SET,NEG",
                 "'only ever sent to accounts that never sent' is a set "
                 "difference under an absence condition"),
    ("cm", 41): (3, "AR,SEQ",
                 "group_by [endpoint src, time_bucket] count_distinct dst "
                 "gives weekly recipients (filter src post-hoc); 'new' needs "
                 "dedup against earlier buckets and the average needs "
                 "division"),
    ("cm", 42): (3, "G,SET",
                 "day bucketing is now a stride, but pinning both senders "
                 "needs a third dimension (day x src x dst), then an "
                 "intersection"),
    ("cm", 44): (3, "AR,SEQ",
                 "first-reply latency per sender is a lag, and the argmax is "
                 "over an average"),
    ("cm", 45): (3, "SET",
                 "the two per-window activity sets are expressible; their "
                 "union across roles and difference across windows are not"),
    ("cm", 47): (2, "aggregate_events+topk",
                 "group_by [endpoint src], count_distinct dst over April "
                 "2004, compute topk(distinct_dst,2); the answer is rows[1]"),
    ("cm", 48): (3, "SET,AR",
                 "(src, day) counts and per-dst min vt_s are both "
                 "expressible; matching them needs a join plus a "
                 "floor-divide of the minimum to a day"),
    ("cm", 49): (3, "SET,AR",
                 "per-(src,dst) min vt_s is one call; joining the two "
                 "accounts on recipient and differencing is not"),
    ("cm", 50): (2, "aggregate_events+filter+count",
                 "group_by [endpoint src, endpoint dst] count over the log, "
                 "compute filter(count eq 1), then count; CollegeMsg's 20,296 "
                 "pair groups exceed the 10,000-row page, so this one readout "
                 "needs three cursor pages summed"),
    ("cm", 51): (3, "SET,SEQ",
                 "'first message to B, then a reply back' is a pair-set "
                 "transpose join with an ordering constraint"),
    ("cm", 52): (3, "AR",
                 "per-sender counts and the top-10 are expressible; the "
                 "bottom-50% percentile and the ratio are not"),
    ("cm", 53): (3, "CAL,NEG",
                 "an hour-of-day band, plus 'no message outside it'"),
    ("cm", 54): (3, "AR",
                 "per-account counts are expressible; the 1% percentile and "
                 "the share of the total are not"),
    # ---- Bitcoin-OTC ----
    ("bo", 1): (1, "aggregate_events",
                "one call: group_by [], count_distinct src over the full "
                "window"),
    ("bo", 2): (1, "aggregate_events",
                "one call: group_by [], count_distinct dst, window = 2012 "
                "(a window, not a calendar bucket)"),
    ("bo", 4): (3, "PROP",
                "a global count is now group_by [] count, but the predicate "
                "is on the rating prop, and prop aggregates/filters are "
                "explicitly deferred by D-044"),
    ("bo", 7): (3, "SET",
                "each side is a count_distinct; 'both' is an intersection of "
                "the two uid sets"),
    ("bo", 8): (3, "AR,CAL,PROP",
                "years are not fixed strides and the mean is over the rating "
                "prop"),
    ("bo", 9): (2, "aggregate_events+topk",
                "group_by [endpoint src] count over the log, then compute "
                "topk(count,1)"),
    ("bo", 10): (2, "aggregate_events+topk",
                 "group_by [endpoint dst] count over the log, then compute "
                 "topk(count,1)"),
    ("bo", 11): (3, "AR,PROP",
                 "filter(count ge 10) is expressible; the mean is over the "
                 "rating prop"),
    ("bo", 12): (3, "AR,PROP",
                 "same shape as bo-Q11 on the giving side"),
    ("bo", 13): (3, "PROP",
                 "an argmin over the rating prop; the window-reselect idiom "
                 "that closes cm-Q37 needs the extremum to be over vt_s"),
    ("bo", 14): (3, "AR,PROP",
                 "counts of positive vs negative need a prop filter, and the "
                 "spread needs a subtraction"),
    ("bo", 15): (3, "PROP",
                 "'all ratings <= 0' is a predicate over the rating prop"),
    ("bo", 16): (2, "aggregate_events+filter+count",
                 "group_by [endpoint dst], aggregates [min vt_s] over the "
                 "whole log, compute filter(min_vt_s ge 2013-01-01), "
                 "filter(min_vt_s lt 2014-01-01), then count"),
    ("bo", 17): (3, "SEQ",
                 "the longest gap between consecutive events in a group is a "
                 "lag; min/max vt_s only give the two endpoints"),
    ("bo", 18): (3, "SEQ",
                 "arbitrary 24-hour periods, not buckets anchored at t_a"),
    ("bo", 19): (3, "CAL,SET",
                 "years are not fixed strides and 'each of' is an "
                 "intersection"),
    ("bo", 20): (3, "AR,SET",
                 "reciprocal-pair join, then a median"),
    ("bo", 22): (3, "SET,AR",
                 "per-src and per-dst min vt_s are expressible; the match is "
                 "a join and 'same day' a floor-divide of two minima"),
    ("bo", 23): (3, "AR",
                 "one call gives count, min vt_s and max vt_s per rater and "
                 "filter(count ge 50) the cohort; the span in days and its "
                 "average both need division"),
    ("bo", 24): (3, "SET",
                 "the pair set intersected with its transpose"),
    ("bo", 25): (3, "AR,PROP,SET",
                 "reciprocal-pair join, rating signs, and a percentage"),
    ("bo", 26): (3, "PROP,SET",
                 "reciprocal-pair join plus rating signs"),
    ("bo", 27): (3, "PROP,SEQ",
                 "'later, with a different value' compares consecutive "
                 "ratings' props inside a pair"),
    ("bo", 28): (3, "SET",
                 "directed pair counts are one call; summing the two "
                 "directions is a transpose join"),
    ("bo", 29): (3, "PROP,SET,SEQ",
                 "pair-set transpose join, rating comparison, and 'previously "
                 "rated' ordering"),
    ("bo", 30): (3, "PROP,SET",
                 "reciprocal-pair join, then sign categories"),
    ("bo", 31): (3, "NEG,PROP",
                 "a positive-sign triad with an absent third edge; grouping "
                 "was never the obstacle here"),
    ("bo", 33): (3, "PROP",
                 "a mutually-positive clique condition over rating signs"),
    ("bo", 35): (3, "PROP,NEG",
                 "two-hop positive reachability with a 'never directly "
                 "rated' exclusion"),
    ("bo", 42): (3, "AR,SET",
                 "as_of_tt makes each snapshot's per-account count one call; "
                 "the per-account difference across snapshots needs a join "
                 "and a subtraction"),
    ("bo", 43): (3, "AR,PROP",
                 "as_of_tt fixes the belief state and filter(count ge 5) the "
                 "cohort; the mean is over the rating prop"),
    ("bo", 44): (3, "AR,PROP,SET",
                 "positive-rating counts need a prop filter, and 'decreased' "
                 "needs a cross-snapshot join and comparison"),
    ("bo", 46): (3, "AR,PROP",
                 "min vt_s per dst is expressible, but the sign of that first "
                 "rating is a prop, and the tail is a percentage"),
    ("bo", 47): (3, "AR,PROP,SEQ",
                 "'first 5 vs last 5' is per-group sequence slicing over "
                 "rating props, then a difference of means"),
    ("bo", 48): (3, "PROP",
                 "filter(count ge 5) is expressible; 'all > 0' is a prop "
                 "predicate"),
    ("bo", 49): (3, "PROP",
                 "mirror of bo-Q48"),
    ("bo", 50): (3, "AR,PROP",
                 "a prop-conditioned cohort and a percentage"),
    ("bo", 51): (3, "SET",
                 "(day, src) and (day, dst) groupings are both single calls; "
                 "the coincidence is their intersection"),
    ("bo", 52): (3, "G,SET",
                 "top-10 by received count is expressible; the first rater "
                 "per account is an argmin over a second, pair-level grouping "
                 "joined back to that list"),
    ("bo", 53): (3, "SET",
                 "the two per-window rater sets are single calls; the "
                 "difference is not"),
    ("bo", 54): (3, "AR,SET",
                 "given and received counts are two groupings; the top-10% "
                 "percentile and the ratio are not"),
}


# --------------------------------------------------------------------------- #
# re-audit against the 15th capability (D-051 session: `compute` arithmetic)   #
# --------------------------------------------------------------------------- #
# Chains onto the effective C14 verdict; absent entries keep it. Only entries
# whose class or need-tags change appear.
# value = (class, need/ops, justification)
#
# What the extended `compute` does and does not give:
#   REDUCERS over a prior step's rows -> one number: count, sum, min, max,
#   mean(field), median(field). BINARY over two scalars arriving by $ref:
#   ratio(x,y) = x/y, diff(x,y) = x-y, percent(x,y) = 100*x/y. Integer inputs
#   stay exact; anything inexact spends exactly one IEEE rounding.
#   It does NOT add a derived column to a row set (that is `ROW`), does not
#   aggregate per group (a mean is over the whole input, not per key), and
#   does not select by rank or percentile beyond `topk` (that is `PCT`).
C15: dict[tuple[str, int], tuple[int, str, str]] = {
    # ---- became expressible ---- #
    ("cm", 11): (2, "aggregate_events+mean",
                 "group_by [time_bucket(1 day), endpoint src] count over May "
                 "2004 emits one row per account that was active that day — "
                 "the question's own restriction, and the operator's "
                 "non-empty-groups contract — then compute mean(count). "
                 "Measured on the raw log: 7,060 (day, sender) groups in May "
                 "2004, inside one 10,000-row page, so the chain is two "
                 "calls with no cursor walk"),
    ("cm", 27): (2, "aggregate_events+aggregate_events+diff",
                 "one group_by [] count per day-window (2004-06-01 and "
                 "2004-06-30, both inside the data extent), then "
                 "compute diff(x=$ref s1, y=$ref s2)"),
    ("cm", 33): (2, "aggregate_events+topk+median",
                 "group_by [endpoint src] with count and count_distinct dst "
                 "over the whole log (1,350 sender groups, one page), "
                 "topk(count, 10) for the cohort, then "
                 "median(distinct_dst) — the one thing C14 said was missing"),
    ("cm", 43): (2, "aggregate_events+aggregate_events+diff",
                 "the two named weeks are literal windows, not calendar "
                 "buckets, which is the distinction bo-Q2 already turned on "
                 "in C14 ('a window, not a calendar bucket'); two group_by [] "
                 "counts and a diff, whose sign is the answer. February 2004 "
                 "precedes the data extent, so the chain runs and correctly "
                 "reports no change — the cm-Q3 situation"),
    # ---- arithmetic no longer the blocker; the other tags still are ---- #
    ("bo", 6): (3, "PROP",
                "percent(x, y) supplies the percentage; 'greater than 0' is "
                "still a predicate over the rating prop"),
    ("bo", 8): (3, "CAL,PROP",
                "mean is now an operator, but it cannot reach a rating: the "
                "value lives in untyped JSON props"),
    ("bo", 11): (3, "PROP", "as bo-Q8: the mean exists, the rating does not"),
    ("bo", 12): (3, "PROP", "mirror of bo-Q11 on the giving side"),
    ("bo", 25): (3, "PROP,SET",
                 "reciprocal-pair join and rating signs remain; the "
                 "percentage of the two resulting counts is now one call"),
    ("bo", 43): (3, "PROP",
                 "as_of_tt fixes the belief state and filter(count ge 5) the "
                 "cohort; the mean is over the rating prop"),
    ("bo", 46): (3, "PROP,SET",
                 "the tail percentage is now expressible; the sign of the "
                 "first rating is a prop, and 'of those ... later' restricts "
                 "one per-account result by another, which is a join"),
    ("bo", 50): (3, "PROP,SET",
                 "percent closes the readout; the cohort needs a mean over "
                 "rating props and then a uid pre-filter by that cohort, "
                 "which `aggregate_events` does not have"),
    ("bo", 55): (3, "PROP",
                 "mean closes the arithmetic half, and this corrects the "
                 "pre-registered note: the sum was never expressible either. "
                 "No operator projects an *edge* property as a numeric "
                 "field — entity_history and snapshot_subgraph both build "
                 "edge rows from _edge_rows (eid, vid, src, dst, rel_type, "
                 "vt_s, vt_e), and diff_snapshots exposes props only as "
                 "before/after pairs for versions that changed"),
    ("cm", 15): (3, "CAL",
                 "percent supplies the readout; a 12:00-18:00 band across "
                 "every day in the log is an hour-of-day predicate, not a "
                 "window"),
    ("cm", 41): (3, "SEQ",
                 "the average over weekly counts is now mean(field); 'new' "
                 "still needs dedup against every earlier bucket"),
    # ---- residual arithmetic, renamed to the shape it actually needs ---- #
    ("bo", 14): (3, "ROW,PROP,SET",
                 "positive and negative received counts are two "
                 "prop-filtered groupings; aligning them per account is a "
                 "join and the difference is per row, not per result set"),
    ("bo", 20): (3, "ROW,SET",
                 "median is now an operator, but the values it would take "
                 "the median of are per-pair time differences — a derived "
                 "column over the transpose join"),
    ("bo", 22): (3, "ROW,SET",
                 "join the two per-account minima, then floor-divide each "
                 "row to a day and compare the two fields"),
    ("bo", 23): (3, "ROW",
                 "the cohort and its min/max vt_s are one call, and mean is "
                 "now available — but the span it should average is "
                 "max_vt_s - min_vt_s *per row*, and there is no way to "
                 "write that column"),
    ("bo", 42): (3, "ROW,SET",
                 "two as_of_tt groupings joined per account, then a per-row "
                 "difference against a threshold"),
    ("bo", 44): (3, "ROW,PROP,SET",
                 "'decreased' compares two fields of the joined row; filter "
                 "compares a field to a literal"),
    ("bo", 47): (3, "ROW,PROP,SEQ",
                 "first-5 and last-5 slices are sequence work, their means "
                 "are per group, and the flip is their per-row difference"),
    ("bo", 54): (3, "PCT,ROW,SET",
                 "the top 10% is a percentile cut, and the given/received "
                 "ratio is per account rather than over two scalars"),
    ("cm", 21): (3, "ROW,SET",
                 "as bo-Q22: a join, then a per-row floor-divide of two "
                 "minima to days"),
    ("cm", 22): (3, "ROW,SET",
                 "as bo-Q20: the median is available, the per-pair delay it "
                 "would consume is not"),
    ("cm", 29): (3, "ROW,SET",
                 "sent and received counts joined per account, then a ratio "
                 "per row before the argmax"),
    ("cm", 34): (3, "ROW,SET",
                 "'more than double' compares two fields of the joined pair "
                 "row"),
    ("cm", 44): (3, "ROW,SEQ",
                 "first-reply latency is a lag, and the average is per "
                 "sender — a grouped mean over a derived column, not the "
                 "whole-input mean compute performs"),
    ("cm", 48): (3, "ROW,SET",
                 "join the per-src day counts to the per-dst first-message "
                 "minimum, then floor-divide that minimum per row"),
    ("cm", 49): (3, "ROW,SET",
                 "join the two accounts on recipient, then difference the "
                 "two first-message times per row"),
    ("cm", 52): (3, "PCT",
                 "ratio(x, y) now closes the readout; selecting the bottom "
                 "50% of senders by count is a percentile cut, and topk only "
                 "goes from the top"),
    ("cm", 54): (3, "PCT",
                 "percent(x, y) closes the readout; 'top 1% of accounts' is "
                 "a percentile cut whose k is a fraction of the group count"),
}


def parse_raw() -> dict[tuple[str, int], dict]:
    """Parse raw_questions.txt into {(dataset, n): {text, kind, why}}."""
    out: dict[tuple[str, int], dict] = {}
    ds = "cm"
    seen_blank = False
    for line in RAW.read_text().splitlines():
        line = line.strip()
        if not line:
            seen_blank = True
            continue
        if "|" not in line:
            if seen_blank:
                ds = "bo"  # section headers after the gap = bitcoin block
            continue
        qid, text, kind, why = (p.strip() for p in line.split("|", 3))
        n = int(qid.lstrip("Q"))
        out[(ds, n)] = {"text": text, "kind": kind, "why": why}
    return out


def utc(y, m, d):
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1_000_000)


# --------------------------------------------------------------------------- #
# gold: two independent keys per runnable question                             #
# --------------------------------------------------------------------------- #

def _events_sql(db):  # key 1: SQL over the canonical store (current belief)
    import duckdb
    conn = duckdb.connect(db, read_only=True)
    return conn


def _events_py(db):   # key 2: pure Python row list
    import duckdb
    conn = duckdb.connect(db, read_only=True)
    rows = conn.execute(
        "SELECT src, dst, vt_s FROM edge_versions "
        f"WHERE tt_e = {OPEN_END}").fetchall()
    conn.close()
    return rows


def gold_busiest_day(db):
    conn = _events_sql(db)
    day, cnt = conn.execute(
        f"SELECT vt_s // {MICROS_PER_DAY} AS d, count(*) c FROM edge_versions "
        f"WHERE tt_e = {OPEN_END} GROUP BY d ORDER BY c DESC, d LIMIT 1"
    ).fetchone()
    conn.close()
    py = Counter(v // MICROS_PER_DAY for _, _, v in _events_py(db))
    top = max(sorted(py), key=lambda d: py[d])
    assert top == day and py[top] == cnt, (top, day)
    return {"t_a": day * MICROS_PER_DAY, "t_b": (day + 1) * MICROS_PER_DAY}


def gold_senders_to(db, uid, t_a, t_b):
    conn = _events_sql(db)
    rows = conn.execute(
        "SELECT DISTINCT src FROM edge_versions WHERE dst = ? "
        f"AND vt_s >= ? AND vt_s < ? AND tt_e = {OPEN_END}",
        [uid, t_a, t_b]).fetchall()
    conn.close()
    py = sorted({s for s, d, v in _events_py(db)
                 if d == uid and t_a <= v < t_b})
    sql = sorted(r[0] for r in rows)
    assert sql == py, (sql, py)
    return sql


def gold_count_window(db, t_a, t_b):
    conn = _events_sql(db)
    (n,) = conn.execute(
        "SELECT count(*) FROM edge_versions WHERE vt_s >= ? AND vt_s < ? "
        f"AND tt_e = {OPEN_END}", [t_a, t_b]).fetchone()
    conn.close()
    n2 = sum(1 for _, _, v in _events_py(db) if t_a <= v < t_b)
    assert n == n2
    return n


def build():
    import os
    q = parse_raw()
    # env overrides let gold run on snapshot copies when the live store's
    # write lock is held by a running eval
    cm_db = os.environ.get("TGMS_CM_DB", "stores/collegemsg/store.duckdb")
    bo_db = os.environ.get("TGMS_BO_DB", "stores/bitcoinotc/store.duckdb")
    conn = _events_sql(cm_db)
    (cm_min,) = conn.execute(
        f"SELECT min(vt_s) FROM edge_versions WHERE tt_e = {OPEN_END}"
    ).fetchone()
    conn.close()

    tasks = {
        "cm": [
            dict(id="indep-cm-05", n=5, answer_kind="interval",
                 gold=gold_busiest_day(cm_db), input_uids=[]),
            dict(id="indep-cm-12", n=12, answer_kind="entity_set",
                 gold=gold_senders_to(cm_db, "n5",
                                      utc(2004, 6, 15), utc(2004, 6, 16)),
                 input_uids=["n5"]),
            dict(id="indep-cm-18", n=18, answer_kind="count",
                 gold=gold_count_window(cm_db, cm_min,
                                        cm_min + 7 * MICROS_PER_DAY),
                 input_uids=[]),
        ],
        "bo": [
            dict(id="indep-bo-03", n=3, answer_kind="count",
                 gold=gold_count_window(bo_db, 0, OPEN_END), input_uids=[]),
            dict(id="indep-bo-05", n=5, answer_kind="interval",
                 gold=gold_busiest_day(bo_db), input_uids=[]),
        ],
    }
    names = {"cm": ("collegemsg", "suite-indep-collegemsg"),
             "bo": ("bitcoinotc", "suite-indep-bitcoinotc")}
    for ds, (dataset, out) in names.items():
        suite = {"dataset": dataset, "seed": 0, "dev": [],
                 "test": [{**t, "dataset": dataset, "family": "independent",
                           "difficulty": "independent", "gold_source": "manual-double-keyed",
                           "question_text": q[(ds, t.pop("n"))]["text"]}
                          for t in tasks[ds]],
                 "n_dev": 0, "n_test": len(tasks[ds])}
        import hashlib
        suite["test_split_sha"] = hashlib.sha256(
            json.dumps(suite["test"], sort_keys=True).encode()).hexdigest()
        p = Path("stores") / out
        p.mkdir(parents=True, exist_ok=True)
        (p / "suite.json").write_text(json.dumps(suite, indent=1))
        print(out, suite["n_test"], "tasks", suite["test_split_sha"][:16])


def _needs(cls: int, tags: str) -> list[str]:
    """Need-tags of a class-3 entry; ops strings (`a+b`) are not needs."""
    if cls != 3:
        return []
    return [t for t in tags.split(",") if t and "+" not in t]


# --------------------------------------------------------------------------- #
# re-audit after D-052's property typing shipped (the D-053 session)          #
# --------------------------------------------------------------------------- #
# Chains onto C15. What shipped: `aggregate_events` gained `prop_filter` (a
# predicate on an edge property) and min/max/mean over `of: "prop"`, both
# under D-052's rule — a value participates only if its JSON type fits, text
# is never parsed into a number, and excluded rows are counted per property
# in `prop_coercion`.
#
# What did NOT ship, and the tag it leaves behind:
#   PROJ *new* — projecting a property *value* into output rows, so that two
#        rows' values can be compared with each other. The aggregates reduce
#        a property to one number per group and the predicate compares it to
#        a literal; neither hands the value back.
#   PROP survives only where the predicate is needed *inside another
#        operator* — the motif catalogue and the path operators, which this
#        work did not touch. That is a smaller and much more specific claim
#        than `PROP` carried before, and it is the same lesson as `AR`: the
#        tag named the first obstacle, not the set.
C16: dict[tuple[str, int], tuple[int, str, str]] = {
    # ---- became expressible ---- #
    ("bo", 4): (1, "aggregate_events",
                "one call: group_by [], prop_filter(rating eq 0), count"),
    ("bo", 6): (2, "aggregate_events+aggregate_events+percent",
                "positives via prop_filter(rating gt 0) count, all ratings "
                "via a second group_by [] count, then percent(x, y)"),
    ("bo", 8): (2, "aggregate_events+aggregate_events",
                "one group_by [] with mean of prop rating per year window — "
                "five literal windows, the distinction bo-Q2 already turned "
                "on ('a window, not a calendar bucket'), so no calendar "
                "bucketing is required"),
    ("bo", 12): (2, "aggregate_events+filter+topk",
                 "group_by [endpoint src] with count and mean of prop "
                 "rating, filter(count ge 10), topk(mean_prop_rating, 1)"),
    ("bo", 13): (2, "aggregate_events+aggregate_events",
                 "s1 group_by [] min of prop rating; s2 group_by [endpoint "
                 "src, endpoint dst] with prop_filter(rating eq $ref s1) — "
                 "the window-reselect idiom C14 found for cm-Q37, now "
                 "reselecting on a property instead of a time"),
    ("bo", 15): (2, "aggregate_events+filter+filter+count",
                 "'all ratings <= 0' is max_prop_rating le 0: group_by "
                 "[endpoint dst] with count and max of prop rating, "
                 "filter(count ge 3), filter(max_prop_rating le 0), count"),
    ("bo", 43): (2, "aggregate_events+filter+topk",
                 "as_of_tt fixes the belief state, then the bo-Q12 chain "
                 "over received ratings"),
    ("bo", 48): (2, "aggregate_events+filter+filter+count",
                 "mirror of bo-Q15 on the giving side: 'all > 0' is "
                 "min_prop_rating gt 0"),
    ("bo", 49): (2, "aggregate_events+filter+filter+count",
                 "mirror of bo-Q48 with max_prop_rating lt 0"),
    ("bo", 55): (2, "aggregate_events+filter+aggregate_events+filter+diff",
                 "mean of prop rating per dst in each of the two windows, "
                 "the account picked out by filter(dst eq n200) post-hoc, "
                 "and the sign of the diff is the answer"),
    # ---- the property need is met; the other tags are not ---- #
    ("bo", 14): (3, "ROW,SET",
                 "positive and negative counts are now two prop_filter "
                 "calls; joining them per account and differencing per row "
                 "is what is left"),
    ("bo", 25): (3, "SET",
                 "rating signs are a predicate now; the reciprocal-pair "
                 "join is not, and percent closes the readout"),
    ("bo", 26): (3, "SET", "as bo-Q25"),
    ("bo", 44): (3, "ROW,SET",
                 "positive-rating counts are a prop_filter call per "
                 "snapshot; the cross-snapshot join and per-row comparison "
                 "remain"),
    ("bo", 46): (3, "SET",
                 "per-dst min vt_s overall and among negatives are two "
                 "calls, and equality between them says the first rating "
                 "was negative — no property projection needed, but "
                 "matching the two results per account is a join"),
    ("bo", 47): (3, "ROW,SEQ",
                 "the means are expressible; the first-5/last-5 slices and "
                 "their per-account difference are not"),
    ("bo", 50): (3, "SET",
                 "the cohort test is mean of prop rating lt 0; restricting "
                 "a second grouping to that cohort needs a uid pre-filter "
                 "the operator does not have"),
    # ---- what the property tag was actually hiding ---- #
    ("bo", 27): (3, "PROJ,SEQ",
                 "'a different value' compares one rating with the previous "
                 "one; an aggregate reduces and a predicate compares to a "
                 "literal, so neither hands the value back for that"),
    ("bo", 29): (3, "PROJ,SET,SEQ",
                 "as bo-Q27, plus the transpose join and the ordering"),
    ("bo", 30): (3, "PROJ,SET",
                 "the correlation is between the two ratings of a pair, so "
                 "both values have to reach the same row"),
    ("bo", 11): (3, "PCT",
                 "count and mean of prop rating per dst are one call and "
                 "filter(count ge N) the cohort; the *lowest* mean is a "
                 "bottom-k selection, and topk only ranks from the top"),
    ("bo", 33): (3, "SET",
                 "positive ratings are a predicate now; 'at least 3 raters "
                 "who all also rated each other' is a mutual-clique "
                 "condition over a rater set"),
    ("bo", 36): (3, "SET",
                 "the positively-rated set of one account is reachable by "
                 "grouping plus a post-hoc filter; expanding a *set* by a "
                 "second hop needs a uid pre-filter, i.e. a join"),
}



# --------------------------------------------------------------------------- #
# re-audit after the SET capability shipped (D-054)                           #
# --------------------------------------------------------------------------- #
# Chains onto C16. What shipped: `compute` set operations over two uid lists
# (intersect/difference/union), an `endpoint_filter` cohort pre-filter on
# `aggregate_events`, and `pair_mode` (undirected / reciprocal) over an
# (src, dst) grouping.
# What did NOT ship, and the tag it leaves behind:
#   JOIN *new* — aligning two grouped results on their key so that fields
#        from both rows can be used together ("A's count minus B's count per
#        account"). The set operations answer *which uids*; they cannot
#        carry a value across from one result to the other. This is the
#        fourth tag to split on contact.
C17: dict[tuple[str, int], tuple[int, str, str]] = {
    # ---- became expressible ---- #
    ("bo", 7): (2, "aggregate_events+aggregate_events+intersect+count",
                "givers by [endpoint src], receivers by [endpoint dst], "
                "intersect on the uid column, count"),
    ("bo", 19): (2, "aggregate_events+intersect+intersect+count",
                 "one grouping per year window (literal windows, the bo-Q2 "
                 "distinction), then two intersections"),
    ("bo", 24): (1, "aggregate_events",
                 "group_by [endpoint src, endpoint dst] with "
                 "pair_mode reciprocal; rows_total is the answer"),
    ("bo", 25): (2, "aggregate_events+aggregate_events+percent",
                 "reciprocal pairs among positive ratings over reciprocal "
                 "pairs overall — filtering to positives first makes "
                 "'both rated the other positively' the reciprocity test"),
    ("bo", 28): (2, "aggregate_events+topk",
                 "pair_mode undirected sums both directions per pair, then "
                 "topk(count, 1)"),
    ("bo", 36): (2, "aggregate_events+aggregate_events+count",
                 "endpoint_filter src=[n100] with a positive prop_filter "
                 "gives X by [endpoint dst]; the same shape with "
                 "endpoint_filter src=$ref X gives the second hop"),
    ("bo", 50): (2, "aggregate_events+filter+filter+aggregate_events"
                    "+aggregate_events+percent",
                 "the cohort is filter(mean_prop_rating lt 0) and "
                 "filter(count ge 5); endpoint_filter src=$ref cohort then "
                 "restricts both counts the percentage is taken over"),
    ("bo", 53): (2, "aggregate_events+aggregate_events+difference+count",
                 "raters in each year window, then a set difference"),
    ("cm", 10): (2, "aggregate_events+aggregate_events+difference+count",
                 "recipients minus senders over the whole log"),
    ("cm", 28): (2, "aggregate_events+filter+aggregate_events+difference+count",
                 "count_distinct dst per src with filter(distinct_dst ge 5), "
                 "minus the set of accounts that received"),
    ("cm", 36): (2, "aggregate_events+aggregate_events+difference+count",
                 "endpoint_filter pins n770 on each side; recipients from "
                 "n770 minus senders to n770"),
    ("cm", 39): (2, "aggregate_events+topk",
                 "the cm twin of bo-Q28: pair_mode undirected, topk"),
    ("cm", 42): (2, "aggregate_events+aggregate_events+intersect",
                 "one endpoint_filter grouping per named sender, then "
                 "intersect the recipient sets"),
    ("cm", 45): (2, "aggregate_events+union+aggregate_events+union"
                    "+difference+count",
                 "active = union of the src and dst sets in a window; the "
                 "two windows then differ. January precedes the data extent, "
                 "so the chain runs and correctly returns none (cm-Q3)"),
    # ---- the set need is met; the other tags are not ---- #
    ("bo", 29): (3, "PROJ,SEQ", "reciprocity is an operator argument now"),
    ("bo", 30): (3, "PROJ", "the pair set is expressible; both ratings in "
                            "one row is not"),
    ("cm", 13): (3, "SEQ", "pair_mode reciprocal gives the pairs; 'within an "
                           "hour' is the ordered-sequence part"),
    ("cm", 14): (3, "SEQ", "endpoint_filter either unions the two roles; the "
                           "longest gap is sequence work"),
    ("cm", 30): (3, "CAL", "months are not fixed strides, and seven of them "
                           "plus six intersections exceeds the 12-step cap"),
    ("cm", 34): (3, "ROW", "reciprocal pairs are one call; 'more than double' "
                           "compares the two directions of a row"),
    ("cm", 51): (3, "SEQ,G", "reciprocity is available; 'new in May' is an "
                             "ordering, and the argmax is a regrouping"),
    # ---- what the set tag was hiding ---- #
    ("bo", 14): (3, "ROW,JOIN", "two prop-filtered groupings aligned per "
                                "account, then differenced"),
    ("bo", 20): (3, "ROW,JOIN", "the pair set is expressible; the per-pair "
                                "time difference needs both minima in a row"),
    ("bo", 22): (3, "ROW,JOIN", "two per-account minima, aligned then "
                                "floor-divided"),
    ("bo", 42): (3, "ROW,JOIN", "two as_of_tt groupings aligned per account"),
    ("bo", 44): (3, "ROW,JOIN", "as bo-Q42, with a comparison"),
    ("bo", 46): (3, "ROW,JOIN", "'first rating was negative' compares the "
                                "overall minimum with the negatives-only "
                                "minimum, per account"),
    ("bo", 54): (3, "PCT,ROW,JOIN", "given and received counts aligned per "
                                    "account"),
    ("cm", 21): (3, "ROW,JOIN", "as bo-Q22"),
    ("cm", 22): (3, "ROW,JOIN", "as bo-Q20"),
    ("cm", 29): (3, "ROW,JOIN", "sent and received counts aligned per "
                                "account, then a ratio"),
    ("cm", 48): (3, "ROW,JOIN", "per-src day counts aligned to per-dst "
                                "minima"),
    ("cm", 49): (3, "ROW,JOIN", "two accounts' per-recipient minima aligned "
                                "on the recipient"),
    ("bo", 51): (3, "ROW", "the two per-day sets are expressible; matching "
                           "them needs a derived (day, account) key"),
    ("cm", 16): (3, "ROW,JOIN", "two per-account minima against n42, "
                                "compared"),
    ("cm", 17): (3, "G", "reciprocal pairs are one call; counting distinct "
                         "partners per account regroups that result"),
    ("bo", 26): (3, "PROJ", "reciprocity is available; 'one positive and one "
                            "negative' needs both directions' values in a "
                            "row, and zero ratings make it not the "
                            "complement of the both-positive count"),
}


# --------------------------------------------------------------------------- #
# re-audit after ROW + JOIN shipped (D-055)                                    #
# --------------------------------------------------------------------------- #
# `compute` gained `derive` (one computed column from two fields or a field
# and a literal: add/sub/mul/div/floordiv/concat) and `join` (align two prior
# steps on a key unique on both sides; inner or left with a fill). The pair
# shipped together because JOIN was the sole blocker of nothing.
C18: dict[tuple[str, int], tuple[int, str, str]] = {
    ("bo", 14): (2, "aggregate_events+aggregate_events+join+derive+topk",
                 "positive and negative counts per account, joined left with "
                 "fill 0 so an account with no negatives scores zero, then "
                 "derive sub and topk"),
    ("bo", 20): (2, "aggregate_events+derive+derive+join+derive+median",
                 "pair minima keyed both ways with derive concat, self-joined "
                 "on the swapped key, then the per-pair difference and its "
                 "median"),
    ("bo", 22): (2, "aggregate_events+aggregate_events+join+derive+derive+count",
                 "the two per-account minima joined, each floor-divided to a "
                 "day, then compared"),
    ("bo", 23): (2, "aggregate_events+filter+derive+derive+mean",
                 "max_vt_s - min_vt_s per rater, floor-divided to days, then "
                 "the whole-input mean that already existed — not a grouped "
                 "mean, which is why this one needed only `derive`"),
    ("bo", 42): (2, "aggregate_events+aggregate_events+join+derive+filter+count",
                 "two as_of_tt groupings joined per account, differenced, "
                 "thresholded"),
    ("bo", 44): (2, "aggregate_events+aggregate_events+join+derive+filter",
                 "as bo-Q42, reading the sign of the difference"),
    ("bo", 46): (2, "aggregate_events+aggregate_events+join+derive+percent",
                 "the overall minimum and the negatives-only minimum joined "
                 "per account; equality says the first rating was negative"),
    ("bo", 51): (2, "aggregate_events+derive+aggregate_events+derive+intersect+count",
                 "a (day, account) key on each side with derive concat, then "
                 "the set intersection that shipped in D-054"),
    ("cm", 16): (2, "aggregate_events+aggregate_events+join+derive+filter+count",
                 "the two per-account minima against n42, joined and compared"),
    ("cm", 21): (2, "aggregate_events+aggregate_events+join+derive+derive+count",
                 "as bo-Q22"),
    ("cm", 22): (2, "aggregate_events+derive+derive+join+derive+median",
                 "as bo-Q20"),
    ("cm", 29): (2, "aggregate_events+aggregate_events+join+derive+topk",
                 "sent and received counts joined per account, then a ratio"),
    ("cm", 34): (2, "aggregate_events+derive+derive+join+derive+filter+count",
                 "the two directions of each pair brought into one row by a "
                 "swapped concat key, then 'more than double'"),
    ("cm", 48): (2, "aggregate_events+aggregate_events+join+derive+filter+count",
                 "per-src day counts joined to per-dst minima"),
    ("cm", 49): (2, "aggregate_events+aggregate_events+join+derive+min",
                 "the two accounts' per-recipient minima joined on the "
                 "recipient, then differenced"),
}

# --------------------------------------------------------------------------- #
# re-audit after the sequence aggregates shipped (D-056)                       #
# --------------------------------------------------------------------------- #
# `aggregate_events` gained three aggregates that walk a group's events in
# vt_s order: `max_gap`, `max_in_window` (a *sliding* window of a given span,
# not the fixed stride a time_bucket dimension gives) and `max_session_span`.
# `compute filter` gained `is_null`/`not_null`, without which a null cell
# made the whole column unreducible and `max_gap` was not reachable at all.
#
# **Four questions moved on the capability and a fifth moved on a re-reading.**
# They are marked apart below and the report counts them separately, because
# a coverage delta that quietly absorbs a correction to an earlier verdict is
# not measuring what it says it measures.
#
# What the re-audit found, none of which the session's own forecast had:
#   EGO  *new* — an account's events **in either role, in one group**.
#        `endpoint_filter` unions the two roles in the *population* (D-054)
#        but grouping is still by `src` or by `dst`, so "no messages sent or
#        received" cannot be asked of every account at once. cm-Q14 and
#        cm-Q24 were tagged `SEQ` and the gap was never their obstacle.
#   GMEAN *new* — a reducer taken **per group** over a prior step's rows.
#        `ROW` covered this from C15 ("or a mean taken per group") and only
#        the derived-column half shipped in D-055.
#   ROW  retired: after cm-Q44 takes `GMEAN` and bo-Q47 turns out to want a
#        per-group *slice* rather than arithmetic, no entry needs it.
#   JOIN retired: it named a capability that shipped in D-055, and bo-Q54
#        kept carrying both tags because D-055 re-audited no class-3 entry.
# `SEQ` survives on seven entries and is now four distinct shapes, none of
# them the three that shipped: a lag between events of *opposite directions*
# in a pair (cm-Q13, cm-Q44), a *distinct* count inside a sliding window
# (cm-Q31), a first-k/last-k slice per group (bo-Q47, and bo-Q27's
# consecutive-pair comparison), and a gap whose two ends must both be sends
# (cm-Q24).
#: Entries that changed class because the *earlier verdict* was wrong, not
#: because a capability shipped. Kept out of every capability's delta, in
#: this table and in any that follows it.
REREAD: set[tuple[str, int]] = {("cm", 41), ("cm", 40), ("bo", 11)}

C19: dict[tuple[str, int], tuple[int, str, str]] = {
    # ---- became expressible, on the capability ---- #
    ("bo", 17): (2, "aggregate_events+filter+max+ratio",
                 "max_gap per rater; the accounts that rated once have a "
                 "null gap and `filter not_null` drops them, which is why "
                 "that cmp shipped in the same session"),
    ("bo", 18): (2, "aggregate_events+filter+count",
                 "max_in_window with a 24h span per rated account, then "
                 "'more than 5' and 'was there any'"),
    ("cm", 9): (2, "aggregate_events+filter+count",
                "as bo-Q18, per sender and over 100; the busiest 24 hours "
                "in the store hold 179 messages from one account"),
    ("cm", 35): (2, "aggregate_events+max",
                 "max_session_span with a 60-minute gap over an undirected "
                 "pair grouping. The D-055 study read this one as a "
                 "traversal and forecast it would stay blocked; read again, "
                 "'a sequence of messages between the same two accounts' is "
                 "sessionization inside one group and nothing more"),
    # ---- became expressible on a re-reading, NOT on this session's work ---- #
    ("cm", 41): (2, "aggregate_events+diff+ratio+ratio",
                 "not a D-056 capability. Every contact is new in exactly "
                 "one week, so the mean of the weekly counts is the "
                 "contacts divided by the weeks — one endpoint_filter call "
                 "for count_distinct dst and min vt_s, then arithmetic. It "
                 "was expressible before this session and the `SEQ` tag was "
                 "simply wrong"),
    # ---- re-tagged, still class 3 ---- #
    ("cm", 14): (3, "EGO",
                 "the gap shipped; the grouping did not. 'No messages sent "
                 "or received' is one stream per account across both roles, "
                 "and asking it of every account at once is the missing "
                 "dimension, not the missing aggregate"),
    ("cm", 24): (3, "EGO,SEQ",
                 "as cm-Q14, and additionally the two ends of the gap must "
                 "both be *sends* while the events in between are of either "
                 "role — a role-aware gap, which max_gap is not. `SET` was "
                 "never the obstacle"),
    ("cm", 51): (3, "G",
                 "min vt_s per directed pair, self-joined on the swapped "
                 "key, gives who initiated and whether B replied; counting "
                 "those per initiator is a regrouping of a result"),
    ("bo", 47): (3, "SEQ",
                 "`ROW` is not the obstacle: with a first-5/last-5 slice "
                 "per account the two means are `mean of prop` inside the "
                 "operator and the difference is `derive`. The slice is the "
                 "whole of it"),
    ("cm", 44): (3, "SEQ,GMEAN",
                 "first-reply latency is a lag between events of opposite "
                 "directions, and the average is over each receiver's "
                 "senders — a reducer per group, which is the half of `ROW` "
                 "that did not ship"),
    ("bo", 54): (3, "PCT",
                 "stale tags, found by re-reading rather than by building: "
                 "`ROW` and `JOIN` both shipped in D-055 and the two "
                 "per-account counts join and divide today. Only the top "
                 "10% selection is still missing"),
}


# --------------------------------------------------------------------------- #
# re-audit after the calendar dimension shipped (D-057)                        #
# --------------------------------------------------------------------------- #
# `aggregate_events` gained a `calendar_unit` dimension with three cyclic
# units — hour_of_day, day_of_week, month_of_year — at a fixed offset from
# UTC. Absolute `date`/`month`/`year` were NOT built: they are the same ten
# lines and are wanted only by entries that stay blocked on `GLOB` or `PROP`.
#
# **Seven questions moved and only five are the capability.** The other two
# are `REREAD`: tags that outlived the capability that answered them, from
# **two earlier sessions** this time rather than one.
#   cm-Q40  D-054's `endpoint_filter` cohort plus `difference` answers
#           "only ever sent to accounts that never sent" outright.
#   bo-Q11  "the lowest average, among accounts with at least 10" has no
#           percentile in it: the threshold is a `filter` and "lowest" is
#           D-055's `derive mul -1` in front of `topk`, which ranks
#           descending only.
# That makes it three consecutive sessions in which the previous session's
# tags were still on the board. It is not a coincidence and D-057 records it
# as a process defect rather than a run of bad luck.
#
# `NEG` is the tag that nearly retired here. Both remaining entries want the
# absence *inside a motif* (bo-Q31, bo-Q35), which is `PROP`'s territory and
# exactly the shape `PROP` itself took after D-052; the two that wanted
# absence over a result set (cm-Q40, cm-Q53) are set differences and are
# expressible. It is left un-retired on purpose because two entries still
# carry it honestly.
#
# `CAL` survives on three entries and means something narrower now: a
# calendar predicate *inside another operator* (cm-Q19's motif, bo-Q32's
# path), or an ABSOLUTE calendar unit rather than a cyclic one (bo-Q41's
# date). Neither is what shipped.
C20: dict[tuple[str, int], tuple[int, str, str]] = {
    # ---- became expressible, on the capability ---- #
    ("cm", 15): (2, "aggregate_events+filter+filter+sum+sum+percent",
                 "hour_of_day grouping, the two half-open bounds as two "
                 "filters, then the share of the total. Measured on the "
                 "real store it is 5.66% at UTC and 23.20% at UTC-7, which "
                 "is why the offset is an argument and not a default"),
    ("cm", 20): (2, "aggregate_events+derive+topk",
                 "day_of_week grouping; `topk` ranks descending only, so "
                 "'lowest' is D-055's derive mul -1 in front of it"),
    ("cm", 30): (2, "aggregate_events+aggregate_events+filter+union+intersect",
                 "month_of_year x endpoint twice, because 'active' spans "
                 "both roles; a union per month makes the role-merged set "
                 "and the answer is the intersection across months. Note "
                 "this needs only set membership, which is why it is not "
                 "blocked by `EGO` the way cm-Q14 is"),
    ("cm", 32): (2, "aggregate_events+filter+filter+filter+count",
                 "hour_of_day and day_of_week as the two dimensions — the "
                 "case that made `unit` part of the dimension key"),
    ("cm", 53): (2, "aggregate_events+filter+union+filter+difference",
                 "against its own tag: 'only ever between 21:00 and 03:00' "
                 "is the difference between senders seen in the window and "
                 "senders seen outside it. `NEG` was never the blocker"),
    # ---- became expressible on a re-reading, NOT on this session's work ---- #
    ("cm", 40): (2, "aggregate_events+aggregate_events+difference",
                 "not a D-057 capability. D-054 shipped both halves: the "
                 "cohort pre-filter selects senders who sent to a sender, "
                 "and `difference` removes them from the senders"),
    ("bo", 11): (2, "aggregate_events+filter+derive+topk",
                 "not a D-057 capability, and never a percentile: 'at least "
                 "10 ratings' is a filter on count and 'lowest average' is "
                 "derive mul -1 then topk, both of which shipped in D-055"),
    # ---- re-tagged, still class 3 ---- #
    ("cm", 19): (3, "PAT,CAL",
                 "the calendar constraint was not the obstacle. The motif "
                 "catalogue has five shapes and every one is three edges; "
                 "there is no 2-edge delta-path at all, and 'the same day' "
                 "would have to be a predicate inside it"),
}


# --------------------------------------------------------------------------- #
# re-audit after the version log shipped (D-058)                               #
# --------------------------------------------------------------------------- #
# Two capabilities, forecast and scored separately: O15 `version_history` (the
# belief log as rows, with `belief` = current/superseded/all as of `as_of_tt`)
# and `compute filter`'s `field2`, which compares two columns of one row.
#
# **`GLOB` retires completely, and it is the campaign's cleanest split.** The
# tag covered three unrelated things and now names none of them:
#   - the version-log scan  -> shipped here (bo-Q40, bo-Q45)
#   - `src == dst`          -> shipped here (cm-Q25, cm-Q38)
#   - a longest time-respecting chain -> `CHAIN` *new*, and not built
# bo-Q41 keeps neither half: the version log answers "which corrections", and
# what is left is bucketing them by an ABSOLUTE calendar date (which D-057
# deliberately did not build) and regrouping a result, which is `G`.
#
# The forecast was 7 of 7, per question, pre-registered in the `[tests]`
# commit — the second session running, and the second to be right in every
# cell. Both capabilities delivered exactly what was predicted of them: two
# questions each.
C21: dict[tuple[str, int], tuple[int, str, str]] = {
    # ---- became expressible, on the version log ---- #
    ("bo", 40): (1, "version_history",
                 "one call: `belief: superseded` over the whole window is "
                 "the count of beliefs that were revised. Zero on a store "
                 "nobody corrected, and one per `correct` even when it "
                 "carves a version into three"),
    ("bo", 45): (2, "version_history+filter+count",
                 "the current beliefs, then `filter` comparing tt_s against "
                 "vt_s — the record time against the event time. Needs both "
                 "of this session's capabilities, which is why no "
                 "`record_lag` column was invented to avoid the second"),
    # ---- became expressible, on the two-field filter ---- #
    ("cm", 25): (2, "aggregate_events+filter+sum",
                 "group by (src, dst), keep the rows where the two columns "
                 "are equal, sum the counts. Note the page cap: CollegeMsg "
                 "has 20,296 directed pairs against a 10,000-row page, so "
                 "the plan is expressible and its execution on that dataset "
                 "reduces over a page — the systemic issue D-056 recorded, "
                 "biting a claimed question for the second time"),
    ("cm", 38): (2, "aggregate_events+filter+sum",
                 "the corpus asks cm-Q25 twice"),
    # ---- re-tagged, still class 3 ---- #
    ("bo", 34): (3, "PROP,CHAIN",
                 "the version log was never its obstacle. It wants the "
                 "longest time-respecting chain, over all starting points, "
                 "with a property predicate on every hop"),
    ("cm", 6): (3, "CHAIN",
                "the same longest chain without the predicate — a global "
                "optimum over all sources, which is neither a scan nor a "
                "shortest path"),
    ("bo", 41): (3, "CAL,G",
                 "`version_history` now says which corrections exist and "
                 "when they were recorded; what is left is bucketing tt_s "
                 "by an ABSOLUTE calendar date, which D-057 did not build, "
                 "and regrouping a result to count per bucket"),
}


# --------------------------------------------------------------------------- #
# re-audit after the percentile slice shipped (D-060)                          #
# --------------------------------------------------------------------------- #
# `compute topk` gained `pct` — k as a percentage of the row count, rounded up
# — and `side`, where `bottom` is the exact complement of the matching `top`.
#
# **`PCT` retires, and it is the first tag in the campaign to deliver its full
# sole-blocker count.** Three entries, three moves, forecast per question in
# the `[tests]` commit. Seven sessions of evidence say a tag names the first
# obstacle rather than the set; this is the exception, and one clean case does
# not overturn seven.
#
# The re-read that mattered was of the *capability*, not of the questions.
# bo-Q11 left `PCT` in C20 because `derive mul -1` in front of `topk` ranks
# ascending, so "lowest" needed nothing new. That trick does NOT extend to
# cm-Q52: the top half of `x` and the top half of `-x` are both ceil(n/2)
# rows, so an odd row count lands in both and the ratio is over a population
# that does not add up. Hence `side` as a complement rather than a second
# ranking — a decision (D-060), not an implementation detail.
#
# Re-read and NOT moved, because the word "slice" is in its need string:
# bo-Q47 wants the first 5 and last 5 ratings *per account*, by position in
# that account's own sequence. `pct` slices one flat result by rank on a
# field, which is a different operation and always was — `SEQ` is right and
# `PCT` was never its obstacle. Recorded here rather than as a table row
# because nothing about its verdict changes.
C22: dict[tuple[str, int], tuple[int, str, str]] = {
    # ---- became expressible, on the percentile slice ---- #
    ("bo", 54): (2, "aggregate_events+aggregate_events+join+topk+sum+sum+ratio",
                 "given and received counts per account join on the account "
                 "(D-055), `topk pct: 10` takes the most active tenth by "
                 "given, and the two sums close the ratio over that cohort"),
    ("cm", 52): (2, "aggregate_events+topk+sum+topk+sum+ratio",
                 "one ranking, both ends: `k: 10` for the top senders and "
                 "`pct: 50, side: bottom` for the other half. The two "
                 "populations partition, which is the whole reason the ratio "
                 "means anything. Note the window: the corpus asks for March "
                 "2004 and CollegeMsg starts April 15 — the same empty month "
                 "`C` already annotates on cm-Q3. Expressible here, and "
                 "empty on this dataset"),
    ("cm", 54): (2, "aggregate_events+topk+sum+sum+percent",
                 "`pct: 1` over per-sender counts, then `percent` against "
                 "the total. Run: 14 of 1,350 accounts, 8,952 of 59,835 "
                 "messages, 14.96%"),
    # ---- re-tagged, still class 3 ---- #
    ("bo", 52): (3, "G",
                 "re-read because rank selection is in its need string, and "
                 "`SET` turned out to be stale: 'joined back to that list' "
                 "is D-055's `join`, and the top 10 was already `topk`. What "
                 "is left is one thing — the first rater per rated account "
                 "is an argmin over a pair-level grouping that has to report "
                 "the group's label, which is regrouping a result"),
}


# --------------------------------------------------------------------------- #
# what each chain groups by, and the page it has to fit in (D-062)             #
# --------------------------------------------------------------------------- #
# D-061 fixed the correctness half of the page cap and left this half open: a
# verdict says which *operators* a chain uses and never which `group_by` they
# ran with, and the exposure is entirely a property of the grouping. Measured
# on both canonical stores, every single-dimension grouping fits a 10,000-row
# page and **every endpoint x second-dimension grouping overflows at least one
# of them**:
#
#     grouping                 collegemsg   bitcoinotc
#     (src,) (dst,) (bucket,)   <= 1,862     <= 5,858    fit
#     (src, rel_type)              1,350        4,814    fits DEGENERATELY -
#                                                        one rel_type in each
#     (src, day_of_week)           5,727       11,720    overflows bo
#     (src, hour_of_day)          10,965       17,272    overflows both
#     (src, time_bucket/day)      14,633       25,716    overflows both
#     (src, dst)                  20,296       35,592    overflows both
#
# So the rule is structural, not a list: pairing an endpoint with anything
# else multiplies by that account's activity, and account counts are within
# one order of magnitude of the page on both datasets. A list of exposed
# verdicts would go stale on the next dataset; this does not.
#
# `(src, rel_type)` is the exception that proves it — it fits only because
# both corpora carry exactly one rel_type, so it collapses to `(src,)`. It is
# flagged AT RISK anyway, because the structure is what ages well.
ENDPOINT_DIMS = frozenset({"src", "dst"})

#: Grouping vocabulary. A calendar dimension is named by its *unit* rather
#: than by `calendar_unit`, because two of them can appear in one grouping
#: (cm-Q32 groups hour_of_day x day_of_week) and the operator rejects two
#: dimensions that are literally identical. Naming the unit is also what lets
#: the measurement build the real `group_by` from the declaration.
GROUPING_DIMS = frozenset({"src", "dst", "rel_type", "time_bucket",
                           "hour_of_day", "day_of_week", "month_of_year"})

#: Per verdict, the grouping of each `aggregate_events` call in its chain.
#: `None` means the chain cannot be reconstructed from the op string alone —
#: which is the finding that motivated this table, not a gap in it. Those are
#: reported by `pagecap` as undecidable rather than silently treated as safe.
GROUPINGS: dict[tuple[str, int], tuple[tuple[str, ...], ...] | None] = {
    ("bo", 6): ((), ()),
    ("bo", 7): (("src",), ("dst",)),
    ("bo", 9): (("src",),),
    ("bo", 10): (("dst",),),
    ("bo", 11): (("dst",),),
    ("bo", 12): (("src",),),
    ("bo", 14): (("dst",), ("dst",)),
    ("bo", 15): (("dst",),),
    ("bo", 16): (("dst",),),
    ("bo", 17): (("src",),),
    ("bo", 18): (("src",),),
    ("bo", 19): (("src",), ("src",), ("src",)),   # three yearly cohorts,
                             # one call each: 4,814 rows. The recorded chain
                             # shows ONE aggregate_events and no filter, so
                             # it matches no executable plan (D-063)
    ("bo", 20): (("src", "dst"),),
    ("bo", 22): (("src",), ("dst",)),
    ("bo", 23): (("src",),),
    ("bo", 25): (("src", "dst"),),
    ("bo", 28): (("src", "dst"),),
    ("bo", 26): (("src", "dst"),),   # the mirror join: one grouping, joined
    ("bo", 27): (("src", "dst"),),   # to itself on the swapped key (D-065)
    ("bo", 29): (("src", "dst"),),
    ("bo", 30): (("src", "dst"),),
    ("bo", 36): (("src",), ("src",)),
    ("bo", 42): (("dst",), ("dst",)),
    ("bo", 43): (("dst",),),
    ("bo", 44): (("dst",), ("dst",)),
    ("bo", 46): (("dst",), ("dst",)),
    ("bo", 48): (("src",),),
    ("bo", 49): (("src",),),
    ("bo", 50): (("dst",), ("src",)),
    ("bo", 51): (("src", "time_bucket"), ("dst", "time_bucket")),
    ("bo", 53): (("src",), ("src",)),
    ("bo", 54): (("src",), ("dst",)),
    ("bo", 55): ((), ()),
    ("cm", 3): (("src",),),
    ("cm", 4): (("dst",),),
    ("cm", 7): (("src",),),
    ("cm", 9): (("src",),),
    ("cm", 10): (("dst",), ("src",)),
    ("cm", 11): (("src", "time_bucket"),),
    ("cm", 15): (("hour_of_day",),),
    ("cm", 16): (("src",), ("dst",)),
    ("cm", 17): (("src", "dst"),),   # reciprocal pairs, then grouped by
                             # account (D-067)
    ("cm", 20): (("day_of_week",),),
    ("cm", 21): (("src",), ("dst",)),
    ("cm", 22): (("src", "dst"),),
    ("cm", 23): (("src",),),
    ("cm", 25): (("src", "dst"),),
    ("cm", 27): ((), ()),
    ("cm", 28): (("src",), ("dst",)),
    ("cm", 29): (("src",), ("dst",)),
    ("cm", 30): (("src", "time_bucket"), ("dst", "time_bucket")),
                             # monthly buckets: 3,061 rows, fits. Six windows
                             # x two roles would be 12 calls = MAX_STEPS with
                             # nothing left for the union (D-063)
    ("cm", 32): (("hour_of_day", "day_of_week"),),  # it does allow it:
                             # hour_of_day x day_of_week = 168 rows (D-063)
    ("cm", 33): (("src",), ("src",)),   # count_distinct(of=dst) grouped by
                             # src is ONE dimension: 1,350 rows. Declaring
                             # (src,dst) here was the re-read being wrong
                             # about the operator, not about the question
    ("cm", 34): (("src", "dst"),),
    ("cm", 35): (("src", "dst"),),  # it is one: max_session_span with
                             # gap=1h per pair, 20,296 rows (D-063)
    ("cm", 36): (("dst",), ("src",)),
    ("cm", 38): (("src", "dst"),),
    # cm-39 is gone: three dimensions is not expressible, so it is class 3
    # now (C23) and no longer reduces a grouping at all
    ("cm", 40): (("src",), ("dst",)),
    ("cm", 41): (("dst", "time_bucket"),),  # endpoint_filter to the one
                             # account the question names: 0 rows. A 2-dim
                             # grouping scoped to one account is not big
    ("cm", 44): (("src", "dst"),),   # the mirror join, then grouped by
    ("cm", 51): (("src", "dst"),),   # receiver / initiator (D-067)
    ("cm", 42): (("dst", "time_bucket"),),  # MEASURED 1 row: the question
                             # names two senders, and endpoint_filter shrinks
                             # the grouping to nothing
    ("cm", 43): (("time_bucket",),),
    ("cm", 45): (("src",), ("src",)),
    ("cm", 47): (("src",),),
    ("cm", 48): (("src", "time_bucket"), ("dst", "time_bucket")),
    ("cm", 49): (("src",),),
    ("cm", 50): (("src", "dst"),),
    ("cm", 52): (("src",),),
    ("cm", 53): (("src", "hour_of_day"),),
    ("cm", 54): (("src",),),
}

#: Rows each declared grouping actually produces, per canonical store,
#: refreshed by `pagecap`. Recorded so `report()` can name the real exposure
#: while staying store-free, and so the structural guard and the measurement
#: can be seen to disagree rather than one quietly standing in for the other.
PAGE = 10_000
MEASURED: dict[tuple[str, ...], dict[str, int]] = {
    (): {"collegemsg": 1, "bitcoinotc": 1},
    ("day_of_week",): {"collegemsg": 7, "bitcoinotc": 7},
    ("dst",): {"collegemsg": 1_862, "bitcoinotc": 5_858},
    ("dst", "time_bucket"): {"collegemsg": 18_287, "bitcoinotc": 28_361},
    ("hour_of_day",): {"collegemsg": 24, "bitcoinotc": 24},
    ("hour_of_day", "day_of_week"): {"collegemsg": 168, "bitcoinotc": 168},
    ("src",): {"collegemsg": 1_350, "bitcoinotc": 4_814},
    ("src", "dst"): {"collegemsg": 20_296, "bitcoinotc": 35_592},
    ("src", "hour_of_day"): {"collegemsg": 10_965, "bitcoinotc": 17_272},
    ("src", "time_bucket"): {"collegemsg": 14_633, "bitcoinotc": 25_716},
    ("time_bucket",): {"collegemsg": 192, "bitcoinotc": 1_770},
}

#: Verdicts whose question names particular accounts, so `endpoint_filter`
#: bounds the grouping long before its unscoped size matters. Measured, not
#: assumed: cm-Q42 is 1 row and cm-Q41 is 0, against the 18,287 their
#: grouping produces unscoped. The structural rule cannot see a filter, which
#: is the price of a guard that survives a new dataset.
SCOPED_BY_FILTER: dict[tuple[str, int], int] = {
    ("cm", 41): 0,
    ("cm", 42): 1,
}

#: Measured with an argument that shrinks the population below the page even
#: unscoped. `pair_mode: reciprocal` keeps only pairs that answered each
#: other — 6,458 of CollegeMsg's 20,296 directed pairs.
NARROWED_BY_ARG: dict[tuple[str, int], int] = {
    ("cm", 17): 6_458,
    ("cm", 34): 6_458,
    ("bo", 25): 14_100,
}


#: A question belongs to one dataset, so its exposure is that dataset's.
STORE_OF = {"cm": "collegemsg", "bo": "bitcoinotc"}


def measured_max(k, grouping) -> tuple[int, str]:
    """Rows this verdict really produces on ITS OWN dataset, and where the
    number came from. Reading the other corpus's count here would inflate a
    CollegeMsg question with Bitcoin-OTC's pair count."""
    if k in SCOPED_BY_FILTER:
        return SCOPED_BY_FILTER[k], "scoped by endpoint_filter"
    if k in NARROWED_BY_ARG:
        return NARROWED_BY_ARG[k], "narrowed by pair_mode"
    store = STORE_OF[k[0]]
    return max((MEASURED[g][store] for g in grouping if g in MEASURED),
               default=-1), f"unscoped, on {store}"

#: `compute` functions that reduce to one number. Mirrors the executor's
#: REDUCING_FNS (D-061) — a chain ending in one of these over a page is a
#: wrong number, which is what makes the grouping's cardinality matter.
REDUCING_OPS = frozenset({"count", "sum", "mean", "median", "min", "max",
                          "percent", "ratio", "diff", "topk",
                          "intersect", "difference", "union", "join"})


def check_groupings() -> list[str]:
    """Every declared grouping must be one the operator would actually accept.

    Store-free on purpose, and that is the point: both defects this guard
    exists for were **validation** failures, not cardinality ones. D-063
    declared cm-Q32 as `calendar_unit x calendar_unit`, which the operator
    rejects as a duplicate dimension, and nothing noticed until `pagecap` was
    run by hand a session later. Asking the real validator costs nothing and
    needs no data, so it runs wherever the tables do.

    Returns a list of problems rather than raising, so a caller can report
    all of them at once.
    """
    from tgms.temporal.algebra import ensure_all_registered, validate_args
    ensure_all_registered()
    DIM = {"src": {"dim": "endpoint", "role": "src"},
           "dst": {"dim": "endpoint", "role": "dst"},
           "rel_type": {"dim": "rel_type"},
           "time_bucket": {"dim": "time_bucket"},
           "hour_of_day": {"dim": "calendar_unit", "unit": "hour_of_day"},
           "day_of_week": {"dim": "calendar_unit", "unit": "day_of_week"},
           "month_of_year": {"dim": "calendar_unit", "unit": "month_of_year"}}
    problems = []
    for k, groupings in sorted(GROUPINGS.items()):
        if groupings is None:
            continue
        for g in groupings:
            unknown = set(g) - GROUPING_DIMS
            if unknown:
                problems.append(f"{k[0]}-Q{k[1]}: unknown dimension(s) "
                                f"{sorted(unknown)} in {g}")
                continue
            args = {"group_by": [DIM[d] for d in g],
                    "aggregates": [{"agg": "count"}],
                    "window": {"t_a": 0, "t_b": 1}}
            if "time_bucket" in g:
                args["stride"] = 86_400_000_000
            try:
                validate_args("aggregate_events", args)
            except Exception as e:      # noqa: BLE001 - report, do not raise
                problems.append(f"{k[0]}-Q{k[1]}: {g} is not a grouping the "
                                f"operator accepts — {str(e)[:80]}")
    return problems

def reduces_after_grouping(ops: str) -> bool:
    """Does this chain reduce something a grouping produced?"""
    if not isinstance(ops, str) or "aggregate_events" not in ops:
        return False
    steps = ops.split("+")
    after = steps[steps.index("aggregate_events") + 1:]
    return any(s in REDUCING_OPS for s in after)


def at_risk(grouping: tuple[str, ...]) -> bool:
    """An endpoint paired with any second dimension. See the table above."""
    return len(grouping) >= 2 and bool(ENDPOINT_DIMS & set(grouping))


# --------------------------------------------------------------------------- #
# re-audit by RUNNING the chains, not reading them (D-063)                      #
# --------------------------------------------------------------------------- #
# D-062 recorded each chain's grouping by re-reading the questions. This table
# is what happened when the chains were executed against both canonical stores
# instead. Six of the sixty-five could not be reconstructed from their op
# string at all, and running them found one verdict that is simply wrong.
#
# **cm-Q39 is not expressible, and was published as class 2 for nine
# sessions.** "Which pair exchanged the most messages in a single day, and
# what day" needs (src, dst, time_bucket). Every route is closed, and each was
# closed by running it: three dimensions is a SchemaError because `group_by`
# caps at two; `pair_mode: undirected` refuses to free the slot because it
# "requires group_by = [endpoint src, endpoint dst]"; and one call per day is
# 194 calls against MAX_STEPS = 12. Its recorded chain, `aggregate_events+
# topk`, cannot be executed.
#
# Coverage therefore goes 86 -> 85. It is the first correction in this
# campaign that *lowers* the number, and it was only reachable by execution:
# nine re-audits read this entry and none of them tried it.
C23: dict[tuple[str, int], tuple[int, str, str]] = {
    ("cm", 39): (3, "DIM3",
                 "not expressible, found by running it: pair AND day is three "
                 "grouping dimensions, `group_by` takes two, `pair_mode` "
                 "cannot free the slot because it requires exactly "
                 "[src, dst], and per-day windows are 194 calls against a "
                 "12-step plan budget. Published class 2 since C; no re-audit "
                 "before this one executed it"),
}

# --------------------------------------------------------------------------- #
# re-audit before building PROJ, which turned out not to need building (D-065) #
# --------------------------------------------------------------------------- #
# The session opened to build `PROJ` — "projecting a property value into output
# rows so two rows' values can be compared" — and ran the four blocked
# questions first, per §6. All four already run.
#
# The capability arrived in pieces and nobody re-read the tag: D-052 put a
# property value in the output row as a column (`min of prop`), D-055 gave
# `derive concat` for a join key and `join` for the mirror, and D-058 gave
# `filter` a `field2` so two columns of one row can be compared. Composed,
# they are exactly what `PROJ` names. Each session checked the questions its
# own capability was aimed at; none re-read this one.
#
# Run against stores/bitcoinotc, not argued:
#   bo-Q26  58 reciprocal pairs with one positive and one negative rating
#   bo-Q27  0 — and legitimately: the corpus has ONE rating per directed pair
#           (35,592 pairs for 35,592 edges), so no account ever re-rated. The
#           chain is min(prop) != max(prop) on a pair rated twice, which needs
#           no lag: all ratings equal <=> min == max
#   bo-Q29  246 pairs that received first and gave a lower rating back
#   bo-Q30  1,993 both-positive and 23 both-negative, of 2,074 A-rated-B-first
#
# **PROJ retires, and SEQ loses two entries with it.** bo-Q27 and bo-Q29 were
# tagged `PROJ,SEQ`; neither needs a sequence, only a comparison between two
# values that now reach one row. Eighth instance of §2(a2) — a tag naming the
# first obstacle rather than the set — and the largest re-read gain in the
# campaign: four questions, no code.
#
# Every one of these chains groups by (src, dst), which is 35,592 rows on
# Bitcoin-OTC. All four join the needs-paging set the moment they become
# class 2 (D-061/D-064).
C24: dict[tuple[str, int], tuple[int, str, str]] = {
    ("bo", 26): (2, "aggregate_events+derive+derive+join+filter+filter+count",
                 "the mirror join puts both directions' ratings in one row and "
                 "`field2` compares them; PROJ was answered by D-052 + D-055 + "
                 "D-058 and nobody re-read it. Runs: 58"),
    ("bo", 27): (2, "aggregate_events+filter+filter+count",
                 "'rated again with a different value' is min(prop) != "
                 "max(prop) on a pair rated twice — all ratings equal iff min "
                 "== max, so no lag and no SEQ. Runs: 0, because the corpus "
                 "holds one rating per directed pair"),
    ("bo", 29): (2, "aggregate_events+derive+derive+join+filter+filter+count",
                 "as bo-Q30 with two field-to-field comparisons instead of "
                 "one: received first, and gave lower back. Runs: 246. `SEQ` "
                 "was never its obstacle"),
    # ---- re-tagged, still class 3 ---- #
    ("cm", 44): (3, "GMEAN",
                 "`SEQ` was not its obstacle either. First-reply latency is "
                 "the mirror join again — min(vt_s) each direction, keep the "
                 "pairs that received first, subtract — and it runs: 1,739 "
                 "pairs, mean 82.0 hours over all of them. What is missing is "
                 "the mean PER RECEIVER, which is `GMEAN`: `compute mean` "
                 "reduces a row set, never its groups"),
    ("bo", 30): (2, "aggregate_events+derive+derive+join+filter+filter+count",
                 "min(vt_s) per direction orders the pair and min(prop) per "
                 "direction gives the two signs, both compared with `field2`. "
                 "Runs: 1,993 both-positive, 23 both-negative"),
}

# --------------------------------------------------------------------------- #
# re-audit after compute gained group_by (D-067)                               #
# --------------------------------------------------------------------------- #
# `compute` reduces per group now, so a *result* can be grouped and not only
# the store. That capability was named by three different tags — `G`
# ("regrouping a result"), `GMEAN` ("a reducer per group") and one `SEQ` entry
# — and stayed invisible because every session re-read the questions its own
# capability aimed at, and none asked which blocked questions wanted the same
# thing. §2(a2) inverted: three tags, one capability.
#
# The forecast was 3 of 5 and it landed. Run against the canonical stores:
#   cm-Q17  n32, 107 distinct partners, over 6,458 reciprocal pairs
#   cm-Q44  n255, 2,096.2 hours average first-reply latency, over 517 receivers
#   cm-Q51  n42, 49 initiations in May 2004, over 500 initiators
#
# **GMEAN retires** — it named exactly this and nothing else. `G` keeps
# bo-Q52, which wants the `src` that achieved a per-group minimum: an argmin
# returning a sibling column, which is the *slice* half of this capability
# and is deliberately deferred. bo-Q47 wants the same slice by time.
C25: dict[tuple[str, int], tuple[int, str, str]] = {
    ("cm", 17): (2, "aggregate_events+compute_group+topk",
                 "reciprocal pairs are one call and each row is already a "
                 "distinct pair, so partners per account is a grouped count "
                 "over that result. Runs: n32 with 107"),
    ("cm", 44): (2, "aggregate_events+derive+derive+join+filter+derive+"
                    "compute_group+topk",
                 "the latency rows ran in D-065; the mean PER RECEIVER is "
                 "what was missing and it is `GMEAN` exactly. Runs: n255 at "
                 "2,096.2 hours"),
    ("cm", 51): (2, "aggregate_events+derive+derive+join+filter+"
                    "compute_group+topk",
                 "the mirror join says who initiated; counting those per "
                 "initiator is the regrouping. Runs: n42 with 49 in May 2004"),
    # ---- re-tagged, still class 3 ---- #
    ("bo", 52): (3, "GSLICE",
                 "the reduction half does not reach it: it wants the `src` "
                 "that achieved a per-group minimum, not the minimum. That "
                 "is an argmin returning a sibling column — the slice half of "
                 "D-067, deferred, and the same thing bo-Q47 wants by time"),
    ("bo", 47): (3, "GSLICE",
                 "first 5 and last 5 per account, ordered by time: the slice "
                 "half. `SEQ` was never its obstacle — with the slice the two "
                 "means are grouped `mean` and the difference is `derive`"),
}

def _verdict(table: dict, base, k):
    """A re-audit table's verdict for `k`, falling back to the table it
    chains onto. `base` is a callable so the chain composes."""
    return (table[k][0], table[k][1]) if k in table else base(k)


#: Re-audit tables that inspected no still-blocked entry. A table with no
#: class-3 row cannot discover that a tag has outlived the capability that
#: answered it — it can only record what its own session freed. `C18` is
#: here because it did exactly that, and the two sessions after it each had
#: to clean up tags C18 left standing (`ROW`, `JOIN` in C19; `PCT` on
#: bo-Q11, `SET`/`NEG` on cm-Q40 in C20). The set is closed: a new table
#: that audits nothing fails this check rather than joining it.
NO_CLASS3_AUDIT = frozenset({"C18"})


#: Entries whose earlier verdict was **wrong**, not superseded by a shipped
#: capability. `_check_diff` lets only these move backwards.
#:
#: Until D-063 there was no such set, and that is a finding about the
#: instrument rather than a gap in it: nine re-audits could record a
#: capability arriving and could not record a verdict being mistaken, so the
#: coverage number could only ever rise. An instrument whose entire job is
#: honesty about what the system cannot do should be able to say "we were
#: wrong about this one", and this one could not until it had to.
CORRECTED = frozenset({("cm", 39)})


def _check_diff(table: dict, base, name: str) -> None:
    """A re-audit is a diff, not a rewrite: guard the invariants that make it
    one. `base` returns the (class, tags) each entry is a diff against."""
    assert any(v[0] == 3 for v in table.values()) or name in NO_CLASS3_AUDIT, (
        f"{name} re-audited no class-3 entry. Re-read the need string of "
        f"every blocked question this session's capability could touch; a "
        f"re-audit that only records what it freed has not looked.")
    for k, (cls_new, tags_new, why) in table.items():
        assert k in C, f"{name} key {k} not in the pre-registered table"
        cls, tags = base(k)
        assert cls not in (4, 5), (
            f"{name} must not touch class-{cls} {k}: a new capability cannot "
            f"repair an ambiguous or non-computational question")
        assert (cls_new, tags_new) != (cls, tags), f"{name} {k} is not a change"
        assert cls_new <= cls or k in CORRECTED, (
            f"{name} {k} moved backwards: {cls} -> {cls_new}. A re-audit "
            f"records a capability arriving; a verdict that was wrong goes "
            f"in CORRECTED, with the run that proved it.")
        assert why, f"{name} {k} has no justification"


def report():
    q = parse_raw()
    assert len(q) == 110, len(q)

    def v13(k):
        return C[k][0], C[k][1]

    def v14(k):
        return _verdict(C14, v13, k)

    def v15(k):
        return _verdict(C15, v14, k)

    def v16(k):
        return _verdict(C16, v15, k)

    def v17(k):
        return _verdict(C17, v16, k)

    def v18(k):
        return _verdict(C18, v17, k)

    def v19(k):
        return _verdict(C19, v18, k)

    def v20(k):
        return _verdict(C20, v19, k)

    def v21(k):
        return _verdict(C21, v20, k)

    def v22(k):
        return _verdict(C22, v21, k)

    def v23(k):
        return _verdict(C23, v22, k)

    def v24(k):
        return _verdict(C24, v23, k)

    def v25(k):
        return _verdict(C25, v24, k)

    _check_diff(C14, v13, "C14")
    _check_diff(C15, v14, "C15")
    _check_diff(C16, v15, "C16")
    _check_diff(C17, v16, "C17")
    _check_diff(C18, v17, "C18")
    _check_diff(C19, v18, "C19")
    _check_diff(C20, v19, "C20")
    _check_diff(C21, v20, "C21")
    _check_diff(C22, v21, "C22")
    _check_diff(C23, v22, "C23")
    _check_diff(C24, v23, "C24")
    _check_diff(C25, v24, "C25")
    # AR is retired by C15: the capability shipped, and what is left of it
    # was never AR. Guard it so a later edit cannot quietly reintroduce the
    # tag without deciding what it now means.
    assert not [k for k in q if "AR" in _needs(*v15(k))], \
        "AR survived the C15 re-audit; it should have split into ROW and PCT"
    # ROW and JOIN are retired by C19 on the same terms. JOIN's capability
    # shipped in D-055 and the tag simply outlived it; ROW's shipped in half,
    # and the surviving half is GMEAN. A tag that outlives its capability
    # inflates the board — `ROW` read as blocking 3 questions for a session
    # after `derive` landed — so guard both.
    for tag, split in (("ROW", "GMEAN"), ("JOIN", "nothing"),
                       ("GLOB", "the version log, `src == dst`, and CHAIN"),
                       ("PCT", "nothing — it delivered all three"),
                       ("PROJ", "nothing — D-052, D-055 and D-058 had already "
                                "built it between them"),
                       ("GMEAN", "nothing — compute group_by is it")):
        assert not [k for k in q if tag in _needs(*v25(k))], \
            f"{tag} survived the re-audit; what is left of it is {split}"

    stages = [("13 ops, pre-registered", v13),
              ("14 ops, D-044 re-audit", v14),
              ("15th capability, D-051 session re-audit", v15),
              ("property typing, D-052 session re-audit", v16),
              ("sets, D-054 session re-audit", v17),
              ("row + join, D-055 session re-audit", v18),
              ("sequences, D-056 session re-audit", v19),
              ("calendar units, D-057 session re-audit", v20),
              ("the version log, D-058 session re-audit", v21),
              ("the percentile slice, D-060 session re-audit", v22),
              ("running the chains, D-063 session re-audit", v23),
              ("PROJ re-read, D-065 session re-audit", v24),
              ("grouping a result, D-067 session re-audit", v25)]
    for label, v in stages:
        print(f"class distribution ({label}):",
              dict(sorted(Counter(v(k)[0] for k in q).items())))
    for label, v in stages:
        print(f"missing capabilities (class 3, {label}):",
              dict(Counter(t for k in q for t in _needs(*v(k))).most_common()))

    for label, prev, cur, table in [
            ("D-044 aggregate_events", v13, v14, C14),
            ("D-051 compute arithmetic", v14, v15, C15),
            ("D-052 property typing", v15, v16, C16),
            ("D-054 sets", v16, v17, C17),
            ("D-055 row + join", v17, v18, C18),
            ("D-056 sequences", v18, v19, C19),
            ("D-057 calendar units", v19, v20, C20),
            ("D-058 the version log", v20, v21, C21),
            ("D-060 the percentile slice", v21, v22, C22),
            ("D-063 running the chains", v22, v23, C23),
            ("D-065 PROJ, which did not need building", v23, v24, C24),
            ("D-067 compute group_by", v24, v25, C25)]:
        moved = sorted(k for k in q if cur(k)[0] != prev(k)[0])
        by_move = Counter((prev(k)[0], cur(k)[0]) for k in moved)
        print(f"\nbecame expressible under {label}: {len(moved)} of {len(q)} "
              f"({', '.join(f'{a}->{b}: {n}' for (a, b), n in sorted(by_move.items()))})")
        for d, n in moved:
            mark = "  (re-read)" if (d, n) in REREAD else ""
            print(f"  {d}-Q{n:<2} {prev((d, n))[0]} -> {cur((d, n))[0]}  "
                  f"{cur((d, n))[1]}{mark}")
        reread = [k for k in moved if k in REREAD]
        if reread:
            # a delta that absorbs a correction to an earlier verdict is not
            # measuring the capability it claims to measure
            print(f"  of which {len(reread)} moved on a re-reading and not on "
                  f"this session's work: "
                  f"{', '.join(f'{d}-Q{n}' for d, n in reread)} — "
                  f"delta attributable to the capability: "
                  f"{len(moved) - len(reread)}")
        print(f"class-3 entries whose need-tags were re-audited: "
              f"{len([k for k in table if table[k][0] == 3])}")

    # D-062: every chain that reduces a grouping must say what it grouped by.
    # Without it the page-cap exposure is unknowable from the tables, and a
    # capability session could add one silently.
    needs = {k for k in q if v25(k)[0] in (1, 2)
             and reduces_after_grouping(v25(k)[1])}
    missing = sorted(needs - set(GROUPINGS))
    assert not missing, (
        f"{len(missing)} verdict(s) reduce a grouping and declare no "
        f"group_by: {['%s-Q%d' % m for m in missing]}. Add them to "
        f"GROUPINGS — the page-cap exposure cannot be read off the op chain.")
    stale = sorted(set(GROUPINGS) - needs)
    assert not stale, f"GROUPINGS has entries that no longer reduce: {stale}"
    risky = {k: g for k, g in GROUPINGS.items()
             if g is not None and any(at_risk(x) for x in g)}
    undecidable = sorted(k for k, g in GROUPINGS.items() if g is None)
    print(f"\npage cap (D-062): {len(needs)} verdicts reduce a grouping; "
          f"{len(risky)} group an endpoint with a second dimension and so "
          f"need paging on at least one canonical dataset; "
          f"{len(undecidable)} cannot be decided from the op chain")
    bad = check_groupings()
    assert not bad, ("declared groupings the operator would refuse:\n  "
                     + "\n  ".join(bad))
    unmeasured = sorted({g for gs in GROUPINGS.values() if gs for g in gs}
                        - set(MEASURED))
    assert not unmeasured, (
        f"declared groupings with no recorded measurement: {unmeasured}. "
        f"Run `pagecap` and record them — the structural rule is the guard, "
        f"but it must be able to be contradicted.")
    over = []
    for k in sorted(risky):
        n, how = measured_max(k, risky[k])
        verdict = "NEEDS PAGING" if n > PAGE else f"fits today ({how})"
        if n > PAGE:
            over.append(k)
        print(f"  at risk  {k[0]}-Q{k[1]:<3} {str(risky[k]):44s} "
              f"measured {n:>6,d}  {verdict}")
    print(f"  -> {len(risky)} at risk by shape, {len(over)} of them measured "
          f"over the {PAGE:,}-row page on a canonical store")
    for k in undecidable:
        print(f"  UNDECIDABLE  {k[0]}-Q{k[1]:<3} chain does not say")

    expressible = sum(1 for k in q if v25(k)[0] in (1, 2))
    print(f"\nexpressible now: {expressible} of {len(q)}")
    print("runnable:", [f"{d}-Q{n}" for (d, n) in sorted(C)
                        if C[(d, n)][2]])
    rows = [{"dataset": d, "q": n, **q[(d, n)],
             "class": C[(d, n)][0], "need_or_ops": C[(d, n)][1],
             "run": C[(d, n)][2], "note": C[(d, n)][3],
             "class_14": v14((d, n))[0], "need_or_ops_14": v14((d, n))[1],
             "justification_14": C14[(d, n)][2] if (d, n) in C14 else "",
             "class_15": v15((d, n))[0], "need_or_ops_15": v15((d, n))[1],
             "justification_15": C15[(d, n)][2] if (d, n) in C15 else "",
             "class_16": v16((d, n))[0], "need_or_ops_16": v16((d, n))[1],
             "justification_16": C16[(d, n)][2] if (d, n) in C16 else "",
             "class_17": v17((d, n))[0], "need_or_ops_17": v17((d, n))[1],
             "justification_17": C17[(d, n)][2] if (d, n) in C17 else "",
             "class_18": v18((d, n))[0], "need_or_ops_18": v18((d, n))[1],
             "justification_18": C18[(d, n)][2] if (d, n) in C18 else "",
             "class_19": v19((d, n))[0], "need_or_ops_19": v19((d, n))[1],
             "justification_19": C19[(d, n)][2] if (d, n) in C19 else "",
             "class_20": v20((d, n))[0], "need_or_ops_20": v20((d, n))[1],
             "justification_20": C20[(d, n)][2] if (d, n) in C20 else "",
             "class_21": v21((d, n))[0], "need_or_ops_21": v21((d, n))[1],
             "justification_21": C21[(d, n)][2] if (d, n) in C21 else "",
             "class_22": v22((d, n))[0], "need_or_ops_22": v22((d, n))[1],
             "justification_22": C22[(d, n)][2] if (d, n) in C22 else "",
             "reread_not_capability": (d, n) in REREAD}
            for (d, n) in sorted(q)]
    out = Path("benchmarks/independent-v1/classification.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))
    print("wrote", out)


def pagecap():
    """Measure every declared grouping on both canonical stores.

    The structural rule (`at_risk`) is the guard because it survives a new
    dataset; this is the evidence under it, and the one place the two can
    disagree — `(src, rel_type)` is at risk structurally and fits today,
    because both corpora carry exactly one rel_type.
    """
    import tgms
    from tgms.temporal.algebra import call_operator, ensure_all_registered
    ensure_all_registered()

    DIM = {"src": {"dim": "endpoint", "role": "src"},
           "dst": {"dim": "endpoint", "role": "dst"},
           "rel_type": {"dim": "rel_type"},
           "time_bucket": {"dim": "time_bucket"},
           "hour_of_day": {"dim": "calendar_unit", "unit": "hour_of_day"},
           "day_of_week": {"dim": "calendar_unit", "unit": "day_of_week"}}
    declared = sorted({g for gs in GROUPINGS.values() if gs for g in gs})
    stores = [n for n in ("collegemsg", "bitcoinotc")
              if Path(f"stores/{n}").exists()]
    if not stores:
        print("no canonical store present; nothing to measure")
        return
    print(f"{'grouping':30s}" + "".join(f"{n:>13s}" for n in stores)
          + "   structural")
    for g in declared:
        counts = []
        for name in stores:
            st = tgms.open(f"stores/{name}", read_only=True)
            ext = st.stats()
            args = {"group_by": [DIM[d] for d in g],
                    "aggregates": [{"agg": "count"}],
                    "window": {"t_a": ext["vt_min"], "t_b": ext["vt_max"] + 1},
                    "limit": 10_000}
            if "time_bucket" in g:
                args["stride"] = 86_400_000_000
            r = call_operator(st.adapter, "aggregate_events", args)
            counts.append((r["rows_total"], r["truncated"]))
            st.close()
        cells = "".join(f"{n:>12,d}{'*' if t else ' '}" for n, t in counts)
        print(f"{str(g):30s}{cells}   "
              f"{'AT RISK' if at_risk(g) else 'fits by shape'}")
    print("* = overflows the 10,000-row page on that dataset")


if __name__ == "__main__":
    {"report": report, "build": build,
     "pagecap": pagecap}[sys.argv[1] if len(sys.argv) > 1 else "report"]()
