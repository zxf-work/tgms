#!/usr/bin/env python
"""LDBC 41-template coverage annotation (D-134, second-annotator pass).

Two independent axes, each with a mechanical decision rule.

EXECUTION COVERAGE, against the frozen registry (O1-O15 as returned by
tgms.temporal.algebra.REGISTRY):
  DIRECT_TGMS         one registered operator invocation computes the
                      required relation; field selection/renaming for
                      presentation does not count as a second operator
  DECOMPOSABLE_TGMS   an explicit DAG over registered operators and the
                      deterministic `compute` vocabulary only: no hidden
                      database reads, no unregistered recursion, graph
                      algorithm, or group-by
  SQL_ONLY            no such DAG, but the SQL path expresses the query
  UNSUPPORTED_EXECUTION  neither current surface implements a needed
                      primitive
Every DIRECT/DECOMPOSABLE row carries `exec_witness`, a plan over named
operators; every SQL_ONLY row names the missing TGMS capability; every
UNSUPPORTED row names the primitive missing from both surfaces. The
witnesses are constructed against the frozen operator schemas and are
NOT executed against an LDBC instance (no LDBC data is loaded into
TGMS); they make the classification auditable, not benchmarked.

CLAIM COVERAGE, the first result-contract feature the ECQR grammar
cannot express, as a mutually exclusive partition in this precedence:
  REQUIRES_PATH_CERTIFICATE     the returned object itself has path
                                structure
  REQUIRES_GROUPWISE_EXTREMUM   an extremum per group before global
                                presentation
  REQUIRES_TOP_K                global sort/rank then a finite semantic
                                LIMIT selecting k of a larger relation
  REQUIRES_ORDERED_RESULT       all qualifying rows returned, but the
                                contract fixes their sequence
  CURRENT_ECQR_FRAGMENT         nothing beyond the current six forms
A complex algorithm inside trusted Q does not by itself move a result
out of the fragment: the audit classifies the RETURNED CONTRACT, not
every operation inside Q. A scalar produced by a trusted shortest-path
query is a Scalar; a returned path sequence is not.

PROJECTION (Policy A, flat atomic tuples). `CompleteSet(S,f)` carries a
scalar atom or a flat tuple of scalar atoms, never a nested list, set,
or path sequence. `flat_projection_in_fragment` records whether the
unordered projection of the returned rows fits that rule; execution
difficulty is irrelevant to it. `duplicate_safe` records why the set
comparison is faithful, since CompleteSet is duplicate-insensitive.

    python external_workloads/scripts/annotate_ldbc_coverage.py \
        --manifest external_workloads/ldbc/ldbc_read_templates.jsonl \
        --out external_workloads/ldbc/coverage_annotation.jsonl \
        --receipt benchmarks/results-v1/eval-ldbc-coverage.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

D, DEC, SQL, UNS = ("DIRECT_TGMS", "DECOMPOSABLE_TGMS", "SQL_ONLY",
                    "UNSUPPORTED_EXECUTION")
CUR, ORD, TOPK, GEX, PATH = ("CURRENT_ECQR_FRAGMENT",
                             "REQUIRES_ORDERED_RESULT", "REQUIRES_TOP_K",
                             "REQUIRES_GROUPWISE_EXTREMUM",
                             "REQUIRES_PATH_CERTIFICATE")
KEY, GRP, DIST = ("GUARANTEED_BY_KEY", "GUARANTEED_BY_GROUP",
                  "SINGLE_ROW")

# query_id: (exec, claim, flat_projection, duplicate_safe, witness, note)
C = {
 "IC1": (SQL, TOPK, False, KEY,
   {"missing_tgms_capability": ["correlated_sub_aggregation",
                                "nested_collection_projection"]},
   "3-hop knows plus per-row nested university/company collections; "
   "sorted with semantic limit 20. Nested result fields put the "
   "projection outside Policy A"),
 "IC2": (DEC, TOPK, True, KEY,
   {"plan": ["snapshot_subgraph(seeds=[person], hops=2, "
             "rel_types=[knows, hasCreator])",
             "compute(fn=filter, cmp=le, field=creationDate)",
             "compute(fn=topk, k=20)"],
    "witness_status": "CONSTRUCTED_NOT_EXECUTED"},
   "friends' recent messages, sorted, semantic limit 20"),
 "IC3": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["grouped_join_over_two_filters"]},
   "paired per-person message counts over two location filters"),
 "IC4": (SQL, TOPK, True, GRP,
   {"missing_tgms_capability": ["set_difference_over_grouped_results"]},
   "window-vs-history tag difference; sorted, semantic limit 10"),
 "IC5": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["membership_conditioned_grouped_count"]},
   "forum membership since join date against friend posts"),
 "IC6": (SQL, TOPK, True, GRP,
   {"missing_tgms_capability": ["tag_cooccurrence_grouping"]},
   "tag co-occurrence over friend-of-friend posts, limit 10"),
 "IC7": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["per_row_latest_selection",
                                "derived_latency_field"]},
   "per-liker latest like with latency and isNew flag, limit 20"),
 "IC8": (DEC, TOPK, True, KEY,
   {"plan": ["snapshot_subgraph(seeds=[person], hops=3, "
             "rel_types=[hasCreator, replyOf])",
             "compute(fn=topk, k=20)"],
    "witness_status": "CONSTRUCTED_NOT_EXECUTED"},
   "direct replies to the person's messages, sorted, limit 20"),
 "IC9": (DEC, TOPK, True, KEY,
   {"plan": ["snapshot_subgraph(seeds=[person], hops=3, "
             "rel_types=[knows, hasCreator])",
             "compute(fn=filter, cmp=lt, field=creationDate)",
             "compute(fn=topk, k=20)"],
    "witness_status": "CONSTRUCTED_NOT_EXECUTED"},
   "messages of friends and friends-of-friends before a date, limit 20"),
 "IC10": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["per_candidate_post_partition_scoring"]},
   "one global common-interest score then limit 10; no groupwise rank"),
 "IC11": (DEC, TOPK, True, KEY,
   {"plan": ["snapshot_subgraph(seeds=[person], hops=2, "
             "rel_types=[knows])",
             "snapshot_subgraph(seeds=<candidates>, hops=1, "
             "rel_types=[workAt])",
             "snapshot_subgraph(seeds=<companies>, hops=1, "
             "rel_types=[isLocatedIn])",
             "compute(fn=join, on=companyId)",
             "compute(fn=filter, field=country)",
             "compute(fn=topk, k=10)"],
    "witness_status": "CONSTRUCTED_NOT_EXECUTED",
    "witness_note": "the candidate-to-company-to-country path exceeds "
                    "one hops<=3 snapshot, so the plan re-seeds a "
                    "second and third snapshot from prior step output; "
                    "if that re-seeding is not admissible the template "
                    "is SQL_ONLY"},
   "friend/FoF work histories in a country, limit 10"),
 "IC12": (SQL, TOPK, False, KEY,
   {"missing_tgms_capability": ["tag_class_hierarchy_descent",
                                "nested_collection_projection"]},
   "isSubclassOf* descent; nested tagNames put the projection "
   "outside Policy A"),
 "IC13": (SQL, CUR, True, DIST,
   {"missing_tgms_capability": ["static_variable_length_shortest_path"]},
   "single shortest-path LENGTH over the static knows graph. The "
   "frozen registry has no static hop-distance operator: "
   "snapshot_subgraph stops at hops<=3, temporal_reachability "
   "optimizes earliest arrival under time-respecting semantics, and "
   "temporal_paths enumerates bounded time-respecting paths. The "
   "returned contract is one scalar, so the CLAIM stays in fragment"),
 "IC14": (UNS, PATH, False, KEY,
   {"missing_both_surfaces": ["all_shortest_paths_enumeration",
                              "per_path_weight_scoring"]},
   "returns an ordered vertex sequence per path; the user-visible "
   "object itself has path structure"),
 "IS1": (D, CUR, True, DIST,
   {"plan": ["snapshot_subgraph(seeds=[person], hops=1, "
             "rel_types=[isLocatedIn])"],
    "witness_status": "CONSTRUCTED_NOT_EXECUTED"},
   "single-entity attributes plus the city edge"),
 "IS2": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["unbounded_replyof_root_closure"]},
   "the last 10 messages AND, for each, the original post of its "
   "conversation. Comment threads are not depth-bounded by the "
   "template, and the registry has no reply-root closure"),
 "IS3": (D, ORD, True, KEY,
   {"plan": ["snapshot_subgraph(seeds=[person], hops=1, "
             "rel_types=[knows])"],
    "witness_status": "CONSTRUCTED_NOT_EXECUTED"},
   "the complete knows neighborhood with edge attribute; ordered, "
   "no semantic limit"),
 "IS4": (D, CUR, True, DIST,
   {"plan": ["entity_history(uid=<message>)"],
    "witness_status": "CONSTRUCTED_NOT_EXECUTED"},
   "single-message attribute lookup"),
 "IS5": (D, CUR, True, DIST,
   {"plan": ["snapshot_subgraph(seeds=[message], hops=1, "
             "rel_types=[hasCreator])"],
    "witness_status": "CONSTRUCTED_NOT_EXECUTED"},
   "message to creator, one hop"),
 "IS6": (SQL, CUR, True, DIST,
   {"missing_tgms_capability": ["unbounded_replyof_root_closure"]},
   "for a comment, walk the reply chain to the root post, then its "
   "forum and moderator; the chain is not depth-bounded"),
 "IS7": (DEC, ORD, True, KEY,
   {"plan": ["snapshot_subgraph(seeds=[message], hops=1, "
             "rel_types=[replyOf, hasCreator])",
             "snapshot_subgraph(seeds=[person], hops=1, "
             "rel_types=[knows])",
             "compute(fn=join, on=personId)"],
    "witness_status": "CONSTRUCTED_NOT_EXECUTED",
    "witness_note": "the knows flag is a membership test against the "
                    "1-hop knows set of the original author, joined "
                    "without per-row database reads"},
   "direct replies with a knows flag; ordered, no semantic limit"),
 "BI1": (SQL, ORD, True, GRP,
   {"missing_tgms_capability": ["three_dimensional_group_by",
                                "derived_length_category_dimension"]},
   "grouping by year, message type, and a DERIVED content-length "
   "category. aggregate_events groups edge events by at most two "
   "dimensions from a closed set (time_bucket, calendar_unit, "
   "rel_type, endpoint, label), so this is not expressible"),
 "BI2": (SQL, TOPK, True, GRP,
   {"missing_tgms_capability": ["two_window_grouped_difference"]}, ""),
 "BI3": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["place_hierarchy_join"]}, ""),
 "BI4": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["per_country_grouped_ranking"]}, ""),
 "BI5": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["composite_activity_scoring"]}, ""),
 "BI6": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["two_level_like_authority_score"]}, ""),
 "BI7": (SQL, TOPK, True, GRP,
   {"missing_tgms_capability": ["reply_tag_cooccurrence"]}, ""),
 "BI8": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["interest_plus_reply_composite_score"]},
   ""),
 "BI9": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["recursive_reply_closure"]}, ""),
 "BI10": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["hop_range_selection",
                                "per_person_interest_counts"]}, ""),
 "BI11": (SQL, CUR, True, DIST,
   {"missing_tgms_capability": ["static_triangle_enumeration"]},
   "a scalar count. The registered motif operators are temporal and "
   "not equivalent to the static triangle query, but the returned "
   "contract is one scalar"),
 "BI12": (SQL, ORD, True, GRP,
   {"missing_tgms_capability": ["count_of_counts_two_stage_grouping"]},
   "histogram groups, sorted, no semantic limit"),
 "BI13": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["zombie_activity_ratio_scoring"]}, ""),
 "BI14": (SQL, GEX, True, KEY,
   {"missing_tgms_capability": ["per_city_pair_scoring"]},
   "the highest-scoring pair WITHIN each city with a tie-break, then "
   "global presentation: a groupwise extremum, not one global top-k"),
 "BI15": (UNS, CUR, True, DIST,
   {"missing_both_surfaces": ["weighted_shortest_path"]},
   "execution needs a weighted shortest path absent from both "
   "surfaces, but the returned contract is one scalar cost, so the "
   "claim is in fragment under the query-relative rule"),
 "BI16": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["two_date_windowed_maxima"]}, ""),
 "BI17": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["cascade_pattern_with_time_deltas"]},
   ""),
 "BI18": (SQL, TOPK, True, KEY,
   {"missing_tgms_capability": ["mutual_friend_scoring_with_exclusions"]},
   ""),
 "BI19": (UNS, ORD, True, KEY,
   {"missing_both_surfaces": ["weighted_shortest_path"]},
   "execution is unsupported, but the returned row is the flat tuple "
   "(person1.id, person2.id, totalWeight), so the projection is "
   "representable; the two axes are independent"),
 "BI20": (UNS, TOPK, True, KEY,
   {"missing_both_surfaces": ["constrained_weighted_shortest_path"]},
   "execution is unsupported, but the returned row is the flat tuple "
   "(person1.id, totalWeight)"),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--receipt", type=Path, required=True)
    args = ap.parse_args()

    rows = [json.loads(line) for line in open(args.manifest)]
    assert {r["query_id"] for r in rows} == set(C), "coverage mismatch"
    out_rows, ex, cl = [], {}, {}
    for r in rows:
        e, c, flat, dup, wit, note = C[r["query_id"]]
        rec = {"query_id": r["query_id"], "workload": r["workload"],
               "title": r["title"], "exec_coverage": e,
               "exec_witness": wit, "claim_full_contract": c,
               "flat_projection_in_fragment": flat,
               "duplicate_safe": dup if flat else None,
               "note": note,
               "annotation_status": "second-annotator adjudicated"}
        ex[e] = ex.get(e, 0) + 1
        cl[c] = cl.get(c, 0) + 1
        out_rows.append(rec)
    with open(args.out, "w") as f:
        for rec in out_rows:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
    receipt = {
        "n_templates": len(out_rows),
        "exec_coverage_counts": ex,
        "claim_full_contract_counts": cl,
        "flat_projection_in_fragment": sum(
            1 for r in out_rows if r["flat_projection_in_fragment"]),
        "projection_policy": "Policy A: CompleteSet carries a scalar "
                             "atom or a flat tuple of scalar atoms; "
                             "nested lists, sets, and path sequences "
                             "are outside the projection grammar",
        "projection_excluded": [r["query_id"] for r in out_rows
                                if not r["flat_projection_in_fragment"]],
        "duplicate_safe_counts": {
            k: sum(1 for r in out_rows if r["duplicate_safe"] == k)
            for k in (KEY, GRP, DIST)},
        "claim_partition_precedence": [PATH, GEX, TOPK, ORD, CUR],
        "exec_witness_status": "plans constructed against the frozen "
                               "operator schemas; not executed against "
                               "an LDBC instance",
        "manifest_sha256": hashlib.sha256(
            open(args.manifest, "rb").read()).hexdigest(),
        "status": "second-annotator adjudicated (D-134)",
    }
    args.receipt.write_text(json.dumps(receipt, indent=1) + "\n")
    print(json.dumps(receipt, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
