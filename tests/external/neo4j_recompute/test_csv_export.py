"""`csv_export.py` writes the `neo4j-admin database import full` input for
epoch 0 (memo §2.2). Two of these assertions are regression tests for bugs
the live shape test on xzgpu actually hit (see
`external/neo4j-recompute/README.md`'s "Shape test" section): entities were
imported with no queryable `uid` property (every `{uid:$x}` lookup matched
nothing) because the header was bare `:ID(E)`, and every relationship was
typed by its *own* `rel_type` value (`:KNOWS`, `:FOLLOWS`, ...) instead of
the uniform `:EV` the schema and every Cypher text assume, because the
`:TYPE` column echoed `rel_type` instead of the literal string `"EV"`.
"""
import csv

from neo4j_recompute.csv_export import export_csv

from .conftest import FIXTURES


def test_entities_header_names_the_id_column_uid(tmp_path):
    paths = export_csv(FIXTURES / "versions-epoch0.jsonl", tmp_path)
    header = paths["entities_header"].read_text().strip()
    assert header == "uid:ID(E)"


def test_edge_versions_type_column_is_always_literal_ev(tmp_path):
    paths = export_csv(FIXTURES / "versions-epoch0.jsonl", tmp_path)
    header = paths["edge_versions_header"].read_text().strip().split(",")
    assert header[-1] == ":TYPE"
    rel_type_idx = header.index("rel_type")
    with paths["edge_versions"].open(newline="") as f:
        rows = list(csv.reader(f))
    assert rows  # the fixture has edges
    for row in rows:
        assert row[-1] == "EV"
        # rel_type is still carried as an ordinary property, just not as
        # the graph relationship type
        assert row[rel_type_idx] in {"KNOWS", "FOLLOWS"}


def test_entities_file_is_sorted_and_deduplicated(tmp_path):
    paths = export_csv(FIXTURES / "versions-epoch0.jsonl", tmp_path)
    with paths["entities"].open(newline="") as f:
        uids = [row[0] for row in csv.reader(f)]
    assert uids == sorted(set(uids))
    assert uids == ["u1", "u2", "u3", "u4", "u5"]


def test_node_versions_header_uses_vid_as_the_id_space(tmp_path):
    paths = export_csv(FIXTURES / "versions-epoch0.jsonl", tmp_path)
    header = paths["node_versions_header"].read_text().strip()
    assert header.startswith("vid:ID(NV),uid,label,vt_s:long,vt_e:long,"
                             "tt_s:long,tt_e:long,props,source,provenance_ref")


def test_relationship_header_has_start_end_id_space_e(tmp_path):
    paths = export_csv(FIXTURES / "versions-epoch0.jsonl", tmp_path)
    header = paths["edge_versions_header"].read_text().strip()
    assert header.startswith(":START_ID(E),:END_ID(E)")


def test_node_and_edge_version_row_counts_match_fixture(tmp_path):
    paths = export_csv(FIXTURES / "versions-epoch0.jsonl", tmp_path)
    with paths["node_versions"].open(newline="") as f:
        n_nodes = sum(1 for _ in csv.reader(f))
    with paths["edge_versions"].open(newline="") as f:
        n_edges = sum(1 for _ in csv.reader(f))
    # the fixture's versions-epoch0.jsonl has 5 node versions and 7 edge
    # versions (one logical u1->u3 edge was carved into two by a later
    # overlapping assert_edge — see the README's "Shape test" section)
    assert n_nodes == 5
    assert n_edges == 7


def test_props_are_written_as_canonical_json_text(tmp_path):
    paths = export_csv(FIXTURES / "versions-epoch0.jsonl", tmp_path)
    header = paths["node_versions_header"].read_text().strip().split(",")
    props_idx = header.index("props")
    with paths["node_versions"].open(newline="") as f:
        rows = list(csv.reader(f))
    for row in rows:
        # sorted-key, compact-separator JSON text, parseable back
        import json
        assert json.loads(row[props_idx]) == {"name": row[header.index("uid")]}
