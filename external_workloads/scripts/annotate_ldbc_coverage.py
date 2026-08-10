#!/usr/bin/env python
"""LDBC 41-template coverage annotation (DRAFT, D-129).

Two independent dimensions per template, judged from the pinned
official specifications against the frozen TGMS operator surface
(15 registered operators; no weighted paths, no per-row nested
aggregation, no recursive closures, no hierarchy traversal, no
triangle operator) and the frozen ECQR claim grammar:

  exec_coverage: DIRECT_TGMS | DECOMPOSABLE_TGMS | SQL_ONLY |
                 UNSUPPORTED_EXECUTION
  claim_full_contract: the LDBC result contract INCLUDING its sort
                 specification and semantic limit:
                 CURRENT_ECQR_FRAGMENT | REQUIRES_ORDERED_RESULT |
                 REQUIRES_TOP_K | REQUIRES_RANKING |
                 REQUIRES_PATH_CERTIFICATE
  set_projection_in_fragment: whether the UNORDERED projection of the
                 result rows is certifiable by the current grammar
                 (CompleteSet / Scalar / ExactCount)

Interpretation 1 (as for BIRD): the template query, including its
internal ranking and weighting, is the trusted semantic query under
A1; claim classification concerns the requested result contract.

ANNOTATION STATUS: single-annotator draft (research team);
second-annotator adjudication pending, disagreements to be recorded
in this file before any paper number is derived from it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

# (exec, exec_rationale, claim_full, set_proj, tags)
C = {
 "IC1": ("SQL_ONLY",
         "3-hop knows traversal is expressible via reachability, but "
         "the per-row nested university/company collections need "
         "correlated sub-aggregation the operator surface lacks",
         "REQUIRES_ORDERED_RESULT", True,
         ["multi-hop", "nested-aggregation", "top-k"]),
 "IC2": ("DECOMPOSABLE_TGMS",
         "1-hop neighborhood + date filter + top-k compute",
         "REQUIRES_ORDERED_RESULT", True, ["1-hop", "top-k"]),
 "IC3": ("SQL_ONLY",
         "2-hop reach is expressible, but paired per-person message "
         "counts over two location filters need a grouped join the "
         "compute layer lacks",
         "REQUIRES_TOP_K", True, ["2-hop", "grouped-agg", "top-k"]),
 "IC4": ("SQL_ONLY",
         "window-vs-history tag set difference needs set operations "
         "over two grouped results",
         "REQUIRES_ORDERED_RESULT", True,
         ["1-hop", "windowed-agg", "set-difference"]),
 "IC5": ("SQL_ONLY",
         "forum membership since date joined against friend posts; "
         "membership-conditioned grouped counts",
         "REQUIRES_TOP_K", True, ["membership-join", "grouped-agg"]),
 "IC6": ("SQL_ONLY",
         "tag co-occurrence counts over friend-of-friend posts",
         "REQUIRES_TOP_K", True, ["2-hop", "co-occurrence"]),
 "IC7": ("SQL_ONLY",
         "per-liker latest-like with latency derivation and isNew "
         "flag join",
         "REQUIRES_ORDERED_RESULT", True,
         ["likes", "per-row-derivation"]),
 "IC8": ("DECOMPOSABLE_TGMS",
         "replies-to-my-messages neighborhood + top-k compute",
         "REQUIRES_ORDERED_RESULT", True, ["2-hop", "top-k"]),
 "IC9": ("DECOMPOSABLE_TGMS",
         "reach<=2 + date filter + top-k compute",
         "REQUIRES_ORDERED_RESULT", True, ["2-hop", "top-k"]),
 "IC10": ("SQL_ONLY",
          "recommendation scoring combines common-interest counts and "
          "post partitions per candidate",
          "REQUIRES_RANKING", True, ["2-hop", "scoring"]),
 "IC11": ("DECOMPOSABLE_TGMS",
          "reach<=2 + workAt filter (country, workFrom) + top-k",
          "REQUIRES_TOP_K", True, ["2-hop", "filter", "top-k"]),
 "IC12": ("SQL_ONLY",
          "tag-class hierarchy descent (isSubclassOf*) is not in the "
          "operator surface",
          "REQUIRES_TOP_K", True, ["hierarchy", "counts"]),
 "IC13": ("DECOMPOSABLE_TGMS",
          "unweighted shortest-path length = min hop distance from "
          "reachability with hop tracking",
          "CURRENT_ECQR_FRAGMENT", True, ["path-length", "scalar"]),
 "IC14": ("UNSUPPORTED_EXECUTION",
          "enumeration of ALL shortest paths with per-path weight "
          "scoring is outside the surface",
          "REQUIRES_PATH_CERTIFICATE", False, ["paths", "weights"]),
 "IS1": ("DIRECT_TGMS", "single-entity attribute lookup + city edge",
         "CURRENT_ECQR_FRAGMENT", True, ["lookup"]),
 "IS2": ("DECOMPOSABLE_TGMS",
         "person's messages + root-post walk + top-10 compute",
         "REQUIRES_ORDERED_RESULT", True, ["1-hop", "top-k"]),
 "IS3": ("DIRECT_TGMS",
         "complete 1-hop knows neighborhood with edge attribute",
         "REQUIRES_ORDERED_RESULT", True, ["1-hop", "complete-set"]),
 "IS4": ("DIRECT_TGMS", "single-message lookup",
         "CURRENT_ECQR_FRAGMENT", True, ["lookup"]),
 "IS5": ("DIRECT_TGMS", "message -> creator 1-hop lookup",
         "CURRENT_ECQR_FRAGMENT", True, ["lookup"]),
 "IS6": ("DECOMPOSABLE_TGMS",
         "transitive replyOf walk to root post, then forum/moderator",
         "CURRENT_ECQR_FRAGMENT", True, ["transitive-walk", "lookup"]),
 "IS7": ("DECOMPOSABLE_TGMS",
         "reply neighborhood; knows-flag derivable by membership "
         "check against the knows edge set",
         "REQUIRES_ORDERED_RESULT", True, ["1-hop", "flag-join"]),
 "BI1": ("DECOMPOSABLE_TGMS",
         "single-pass grouped aggregation over messages by "
         "year/type/length class (aggregate with group dims)",
         "REQUIRES_ORDERED_RESULT", True, ["grouped-agg"]),
 "BI2": ("SQL_ONLY",
         "two-window per-tag counts require joining two grouped "
         "aggregates",
         "REQUIRES_TOP_K", True, ["windowed-agg", "diff", "top-k"]),
 "BI3": ("SQL_ONLY", "place-hierarchy plus forum membership joins",
         "REQUIRES_TOP_K", True, ["hierarchy", "membership-join"]),
 "BI4": ("SQL_ONLY", "per-country grouped creator ranking",
         "REQUIRES_TOP_K", True, ["grouped-agg", "top-k"]),
 "BI5": ("SQL_ONLY", "composite activity scoring per person",
         "REQUIRES_TOP_K", True, ["scoring", "top-k"]),
 "BI6": ("SQL_ONLY", "authority score via likes-of-likers",
         "REQUIRES_TOP_K", True, ["2-level-likes", "scoring"]),
 "BI7": ("SQL_ONLY", "reply-tag co-occurrence excluding original tag",
         "REQUIRES_TOP_K", True, ["reply-join", "co-occurrence"]),
 "BI8": ("SQL_ONLY", "interest+reply composite score",
         "REQUIRES_TOP_K", True, ["scoring", "top-k"]),
 "BI9": ("SQL_ONLY", "transitive reply closure per thread initiator",
         "REQUIRES_TOP_K", True, ["recursive-closure", "counts"]),
 "BI10": ("SQL_ONLY",
          "hop-range friend selection plus per-person interest counts",
          "REQUIRES_TOP_K", True, ["multi-hop-range", "counts"]),
 "BI11": ("SQL_ONLY",
          "triangle counting has no registered operator",
          "CURRENT_ECQR_FRAGMENT", True, ["triangles", "count"]),
 "BI12": ("SQL_ONLY", "count-of-counts histogram (two-stage grouping)",
          "REQUIRES_ORDERED_RESULT", True, ["count-of-counts"]),
 "BI13": ("SQL_ONLY", "zombie activity-ratio scoring",
          "REQUIRES_TOP_K", True, ["activity-ratio", "top-k"]),
 "BI14": ("SQL_ONLY", "cross-country pair scoring, top pair per city",
          "REQUIRES_RANKING", True, ["pair-scoring"]),
 "BI15": ("UNSUPPORTED_EXECUTION",
          "weighted shortest-path cost over interaction-scored knows "
          "edges",
          "CURRENT_ECQR_FRAGMENT", True,
          ["weighted-path", "scalar-cost"]),
 "BI16": ("SQL_ONLY", "two-date tag-message maxima with joint filter",
          "REQUIRES_TOP_K", True, ["temporal-windows", "counts"]),
 "BI17": ("SQL_ONLY", "message cascade pattern with time deltas",
          "REQUIRES_TOP_K", True, ["cascade-pattern"]),
 "BI18": ("SQL_ONLY", "mutual-friend recommendation with exclusions",
          "REQUIRES_TOP_K", True, ["mutual-friends", "scoring"]),
 "BI19": ("UNSUPPORTED_EXECUTION",
          "cheapest interaction paths between city populations",
          "REQUIRES_ORDERED_RESULT", False, ["weighted-path"]),
 "BI20": ("UNSUPPORTED_EXECUTION",
          "constrained (same-university) weighted shortest path",
          "REQUIRES_TOP_K", False, ["constrained-path"]),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--receipt", type=Path, required=True)
    args = ap.parse_args()

    rows = [json.loads(line) for line in open(args.manifest)]
    assert {r["query_id"] for r in rows} == set(C), "coverage mismatch"
    out_rows = []
    ex_counts, cl_counts = {}, {}
    for r in rows:
        ex, exr, cl, setp, tags = C[r["query_id"]]
        rec = {"query_id": r["query_id"], "workload": r["workload"],
               "title": r["title"], "exec_coverage": ex,
               "exec_rationale": exr, "claim_full_contract": cl,
               "set_projection_in_fragment": setp,
               "semantic_tags": tags,
               "annotation_status": "draft-single-annotator"}
        ex_counts[ex] = ex_counts.get(ex, 0) + 1
        cl_counts[cl] = cl_counts.get(cl, 0) + 1
        out_rows.append(rec)
    with open(args.out, "w") as f:
        for rec in out_rows:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    receipt = {
        "n_templates": len(out_rows),
        "exec_coverage_counts": ex_counts,
        "claim_full_contract_counts": cl_counts,
        "set_projection_in_fragment": sum(
            1 for r in out_rows if r["set_projection_in_fragment"]),
        "manifest_sha256": hashlib.sha256(
            open(args.manifest, "rb").read()).hexdigest(),
        "status": "draft-single-annotator; adjudication pending",
    }
    args.receipt.write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps(receipt, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
