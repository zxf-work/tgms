"""`format.py` turns a raw Neo4j record into TGMS's own payload shape:
parse `props` JSON-string columns, derive `truncated`/`cursor` the way
`tgms.temporal.algebra.paginate` would for every storm-registered artifact
(always called with `cursor=None`, i.e. offset 0), and re-round every float
with Python's own (half-even) `round()` rather than trust whatever Neo4j's
`round()` returned. No Neo4j needed — these are pure functions over
already-fetched record dicts.
"""
from neo4j_recompute import format as fmt


def test_paginate_fields_not_truncated_when_under_limit():
    truncated, cursor = fmt._paginate_fields(rows_total=3, limit=100)
    assert truncated is False
    assert cursor is None


def test_paginate_fields_truncated_sets_cursor_to_limit_offset():
    # TGMS's own paginate(): offset=0, so a truncated page's cursor is
    # str(offset + len(window)) == str(limit)
    truncated, cursor = fmt._paginate_fields(rows_total=250, limit=100)
    assert truncated is True
    assert cursor == "100"


def test_paginate_fields_exactly_at_limit_is_not_truncated():
    truncated, cursor = fmt._paginate_fields(rows_total=100, limit=100)
    assert truncated is False
    assert cursor is None


def test_props_parses_json_string():
    assert fmt._props('{"a":1,"b":2}') == {"a": 1, "b": 2}


def test_props_passes_through_already_parsed_values():
    assert fmt._props({"a": 1}) == {"a": 1}
    assert fmt._props(None) is None


def test_parse_version_row_parses_props_in_place():
    row = {"vid": "v1", "uid": "u1", "props": '{"w":1}'}
    out = fmt._parse_version_row(row)
    assert out["props"] == {"w": 1}
    assert row["props"] == '{"w":1}'  # original dict untouched


def test_format_entity_history_shape():
    record = {
        "rows": [{"vid": "v1", "uid": "u1", "label": "Person", "vt_s": 0,
                  "vt_e": 10, "tt_s": 0, "tt_e": 0, "props": '{"name":"u1"}',
                  "source": "ingest", "provenance_ref": None}],
        "rows_total": 1,
        "edges": [], "edges_truncated": False,
    }
    payload = fmt.format_entity_history(record, limit=100)
    assert payload["rows"][0]["props"] == {"name": "u1"}
    assert payload["truncated"] is False
    assert payload["cursor"] is None
    assert payload["rows_total"] == 1


def test_format_entity_history_truncated_cursor():
    record = {"rows": [{"props": "{}"}] * 5, "rows_total": 12,
              "edges": [], "edges_truncated": False}
    payload = fmt.format_entity_history(record, limit=5)
    assert payload["truncated"] is True
    assert payload["cursor"] == "5"


def test_format_snapshot_subgraph_truncated_ors_nodes_and_rows():
    record = {"rows": [{"a": 1}], "rows_total": 1, "nodes": [{"uid": "x"}] * 3,
              "nodes_total": 10}
    payload = fmt.format_snapshot_subgraph(record, limit=5)
    assert payload["truncated"] is True  # nodes_total (10) > limit (5)
    assert payload["cursor"] is None  # edges/rows were not truncated (1 <= 5)


def test_format_diff_snapshots_parses_props_in_changed_rows():
    record = {
        "nodes_added": ["a"], "nodes_added_total": 1,
        "nodes_removed": [], "nodes_removed_total": 0,
        "edges_added": [], "edges_added_total": 0,
        "edges_removed": [], "edges_removed_total": 0,
        "props_changed": [{"kind": "node", "id": "u1",
                           "from": {"label": "Person", "props": '{"a":1}'},
                           "to": {"label": "Person", "props": '{"a":2}'}}],
        "props_changed_total": 1,
    }
    payload = fmt.format_diff_snapshots(record, limit=100)
    assert payload["props_changed"][0]["from"]["props"] == {"a": 1}
    assert payload["props_changed"][0]["to"]["props"] == {"a": 2}
    assert payload["truncated"] is False


def test_format_diff_snapshots_truncated_when_any_total_exceeds_limit():
    record = {
        "nodes_added": ["a"] * 3, "nodes_added_total": 30,
        "nodes_removed": [], "nodes_removed_total": 0,
        "edges_added": [], "edges_added_total": 0,
        "edges_removed": [], "edges_removed_total": 0,
        "props_changed": [], "props_changed_total": 0,
    }
    payload = fmt.format_diff_snapshots(record, limit=10)
    assert payload["truncated"] is True


def test_format_count_temporal_motifs_is_never_truncated():
    payload = fmt.format_count_temporal_motifs(
        {"count": 5, "n_events_in_window": 12}, limit=100)
    assert payload == {"count": 5, "n_events_in_window": 12, "truncated": False}


def test_format_compute_constant_control():
    payload = fmt.format_compute({"value": 2})
    assert payload == {"value": 2, "truncated": False}


def test_format_burst_detection_rerounds_score_half_even():
    # a Cypher-side round() might return something that needs re-rounding;
    # canonicalize_floats must apply Python's own round(), not trust the
    # value as final
    record = {"rows": [{"t_a": 0, "t_b": 10, "value": 5.0,
                        "score": 3.123456789499}],
              "rows_total": 1, "n_buckets": 20}
    payload = fmt.format_burst_detection(record, limit=100)
    assert payload["rows"][0]["score"] == round(3.123456789499, 9)


def test_format_temporal_reachability_applies_limit_and_pagination():
    rows = [{"uid": f"n{i}", "earliest_arrival": i} for i in range(5)]
    payload = fmt.format_temporal_reachability(rows, limit=3)
    assert payload["rows_total"] == 5
    assert len(payload["rows"]) == 3
    assert payload["truncated"] is True
    assert payload["cursor"] == "3"


def test_format_temporal_paths_cursor_always_none():
    record = {"rows": [{"arrival": 1, "hops": 1, "edges": []}],
              "rows_total": 1, "truncated": False}
    payload = fmt.format_temporal_paths(record, k=2)
    assert payload["cursor"] is None
