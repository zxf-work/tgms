#!/usr/bin/env python3
"""LDBC SNB read-workload fit against the 14-operator algebra (D-050).

Forty-one third-party-defined read query templates — Interactive Short
IS1-IS7, Interactive Complex IC1-IC14 (v1; the only auditable version), and
Business Intelligence BI1-BI20 — classified against TGMS's closed operator
set with the instrument `scripts/independent_questions.py` used on the
110-question study (D-026, re-audited in D-044).

Classes are that study's, verbatim:
  1 directly expressible (single operator + answer readout)
  2 expressible by operator composition
  3 requires an unimplemented capability (see `need` tags)
Classes 4 (ambiguous) and 5 (not a computation over the log) cannot arise:
every LDBC template is a well-posed computation with a published reference
implementation and an expected result.

`need` tags are the study's vocabulary, plus two this workload needs and the
CollegeMsg/Bitcoin-OTC questions never did:
  G    grouped/distinct aggregation beyond the operator's two dimensions, or
       an aggregate over a JSON property (deferred in D-044)
  AR   arithmetic beyond count/sum/min/max/topk (ratio, average, weighted
       score, difference of two aggregates)
  PROP property predicate or projection beyond uid, label, rel_type and the
       interval columns
  CAL  calendar semantics (year, month, calendar-date equality)
  SET  set operations, or a join between two result sets
  NEG  absence conditions
  GLOB global scan-select with no anchor
  SEQ  per-group ordered-sequence work
  PAT  *new* — a labelled multi-way structural pattern, beyond the fixed
       five-shape motif catalogue and the untyped k-hop expansion of
       `snapshot_subgraph` (`docs/eval_semantics.md` §6: "Fixed motif
       catalogue. Five shapes, not arbitrary pattern matching.")
  SP   *new* — shortest-path length, all-shortest-paths, or a weighted
       cheapest path. `temporal_paths` caps at six hops and ranks by
       (arrival, hops, edge sequence); `temporal_reachability` returns
       earliest arrival, not distance. Neither reports a hop count as the
       answer and neither accepts edge weights.

THE MAPPING THIS CLASSIFICATION ASSUMES (stated because the verdicts are
only meaningful relative to one). Node -> node version, uid
`"<Type>:<id>"`, label = the LDBC type, props = the remaining attributes,
`vt_s` = `creationDate` where the type has one and `vt_e` = OPEN_END.
Edge -> edge version, `rel_type` = the LDBC edge type, `vt_s` =
`creationDate` for the three edge types that carry one. Fifteen of the
schema's twenty edge-type rows carry no attribute at all
(`tables/table-relations.tex`), so their valid time is *invented* by the
adapter; the classification charitably assumes the best available choice
(the later of the two endpoints' creation dates, which the spec's
referential-integrity rule permits) and never penalises a query for it.

WHAT THE `temporal_predicate` FIELD MEANS. True when the template filters,
buckets, or compares on a temporal attribute at all. It is deliberately
generous: `IC10`'s birthday window and `IC7`'s latency arithmetic count.
No template references a second clock, so there is no belief-clock column —
the count would be 0 of 41 by construction, which is the finding.

Usage:
  python3 scripts/ldbc_fit.py report   # tables + benchmarks/ldbc-fit-v1/
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

#: Reference implementations the shapes were read from, quoted in the
#: supporting analysis. Interactive is v1 because v1 is the only version for
#: which audits can be commissioned (spec `interactive-v2.tex`: "As of
#: January 2024, commissioning audits for this workload is not yet
#: possible.").
SOURCES = {
    "IS": "https://github.com/ldbc/ldbc_snb_interactive_v1_impls"
          "/tree/main/cypher/queries",
    "IC": "https://github.com/ldbc/ldbc_snb_interactive_v1_impls"
          "/tree/main/cypher/queries",
    "BI": "https://github.com/ldbc/ldbc_snb_bi/tree/main/neo4j/queries",
}

# value = (class, need-tags or operator chain, temporal_predicate, title,
#          justification)
L: dict[str, tuple[int, str, bool, str, str]] = {
    # ---------------- Interactive Short (IS1-IS7) ---------------- #
    "IS1": (2, "entity_history+filter", False, "Profile of a person",
            "one entity_history(uid, include_edges) carries every returned "
            "scalar in the version's props and creationDate as vt_s; "
            "compute filter(rel_type eq IS_LOCATED_IN) over the incident "
            "edges yields the City uid, which is `cityId` under the mapping"),
    "IS2": (3, "PAT,PROP", False, "Recent messages of a person",
            "the ten most recent messages are reachable (incident "
            "HAS_CREATOR edges, then topk on vt_s), but each one then needs "
            "a variable-length typed REPLY_OF walk to its root Post and the "
            "root author's names — a per-row expansion with no iteration "
            "primitive in the plan language"),
    "IS3": (3, "PROP", False, "Friends of a person",
            "friend uids and friendship dates come from one "
            "entity_history+filter chain; firstName/lastName per friend do "
            "not — operators take one uid, no operator projects props for a "
            "set of nodes, and the plan DAG has no map"),
    "IS4": (1, "entity_history", False, "Content of a message",
            "one call on the Message uid: creationDate is the version's "
            "vt_s and content/imageFile are its props"),
    "IS5": (2, "entity_history+filter+entity_history", False,
            "Creator of a message",
            "the incident HAS_CREATOR edge gives the creator uid; a $ref "
            "binds that scalar into a second entity_history whose props "
            "carry firstName and lastName"),
    "IS6": (3, "PAT,PROP", False, "Forum of a message",
            "variable-length typed REPLY_OF walk to the root Post, then "
            "CONTAINER_OF and HAS_MODERATOR, then the moderator's names"),
    "IS7": (3, "PAT,PROP,NEG", False, "Replies of a message",
            "two-hop typed pattern to each reply's author, per-author name "
            "projection, and an optional KNOWS test whose *absence* is part "
            "of the answer"),

    # ---------------- Interactive Complex (IC1-IC14, v1) ---------------- #
    "IC1": (3, "SP,PROP,PAT", False, "Transitive friends with a certain name",
            "ranks friends by shortest-path distance 1..3 (a hop count, "
            "which no operator returns), selects on firstName equality, and "
            "projects thirteen attributes plus nested university and "
            "company tuples"),
    "IC2": (3, "PAT,PROP", True, "Recent messages by your friends",
            "one-hop KNOWS then HAS_CREATOR into messages under a date "
            "bound is a typed two-way pattern; the answer projects message "
            "content and each author's names"),
    "IC3": (3, "PAT,PROP,SET,AR,NEG", True,
            "Friends and friends of friends in countries X and Y",
            "two country restrictions reached through Message->Country, an "
            "intersection of the two per-friend count sets, their sum, and "
            "a not-located-in exclusion"),
    "IC4": (3, "PAT,PROP,NEG,G", True, "New topics",
            "tags of friends' posts inside a window minus tags used before "
            "it — a typed three-hop pattern with a set difference over "
            "grouped counts"),
    "IC5": (3, "PAT,PROP,G", True, "New groups",
            "forums joined after a date by friends and friends-of-friends, "
            "counted by that same friend's posts inside each forum: a "
            "three-way typed join underneath a grouping"),
    "IC6": (3, "PAT,PROP,G", False, "Tag co-occurrence",
            "co-occurring tags on friends' posts carrying a given tag; tag "
            "identity is reached through HAS_TAG and the anchor tag is "
            "excluded from its own histogram"),
    "IC7": (3, "PAT,PROP,AR,NEG", True, "Recent likers",
            "most recent like per liker, latency in minutes from the "
            "message's creation (a division), and an is-new test against "
            "the KNOWS set"),
    "IC8": (3, "PAT,PROP", False, "Recent replies",
            "replies to any message of a person — a typed three-hop "
            "pattern — projecting reply content and author names"),
    "IC9": (3, "PAT,PROP", True, "Recent messages by friends or friends of "
            "friends",
            "two-hop KNOWS expansion into messages under a date bound, "
            "projecting content and author names"),
    "IC10": (3, "PAT,PROP,CAL,AR,SET", True, "Friend recommendation",
             "exactly-two-hop friends filtered by a birthday month/day "
             "window, scored as common-interest minus non-common counts"),
    "IC11": (3, "PAT,PROP,CAL", True, "Job referral",
             "friends and friends-of-friends with a WORK_AT edge into a "
             "named country before a year; the year predicate is on an "
             "integer attribute and the company->country restriction is a "
             "typed two-hop pattern"),
    "IC12": (3, "PAT,PROP,G", False, "Expert search",
             "comments replying to posts whose tags sit under a TagClass, "
             "requiring a variable-length IS_SUBCLASS_OF walk and a "
             "per-friend distinct count"),
    "IC13": (3, "SP,PAT", False, "Single shortest path",
             "unbounded shortest-path length over KNOWS. temporal_paths "
             "caps at six hops and ranks by (arrival, hops, edge sequence) "
             "rather than reporting the minimum hop count; "
             "temporal_reachability returns earliest arrival, not distance"),
    "IC14": (3, "SP,PAT,AR", False, "Trusted connection paths",
             "all shortest paths over KNOWS plus a weight summed from "
             "interaction counts. v2 replaces this with a Dijkstra cheapest "
             "path, which is no closer to any operator"),

    # ---------------- Business Intelligence (BI1-BI20) ---------------- #
    "BI1": (3, "G,CAL,AR,PROP", True, "Posting summary",
            "three grouping dimensions (year x isComment x length bucket) "
            "against a cap of two, a calendar year rather than a fixed "
            "stride, an aggregate over message length (a JSON prop — "
            "explicitly deferred in D-044), an average, and a percentage of "
            "a global total"),
    "BI2": (3, "PAT,SET,AR,PROP", True, "Tag evolution",
            "the nearest miss on the aggregation side: per-tag counts in "
            "one window ARE an aggregate_events grouping by dst endpoint. "
            "The two windows must then be joined per tag, differenced with "
            "abs(), restricted to a TagClass through HAS_TYPE, and reported "
            "by tag name"),
    "BI3": (3, "PAT,PROP,G", False, "Popular topics in a country",
            "forums in a country whose threads carry a TagClass, counting "
            "messages over a variable-length reply chain"),
    "BI4": (3, "PAT,PROP,G,SET", True, "Top message creators",
            "top-100 forums by member count, then top message creators "
            "inside them, with a UNION ALL over two branches"),
    "BI5": (3, "PAT,PROP,AR,SET", False, "Most active posters",
            "per-poster message, reply and like counts joined per person "
            "and combined as 1*m + 2*r + 10*l"),
    "BI6": (3, "PAT,PROP,G,SET", False, "Most authoritative users",
            "two-level like propagation: per-message like counts feed a "
            "per-author authority score, a join between two groupings"),
    "BI7": (3, "PAT,PROP,NEG,G", False, "Related topics",
            "tags carried by replies to messages carrying a tag, excluding "
            "that tag itself"),
    "BI8": (3, "PAT,PROP,AR,SET", True, "Central person for a tag",
            "interest and message scores per person plus the summed scores "
            "of that person's friends — a join of two groupings and a "
            "weighted sum (100*i + m)"),
    "BI9": (3, "PAT,PROP,G,AR", True, "Top thread initiators",
            "per-thread message counts and length sums over "
            "variable-length reply chains inside a window"),
    "BI10": (3, "PAT,PROP,SET,G", False, "Experts in social circle",
             "a variable-length friend expansion at distance 3-4, a country "
             "restriction, a TagClass restriction and an explicit set "
             "difference"),
    "BI11": (3, "PAT,SET", True, "Friend triangles",
             "the nearest miss in the whole set. M_triangle_cyclic and "
             "M_triangle_acyclic_1 between them cover both orientation "
             "classes of a triangle, and the date window is a valid-time "
             "window. But the country restriction is a typed two-hop "
             "pattern (Person->City->Country), node_filter caps at 10,000 "
             "uids against 68,673 Persons at SF10, and the delta-motif "
             "contract counts *time-ordered* instances under a span bound "
             "rather than undirected triangles, so the two counts would "
             "have to be argued equal rather than read off"),
    "BI12": (3, "PROP,G,AR", True, "How many persons have a given number of "
             "messages",
             "a histogram of persons by their qualifying-message count — a "
             "grouping over the result of a grouping — with a message "
             "length predicate on a prop"),
    "BI13": (3, "PAT,PROP,CAL,AR,SET", True, "Zombies in a country",
             "the zombie definition is a per-calendar-month message rate "
             "since account creation, and the reported ratio needs a join "
             "between the zombie set and its likers"),
    "BI14": (3, "PAT,PROP,AR,SET,G", False, "International dialog",
             "per-city top interacting person pair across two countries, "
             "scored 4/1/10/1 over four distinct reply patterns"),
    "BI15": (3, "SP,PAT,AR", True, "Weighted interaction paths",
             "a weighted shortest path where the weight is "
             "1/(interaction count + 1) computed over a window — a Dijkstra "
             "over a derived projection"),
    "BI16": (3, "PAT,PROP,CAL,SET,AR", True, "Fake news detection",
             "persons who posted about two different tags on two given "
             "calendar dates, joined per person and summed"),
    "BI17": (3, "PAT,PROP,G,SET", True, "Information propagation analysis",
             "an eight-step typed pattern with two variable-length reply "
             "chains and a duration offset between two message times"),
    "BI18": (3, "PAT,PROP,G,NEG", False, "Friend recommendation",
             "mutual friends sharing a tag, excluding pairs that already "
             "know each other"),
    "BI19": (3, "SP,PAT,AR", False, "Interaction path between cities",
             "cheapest interaction path between persons in two cities over "
             "a derived weighted projection"),
    "BI20": (3, "SP,PAT,AR,PROP", False, "Recruitment",
             "cheapest path weighted by the difference of study years — a "
             "Dijkstra over a projection built from STUDY_AT attributes"),
}

# --------------------------------------------------------------------------- #
# re-audit after the D-051 session extended O13 `compute` with arithmetic      #
# --------------------------------------------------------------------------- #
# A diff, not a rewrite: `L` stays as D-050 published it and an entry absent
# here keeps its verdict. Same shape and same discipline as `C15` in
# scripts/independent_questions.py, and the same finding — `AR` was three
# capabilities under one label, and the two that remain here are `ROW`
# (row-wise arithmetic over a prior step's rows: a derived column, a
# per-group score, a weighted sum) and nothing else. **No template changes
# class**: every one of these is also gated by `PAT` or `SP`, so the LDBC
# number is unchanged at 3 of 41, exactly as the handoff predicted.
# value = (class, need tags, justification)
L15: dict[str, tuple[int, str, str]] = {
    "IC3": (3, "PAT,PROP,SET,ROW,NEG",
            "the two per-country counts are summed per friend once the sets "
            "are joined — a derived column, not a scalar quotient"),
    "IC7": (3, "PAT,PROP,ROW,NEG",
            "latency in minutes is (like time - message creation) per row, "
            "then a divide — a derived column, not a scalar quotient"),
    "IC10": (3, "PAT,PROP,CAL,ROW,SET",
             "the common-minus-non-common score is per candidate friend"),
    "IC14": (3, "SP,PAT,ROW",
             "the path weight is summed per path from interaction counts"),
    "BI1": (3, "G,CAL,ROW,PROP",
            "both remaining numbers are per bucket — the average message "
            "length within the group and that group's share of the global "
            "total — so a whole-input mean and a two-scalar percent reach "
            "neither"),
    "BI2": (3, "PAT,SET,ROW,PROP",
            "the two windows are differenced per tag, with abs()"),
    "BI5": (3, "PAT,PROP,ROW,SET",
            "1*m + 2*r + 10*l is a weighted sum per person"),
    "BI8": (3, "PAT,PROP,ROW,SET",
            "100*i + m is a weighted sum per person"),
    "BI9": (3, "PAT,PROP,G",
            "AR was over-tagged here: counts and sums are original-13 "
            "aggregates, and what blocks the template is reaching message "
            "length (a prop) under a grouping over a variable-length walk"),
    "BI12": (3, "PROP,G",
             "AR was over-tagged here too: a histogram of persons by their "
             "message count is a grouping over a grouping, and the "
             "threshold is `filter(count ge k)`, which shipped with the "
             "original thirteen"),
    "BI13": (3, "PAT,PROP,CAL,ROW,SET",
             "the zombie rate is per person: messages over months since "
             "that person's own account creation"),
    "BI14": (3, "PAT,PROP,ROW,SET,G",
             "the 4/1/10/1 score is per city-pair row"),
    "BI15": (3, "SP,PAT,ROW",
             "1/(interaction count + 1) is a derived edge weight"),
    "BI16": (3, "PAT,PROP,CAL,SET,ROW",
             "the two tag counts are summed per person"),
    "BI19": (3, "SP,PAT,ROW",
             "a derived weighted projection, as BI15"),
    "BI20": (3, "SP,PAT,ROW,PROP",
             "the weight is the difference of study years, per edge"),
}

# --------------------------------------------------------------------------- #
# re-audit after D-052's property typing shipped (the D-053 session)          #
# --------------------------------------------------------------------------- #
# `aggregate_events` gained a predicate and min/max/mean over an **edge**
# property. Two facts decide this table, and both are worth stating plainly
# because they are the difference between the two workloads:
#   1. the implementation reaches *edge* properties only. LDBC's property
#      demand is overwhelmingly *node* attributes — a Person's names, a
#      Message's content and length — which it does not touch;
#   2. most of that demand is **projection**, not predication: the template
#      must return the attribute, not test it. That is `PROJ`, the tag the
#      companion study also grew this session.
# **No template changes class.** LDBC stays 3 of 41 for the third session
# running, because `PAT` gates 35 of the 38 and nothing here is `PAT`.
# value = (class, need tags, justification)
L16: dict[str, tuple[int, str, str]] = {
    "IS2": (3, "PAT,PROJ",
            "the missing half is message content and the root author's "
            "names in the answer — projection, not a predicate"),
    "IS3": (3, "PROJ",
            "the one template `PROP` was the sole blocker of, and it wants "
            "firstName/lastName *per friend in the output*. An aggregate "
            "reduces a property to one number and a predicate compares it "
            "to a literal; neither returns it"),
    "IS6": (3, "PAT,PROJ", "the moderator's names in the answer"),
    "IS7": (3, "PAT,PROJ,NEG", "per-author name projection"),
    "IC1": (3, "SP,PROP,PROJ,PAT",
            "both halves, and they are different capabilities: firstName "
            "equality is a predicate on a node attribute, the thirteen "
            "returned attributes and the nested tuples are projection"),
    "IC2": (3, "PAT,PROJ", "message content and author names in the answer"),
    "IC8": (3, "PAT,PROJ", "reply content and author names in the answer"),
    "IC9": (3, "PAT,PROJ", "content and author names in the answer"),
}

WORKLOADS = {"IS": "Interactive Short", "IC": "Interactive Complex (v1)",
             "BI": "Business Intelligence"}


def workload(qid: str) -> str:
    return "IS" if qid.startswith("IS") else ("IC" if qid.startswith("IC")
                                              else "BI")


def _needs(cls: int, tags: str) -> list[str]:
    """Need tags of a class-3 entry; class 1-2 entries carry an operator
    chain in the same slot and contribute nothing (same rule as
    scripts/independent_questions.py)."""
    if cls != 3:
        return []
    return [t for t in tags.split(",") if t and "+" not in t]


#: Every token a class-1/2 chain may name: an operator in the registry, or a
#: `compute` function. Guards the table against a chain that quietly invents
#: a capability, which is the failure mode this instrument exists to avoid.
CHAIN_VOCAB = {
    "entity_history", "snapshot_subgraph", "diff_snapshots",
    "neighborhood_evolution", "resolve_entities", "graph_metric_timeseries",
    "burst_detection", "co_active", "count_temporal_motifs",
    "find_temporal_motif_instances", "temporal_reachability",
    "temporal_paths", "compute", "aggregate_events",
    "count", "sum", "min", "max", "topk", "filter", "interval_relation",
    # O13 `compute` arithmetic, added by the D-051 session
    "mean", "median", "ratio", "diff", "percent",
}

#: Every tag a class-3 entry may carry. `AR` is retired by `L15` (it named
#: three capabilities); `PCT` is in the vocabulary because the companion
#: study needs it, and the fact that **no LDBC template needs it** is itself
#: a difference between the two workloads worth being able to state.
TAG_VOCAB = {"G", "PROP", "CAL", "SET", "NEG", "GLOB", "SEQ",
             "PAT", "SP", "ROW", "PCT", "PROJ"}
#: Retired tags: still legal in `L`, never legal in a re-audit.
RETIRED_TAGS = {"AR"}


def verdict15(qid: str) -> tuple[int, str]:
    """The 15th-capability verdict: `L15` if it re-audited, else `L`."""
    return (L15[qid][0], L15[qid][1]) if qid in L15 else (L[qid][0], L[qid][1])


def verdict(qid: str) -> tuple[int, str]:
    """The current verdict: `L16` if it re-audited, else the `L15` one."""
    return (L16[qid][0], L16[qid][1]) if qid in L16 else verdict15(qid)


def _check() -> None:
    assert len(L) == 41, len(L)
    for qid, (cls, tags, _tp, title, why) in L.items():
        assert cls in (1, 2, 3), f"{qid}: class {cls} cannot arise here"
        assert title and why, f"{qid} is missing a title or justification"
        if cls == 3:
            unknown = set(tags.split(",")) - TAG_VOCAB - RETIRED_TAGS
            assert not unknown, f"{qid}: unknown need tag(s) {unknown}"
        else:
            unknown = set(tags.split("+")) - CHAIN_VOCAB
            assert not unknown, f"{qid}: chain names non-operator {unknown}"
            assert (cls == 1) == ("+" not in tags), (
                f"{qid}: class 1 is a single operator, class 2 a chain")
    for name, table, base in (("L15", L15, lambda q: (L[q][0], L[q][1])),
                              ("L16", L16, verdict15)):
      for qid, (cls, tags, why) in table.items():
        assert qid in L, f"{name} {qid} is not an LDBC template"
        assert why, f"{name} {qid} has no justification"
        assert (cls, tags) != base(qid), f"{name} {qid} is not a change"
        assert cls <= base(qid)[0], f"{name} {qid} moved backwards"
        unknown = set(tags.split(",")) - TAG_VOCAB if cls == 3 \
            else set(tags.split("+")) - CHAIN_VOCAB
        assert not unknown, f"{name} {qid}: unknown tag(s) {unknown}"
    assert not [q for q in L if set(_needs(*verdict(q))) & RETIRED_TAGS], \
        "a retired tag survived the L15 re-audit"


def report() -> None:
    _check()
    print("class distribution (D-050, 14 ops):",
          dict(sorted(Counter(L[q][0] for q in L).items())))
    print("class distribution (D-052 session re-audit):",
          dict(sorted(Counter(verdict(q)[0] for q in L).items())))
    expressible = sorted(q for q in L if verdict(q)[0] <= 2)
    print(f"expressible: {len(expressible)} of {len(L)} — "
          f"{', '.join(expressible)}")
    for pfx, name in WORKLOADS.items():
        qs = [q for q in L if workload(q) == pfx]
        ok = [q for q in qs if verdict(q)[0] <= 2]
        print(f"  {name:<26} {len(ok)}/{len(qs)}")

    print("missing capabilities (class 3, D-050):",
          dict(Counter(t for q in L
                       for t in _needs(L[q][0], L[q][1])).most_common()))
    print("missing capabilities (class 3, after both re-audits):",
          dict(Counter(t for q in L
                       for t in _needs(*verdict(q))).most_common()))
    moved = [q for q in L if verdict(q)[0] != L[q][0]]
    print(f"templates that changed class: {len(moved) or 'none'}"
          f"{' — ' + ', '.join(moved) if moved else ''}; "
          f"{len(L15)} + {len(L16)} re-audited tag strings")
    tp = sum(1 for q in L if L[q][2])
    print(f"templates with a predicate on a temporal attribute: {tp} of {len(L)}")
    print(f"templates referencing a second clock (belief/as-of): 0 of {len(L)}")

    rows = [{"id": q, "workload": WORKLOADS[workload(q)],
             "source": SOURCES[workload(q)], "title": L[q][3],
             "class": L[q][0], "need_or_ops": L[q][1],
             "class_15": verdict(q)[0], "need_or_ops_15": verdict(q)[1],
             "justification_15": L15[q][2] if q in L15 else "",
             "temporal_predicate": L[q][2], "belief_clock": False,
             "justification": L[q][4]}
            for q in sorted(L, key=lambda k: (list(WORKLOADS).index(workload(k)),
                                              int(k[2:])))]
    out = Path("benchmarks/ldbc-fit-v1/classification.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=1))
    print("wrote", out)


if __name__ == "__main__":
    {"report": report}[sys.argv[1] if len(sys.argv) > 1 else "report"]()
