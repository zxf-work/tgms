"""Unit tests for Cypher generation per family (validation item 1 of N1's
task: "unit tests of the Cypher generation per family on a tiny synthetic
cell"). These test the *generation* — parameter construction and query
text — with no Neo4j involved; the live cross-check that the generated
Cypher actually answers correctly against TGMS's own oracle on this exact
fixture is `external/neo4j-recompute/README.md`'s "Shape test" section
(run on xzgpu, since no Neo4j install exists on the laptop).
"""
import pytest
from neo4j_recompute import queries
from neo4j_recompute.canon import OPEN_END

from .conftest import load_artifact_specs

PARAM_BUILDERS = {
    "entity_history": queries.f1_params,
    "version_history": queries.f2_params,
    "snapshot_subgraph": queries.f3_params,
    "diff_snapshots": queries.f4_params,
    "neighborhood_evolution": queries.f5_params,
    "aggregate_events": queries.f6_params,
    "graph_metric_timeseries": queries.f7_params,
    "burst_detection": queries.f8_params,
    "count_temporal_motifs": queries.f9_params,
    "find_temporal_motif_instances": queries.f10_params,
}

ARTIFACT_SPECS = load_artifact_specs()


@pytest.mark.parametrize("name,op,args", ARTIFACT_SPECS)
def test_registered_scope_is_always_expressible(name, op, args):
    """Every artifact the storm templates register (`tgms/eval/storm.py`'s
    own args, concretized in the fixture) must build params without
    `NotExpressibleError` — the frozen scope is defined to cover exactly
    the registered population (memo §2.3/§2.6's "13/13 families")."""
    if op == "temporal_reachability":
        queries.f11_window_params(args)
    elif op == "temporal_paths":
        queries.f12_params(args)
    elif op == "compute":
        queries.f13_params(args)
    else:
        PARAM_BUILDERS[op](args, args.get("limit", 100))


def test_all_13_families_have_cypher_text():
    assert set(queries.FAMILY_TEXT) == {
        "entity_history", "version_history", "snapshot_subgraph", "diff_snapshots",
        "neighborhood_evolution", "aggregate_events", "graph_metric_timeseries",
        "burst_detection", "count_temporal_motifs", "find_temporal_motif_instances",
        "temporal_reachability", "temporal_paths", "compute",
    }


def test_f1_entity_history_params():
    args = {"uid": "u1", "as_of_tt": OPEN_END, "include_edges": True, "limit": 100}
    assert queries.f1_params(args) == {"uid": "u1", "O": OPEN_END, "lim": 100}


def test_f1_rejects_pinned_as_of_tt():
    args = {"uid": "u1", "as_of_tt": 123, "include_edges": True, "limit": 100}
    with pytest.raises(queries.NotExpressibleError):
        queries.f1_params(args)


def test_f1_rejects_include_edges_false():
    args = {"uid": "u1", "as_of_tt": OPEN_END, "include_edges": False, "limit": 100}
    with pytest.raises(queries.NotExpressibleError):
        queries.f1_params(args)


def test_f2_rejects_edge_kind():
    args = {"kind": "edge", "window": {"t_a": 0, "t_b": 10}, "belief": "current"}
    with pytest.raises(queries.NotExpressibleError):
        queries.f2_params(args)


def test_f2_rejects_non_current_belief():
    args = {"kind": "node", "window": {"t_a": 0, "t_b": 10}, "belief": "superseded"}
    with pytest.raises(queries.NotExpressibleError):
        queries.f2_params(args)


def test_f3_rejects_multiple_seeds():
    args = {"seeds": ["a", "b"], "hops": 1, "t_valid": 5}
    with pytest.raises(queries.NotExpressibleError):
        queries.f3_params(args)


def test_f3_rejects_hops_other_than_1():
    args = {"seeds": ["a"], "hops": 2, "t_valid": 5}
    with pytest.raises(queries.NotExpressibleError):
        queries.f3_params(args)


def test_f3_params_shape():
    args = {"seeds": ["u1"], "hops": 1, "t_valid": 160, "limit": 50}
    assert queries.f3_params(args) == {"seed": "u1", "t": 160, "O": OPEN_END, "lim": 50}


def test_f4_rejects_scoped_diff():
    args = {"t1": 0, "t2": 10, "scope": {"seeds": ["a"], "hops": 1}}
    with pytest.raises(queries.NotExpressibleError):
        queries.f4_params(args)


def test_f6_rejects_non_frozen_aggregate():
    args = {"group_by": [{"dim": "endpoint", "role": "dst"}],
            "aggregates": [{"agg": "count"}], "window": {"t_a": 0, "t_b": 10}}
    with pytest.raises(queries.NotExpressibleError):
        queries.f6_params(args)


def test_f8_rejects_edge_event_rate_target():
    args = {"target": {"kind": "edge_event_rate", "rel_type": None},
            "window": {"t_a": 0, "t_b": 10}, "stride": 1}
    with pytest.raises(queries.NotExpressibleError):
        queries.f8_params(args)


def test_f9_rejects_node_filter():
    args = {"motif": "M_2node_pingpong", "window": {"t_a": 0, "t_b": 10},
            "delta": 5, "node_filter": ["a"]}
    with pytest.raises(queries.NotExpressibleError):
        queries.f9_params(args)


def test_f11_rejects_delta_max_wait():
    args = {"src": "u1", "window": {"t_a": 0, "t_b": 10}, "delta_max_wait": 5}
    with pytest.raises(queries.NotExpressibleError):
        queries.f11_window_params(args)


def test_f11_rejects_direction_other_than_out():
    args = {"src": "u1", "window": {"t_a": 0, "t_b": 10}, "direction": "in"}
    with pytest.raises(queries.NotExpressibleError):
        queries.f11_window_params(args)


def test_f13_rejects_non_literal_control():
    with pytest.raises(queries.NotExpressibleError):
        queries.f13_params({"fn": "sum", "input": [{"x": 1}]})


def test_f13_params_is_empty_for_the_registered_control():
    assert queries.f13_params({"fn": "count", "input": [{"x": 1}, {"x": 2}]}) == {}


class TestF12BranchGeneration:
    """F12's four hop-pruned branches are generated programmatically
    (`f12_branch`/`f12_query`) rather than hand-duplicated; these pin their
    shape so a future edit can't silently break one hop length only."""

    @pytest.mark.parametrize("hops", [1, 2, 3, 4])
    def test_branch_returns_four_uniform_columns(self, hops):
        text = queries.f12_branch(hops)
        assert "AS arrival" in text
        assert f"{hops} AS hops" in text
        assert "AS key" in text
        assert "AS edges" in text

    def test_branch_one_hop_has_no_interior_node(self):
        text = queries.f12_branch(1)
        assert "n1.uid<>$dst" not in text  # nothing excludes an interior node...
        assert "(nL:E {uid:$dst})" in text  # ...because the only hop lands on dst

    @pytest.mark.parametrize("hops", [2, 3, 4])
    def test_branch_excludes_interior_nodes_from_src_and_dst(self, hops):
        text = queries.f12_branch(hops)
        for i in range(1, hops):
            assert f"n{i}.uid<>$dst" in text
            assert f"n{i}.uid<>$src" in text

    def test_branch_rejects_zero_hops(self):
        with pytest.raises(ValueError):
            queries.f12_branch(0)

    def test_query_unions_exactly_max_hops_branches(self):
        text = queries.f12_query(4)
        assert text.count("UNION ALL") == 3  # 4 branches -> 3 separators
        assert "LIMIT" not in text  # pagination is rows[0..$k], not LIMIT
        assert "$k" in text

    def test_f12_params_passes_through_k_and_max_hops(self):
        args = {"src": "u1", "dst": "u3", "window": {"t_a": 0, "t_b": 1000},
                "k": 2, "max_hops": 4}
        params, max_hops = queries.f12_params(args)
        assert params == {"src": "u1", "dst": "u3", "t_a": 0, "t_b": 1000,
                          "k": 2, "O": OPEN_END}
        assert max_hops == 4

    def test_f12_rejects_pinned_as_of_tt(self):
        args = {"src": "u1", "dst": "u3", "window": {"t_a": 0, "t_b": 1000},
                "as_of_tt": 5}
        with pytest.raises(queries.NotExpressibleError):
            queries.f12_params(args)
