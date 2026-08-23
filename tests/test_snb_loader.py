"""The LDBC SNB -> TGMS mapping, rule by rule (E13).

Every test here names the frozen rule it defends
(`docs/design/PAPER_A_EVIDENCE_FREEZE.md` §A3-A9 + §E addendum 2). The mapping
is the part of E13 where judgement can leak into a claim, so a rule that is not
tested is a rule that is only asserted.

Two fixtures, deliberately:

* a **synthetic mini tree**, written in the real on-disk shape (gzipped
  `part-*.csv.gz`, `|`-delimited, one header per part), referentially
  consistent, which is what lets the rules be checked against known answers;
* **two genuine SF1 files** — `static/TagClass` (71 rows) and `static/Place`
  (1,460 rows), the two smallest in the distribution — so the parser is also
  exercised against real bytes rather than against our idea of them. They carry
  the nullable-FK case (one root TagClass, six Continents) and the `type`
  discriminator, and they are LDBC material under CC-BY 4.0.
"""

from __future__ import annotations

import gzip
import shutil
from pathlib import Path

import pytest

from tgms.data.snb_loader import (
    EDGES, HIERARCHY_TAG, LABEL_HIERARCHY, NODES, OPEN_END, SF1_EDGES,
    SF1_NODES, all_ops, edge_ops, fidelity, node_ops, parse_date, parse_ts,
    snb_uid, uid_to_ldbc_id,
)

REAL = Path(__file__).parent / "fixtures" / "snb_sf1_slice"

# --------------------------------------------------------------------------
# a mini SNB tree in the real on-disk shape
# --------------------------------------------------------------------------

TS = "2010-01-03T15:10:41.499+00:00"
TS2 = "2011-07-28T03:54:23.934+00:00"

MINI: dict[tuple[str, str], list[str]] = {
    ("static", "Place"): [
        "0|Vaduz|http://x/Vaduz|City|1",
        "1|Liechtenstein|http://x/Li|Country|2",
        "2|Europe|http://x/Eu|Continent|",          # nullable FK: no parent
    ],
    ("static", "Organisation"): [
        "0|Company|Acme|http://x/Acme|0",
        "1|University|Uni|http://x/Uni|0",
    ],
    ("static", "TagClass"): [
        "0|Thing|http://x/Thing|",                  # nullable FK: the root
        "1|Person|http://x/Person|0",
    ],
    ("static", "Tag"): [
        "0|Karzai|http://x/Karzai|1",
        "1|Hendrix|http://x/Hendrix|1",
    ],
    ("dynamic", "Person"): [
        f"{TS}|10|Ann|Lee|female|1984-03-11|10.0.0.1|Firefox|0|en|a@x",
        f"{TS}|11|Bo|Ng|male|1985-04-12|10.0.0.2|Chrome|0|en;fr|b@x",
    ],
    ("dynamic", "Forum"): [f"{TS}|20|Wall of Ann|10"],
    ("dynamic", "Post"): [
        f"{TS2}|30||10.0.0.1|Firefox|en|hello world|11|10|20|1",
    ],
    ("dynamic", "Comment"): [
        # replies to a Post (col 8 set, col 9 empty)
        f"{TS2}|31|10.0.0.2|Chrome|nice|4|11|1|30|",
        # replies to a Comment (col 8 empty, col 9 set)
        f"{TS2}|32|10.0.0.1|Firefox|thanks|6|10|1||31",
    ],
    ("dynamic", "Comment_hasTag_Tag"): [f"{TS2}|31|0"],
    ("dynamic", "Forum_hasMember_Person"): [f"{TS}|20|11"],
    ("dynamic", "Forum_hasTag_Tag"): [f"{TS}|20|1"],
    ("dynamic", "Person_hasInterest_Tag"): [f"{TS}|10|0"],
    ("dynamic", "Person_knows_Person"): [f"{TS}|10|11"],
    ("dynamic", "Person_likes_Comment"): [f"{TS2}|10|31"],
    ("dynamic", "Person_likes_Post"): [f"{TS2}|11|30"],
    ("dynamic", "Person_studyAt_University"): [f"{TS}|10|1|2004"],
    ("dynamic", "Person_workAt_Company"): [f"{TS}|11|0|2010"],
    ("dynamic", "Post_hasTag_Tag"): [f"{TS2}|30|1"],
}

HEADERS = {(n.group, n.name): n.header for n in NODES}
HEADERS.update({("dynamic", e.name): e.header for e in EDGES})


def _write_part(path: Path, header: str, rows: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        f.write(header + "\n")
        for r in rows:
            f.write(r + "\n")


@pytest.fixture
def mini(tmp_path: Path) -> Path:
    root = tmp_path / "initial_snapshot"
    for (group, name), header in HEADERS.items():
        rows = MINI.get((group, name), [])
        # split across two parts, the second frequently empty — Spark emits
        # empty parts and every part carries its own header
        _write_part(root / group / name / "part-00000-c000.csv.gz", header, rows[:1])
        _write_part(root / group / name / "part-00001-c000.csv.gz", header, rows[1:])
    return root


@pytest.fixture
def real_tree(tmp_path: Path) -> Path:
    """The two genuine SF1 files, with header-only stubs for everything else so
    the loader runs end to end over real bytes."""
    root = tmp_path / "real"
    for (group, name), header in HEADERS.items():
        src = REAL / group / name
        if src.is_dir():
            shutil.copytree(src, root / group / name)
        else:
            _write_part(root / group / name / "part-00000-c000.csv.gz", header, [])
    return root


def _nodes(root: Path) -> list[dict]:
    return list(node_ops(root))


def _edges(root: Path) -> list[dict]:
    return list(edge_ops(root))


# --------------------------------------------------------------------------
# §A4 (amended) — identity
# --------------------------------------------------------------------------

def test_uid_is_collision_free_across_hierarchies_at_any_magnitude():
    """The rule the first frozen encoding got wrong. A 1e13-stride scheme sized
    its bands by entity *count*; SF1 has 10,295 Persons with ids to
    37,383,395,354,990, so Person overran three other bands. The interleave
    cannot collide regardless of magnitude — that is the whole point of it."""
    magnitudes = [0, 1, 7, 16_079, 37_383_395_354_990, 2 ** 59]
    seen: dict[str, tuple[str, int]] = {}
    for hierarchy in HIERARCHY_TAG:
        for ldbc_id in magnitudes:
            uid = snb_uid(hierarchy, ldbc_id)
            assert uid not in seen, (
                f"{hierarchy}:{ldbc_id} collides with {seen.get(uid)}")
            seen[uid] = (hierarchy, ldbc_id)
    # and the id survives the round trip
    for uid, (_h, ldbc_id) in seen.items():
        assert uid_to_ldbc_id(uid) == ldbc_id


def test_uid_preserves_order_within_a_hierarchy():
    """`ORDER BY id` and §2.7's `cast(uid, int)` both depend on this: the cast
    is row-determining on five of the 21 plans and a sort key under a `Limit`
    on two, so an encoding that reordered ids would change answers."""
    for hierarchy in HIERARCHY_TAG:
        ids = [0, 1, 2, 57_459, 962_072_674_331, 2_336_468_744_013]
        uids = [int(snb_uid(hierarchy, i)) for i in ids]
        assert uids == sorted(uids)
        assert all(b > a for a, b in zip(uids, uids[1:]))


def test_post_and_comment_share_one_id_space():
    """LDBC guarantees disjointness within a hierarchy (`data.tex:444`), which
    is why IS2/IS6/IS7 may treat Post and Comment as one Message population —
    and why they must not get separate tags."""
    assert LABEL_HIERARCHY["Post"] == LABEL_HIERARCHY["Comment"] == "Message"
    assert snb_uid("Message", 30) != snb_uid("Message", 31)
    # the four Place labels and the two Organisation labels likewise roll up
    assert {LABEL_HIERARCHY[x] for x in ("City", "Country", "Continent")} == {"Place"}
    assert {LABEL_HIERARCHY[x] for x in ("University", "Company")} == {"Organisation"}


def test_hierarchy_tags_are_frozen_alphabetical():
    assert list(HIERARCHY_TAG) == sorted(HIERARCHY_TAG)
    assert HIERARCHY_TAG == {"Forum": 0, "Message": 1, "Organisation": 2,
                             "Person": 3, "Place": 4, "Tag": 5, "TagClass": 6}


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

def test_parse_ts_is_utc_microseconds():
    assert parse_ts("1970-01-01T00:00:00.000+00:00") == 0
    assert parse_ts("1970-01-01T00:00:00.001+00:00") == 1_000
    assert parse_ts("1970-01-02T00:00:00.000+00:00") == 86_400_000_000
    assert parse_ts("2010-01-03T15:10:41.499+00:00") == 1_262_531_441_499_000


def test_parse_ts_refuses_a_shape_it_was_not_frozen_against():
    """A non-UTC offset would shift `vt_s` silently, and every temporal
    predicate in the 21 plans reads `vt_s`."""
    for bad in ("2010-01-03T15:10:41.499+02:00", "2010-01-03T15:10:41+00:00",
                "2010-01-03 15:10:41.499+00:00"):
        with pytest.raises(ValueError, match="unexpected LDBC timestamp"):
            parse_ts(bad)


def test_parse_date_is_midnight_utc():
    assert parse_date("1970-01-01") == 0
    assert parse_date("1984-03-11") == 447_811_200_000_000


# --------------------------------------------------------------------------
# §A3 M1/M2/M6/M10 — nodes
# --------------------------------------------------------------------------

def test_static_entities_start_at_zero_and_dynamic_ones_at_their_creation(mini):
    by_uid = {op["uid"]: op for op in _nodes(mini)}
    # M2: zero is the only value that cannot exclude a static entity
    assert by_uid[snb_uid("Tag", 0)]["vt_s"] == 0
    assert by_uid[snb_uid("Place", 0)]["vt_s"] == 0
    assert by_uid[snb_uid("TagClass", 0)]["vt_s"] == 0
    assert by_uid[snb_uid("Organisation", 0)]["vt_s"] == 0
    # M1
    assert by_uid[snb_uid("Person", 10)]["vt_s"] == parse_ts(TS)
    assert by_uid[snb_uid("Message", 30)]["vt_s"] == parse_ts(TS2)
    # M8: valid time never closes
    assert all(op["vt_e"] == OPEN_END for op in by_uid.values())


def test_place_and_organisation_take_their_label_from_the_type_column(mini):
    """"Map hierarchy to single table": the concrete class is a discriminator
    column, not the file name. Reading the file name instead would label every
    City a Place and break IC11's country/city distinction."""
    by_uid = {op["uid"]: op for op in _nodes(mini)}
    assert by_uid[snb_uid("Place", 0)]["label"] == "City"
    assert by_uid[snb_uid("Place", 1)]["label"] == "Country"
    assert by_uid[snb_uid("Place", 2)]["label"] == "Continent"
    assert by_uid[snb_uid("Organisation", 0)]["label"] == "Company"
    assert by_uid[snb_uid("Organisation", 1)]["label"] == "University"
    # M6: Post and Comment keep their concrete labels; `Message` is not a label
    labels = {op["label"] for op in _nodes(mini)}
    assert {"Post", "Comment"} <= labels
    assert "Message" not in labels


def test_props_carry_every_remaining_column_verbatim(mini):
    """M10. BI12 reads `length`; a truncated corpus would understate the store
    SF1 exists to produce."""
    by_uid = {op["uid"]: op for op in _nodes(mini)}
    post = by_uid[snb_uid("Message", 30)]["props"]
    assert post["content"] == "hello world"
    assert post["length"] == 11 and isinstance(post["length"], int)
    assert post["id"] == 30 and post["language"] == "en"
    assert post["creationDate"] == parse_ts(TS2)
    assert "imageFile" not in post          # empty columns are simply absent
    person = by_uid[snb_uid("Person", 10)]["props"]
    assert person["firstName"] == "Ann" and person["language"] == "en"
    assert person["birthday"] == parse_date("1984-03-11")


# --------------------------------------------------------------------------
# §A3 M3 as sharpened — the eight FK-merged edges
# --------------------------------------------------------------------------

def test_every_fk_merged_edge_type_is_emitted(mini):
    got = {op["rel_type"] for op in _edges(mini)}
    assert {"HAS_CREATOR", "IS_LOCATED_IN", "REPLY_OF", "CONTAINER_OF",
            "HAS_MODERATOR", "HAS_TYPE", "IS_SUBCLASS_OF", "IS_PART_OF"} <= got


def test_fk_edges_take_the_creation_time_of_the_row_carrying_them(mini):
    """§E addendum 2's uniform principle: the moment the relationship is
    recorded. Streaming, and no side table."""
    edges = _edges(mini)
    creator = [e for e in edges if e["rel_type"] == "HAS_CREATOR"]
    assert all(e["vt_s"] == parse_ts(TS2) for e in creator)     # from Post/Comment
    moderator = [e for e in edges if e["rel_type"] == "HAS_MODERATOR"]
    assert all(e["vt_s"] == parse_ts(TS) for e in moderator)    # from Forum
    static = [e for e in edges
              if e["rel_type"] in ("HAS_TYPE", "IS_SUBCLASS_OF", "IS_PART_OF")]
    assert static and all(e["vt_s"] == 0 for e in static)


def test_container_of_points_forum_to_post_and_cannot_predate_its_post(mini):
    """The one reversed FK: the edge is Forum->Post while the column lives on
    Post. Ruled to take `Post.creationDate` — a containment edge must not
    predate the post it contains."""
    [edge] = [e for e in _edges(mini) if e["rel_type"] == "CONTAINER_OF"]
    assert edge["src"] == snb_uid("Forum", 20)
    assert edge["dst"] == snb_uid("Message", 30)
    assert edge["vt_s"] == parse_ts(TS2)               # the Post's own time
    assert edge["vt_s"] > parse_ts(TS)                 # the Forum is older


def test_reply_of_reads_both_parent_columns(mini):
    """A Comment replies to exactly one parent; which column carries it says
    whether the parent is a Post or a Comment, and both encode into the same
    Message space."""
    replies = {e["src"]: e["dst"] for e in _edges(mini)
               if e["rel_type"] == "REPLY_OF"}
    assert replies[snb_uid("Message", 31)] == snb_uid("Message", 30)   # -> Post
    assert replies[snb_uid("Message", 32)] == snb_uid("Message", 31)   # -> Comment


def test_nullable_foreign_keys_are_skipped_not_invented(mini):
    """The root TagClass and the top Place have no parent. Emitting an edge to
    a sentinel would fabricate topology."""
    edges = _edges(mini)
    assert len([e for e in edges if e["rel_type"] == "IS_SUBCLASS_OF"]) == 1
    assert len([e for e in edges if e["rel_type"] == "IS_PART_OF"]) == 2


def test_a_missing_mandatory_foreign_key_raises(tmp_path):
    """Silence here would drop a HAS_CREATOR edge and quietly shrink the graph;
    the fidelity gate would catch the count, but the cause should be loud."""
    root = tmp_path / "broken"
    for (group, name), header in HEADERS.items():
        rows = MINI.get((group, name), [])
        if name == "Forum":
            rows = [f"{TS}|20|Wall of Ann|"]          # no moderator
        _write_part(root / group / name / "part-00000-c000.csv.gz", header, rows)
    with pytest.raises(ValueError, match="mandatory"):
        list(edge_ops(root))


# --------------------------------------------------------------------------
# §A3 M1/M4/M7 — the ten standalone edge files
# --------------------------------------------------------------------------

def test_standalone_edges_use_their_own_creation_date(mini):
    """The restated M1: the serialization timestamps four relations the schema
    table does not list as timestamped, and the data's own timestamp is
    strictly more faithful than an inherited one."""
    by_rel = {}
    for e in _edges(mini):
        by_rel.setdefault(e["rel_type"], []).append(e)
    assert by_rel["HAS_INTEREST"][0]["vt_s"] == parse_ts(TS)
    assert by_rel["STUDY_AT"][0]["vt_s"] == parse_ts(TS)
    assert all(e["vt_s"] == parse_ts(TS2)
               for e in by_rel["HAS_TAG"] if e["src"] == snb_uid("Message", 30))


def test_knows_is_written_in_both_directions(mini):
    """M7. The pattern evaluator binds src/dst positionally and never consults
    `directed`, while Cypher's KNOWS is undirected."""
    knows = [e for e in _edges(mini) if e["rel_type"] == "KNOWS"]
    assert len(knows) == 2
    a, b = snb_uid("Person", 10), snb_uid("Person", 11)
    assert {(e["src"], e["dst"]) for e in knows} == {(a, b), (b, a)}
    assert all(e["vt_s"] == parse_ts(TS) for e in knows)


def test_no_other_edge_type_is_doubled(mini):
    doubled = [e.rel_type for e in EDGES if e.both_ways]
    assert doubled == ["KNOWS"]


def test_class_year_and_work_from_are_scalars_not_clocks(mini):
    """M4. BI20 reads `classYear` as a path weight and IC11 reads `workFrom` as
    a scalar predicate; promoting either to valid time would break both."""
    [study] = [e for e in _edges(mini) if e["rel_type"] == "STUDY_AT"]
    assert study["props"] == {"classYear": 2004}
    assert study["vt_s"] == parse_ts(TS)          # not 2004
    [work] = [e for e in _edges(mini) if e["rel_type"] == "WORK_AT"]
    assert work["props"] == {"workFrom": 2010}
    assert work["vt_s"] == parse_ts(TS)


def test_likes_and_has_tag_merge_their_several_files(mini):
    """Three HAS_TAG files and two LIKES files collapse to one rel_type each —
    which is what the fidelity table's composed counts assume."""
    rels = [e["rel_type"] for e in _edges(mini)]
    assert rels.count("LIKES") == 2               # one Comment + one Post file
    assert rels.count("HAS_TAG") == 3             # Comment + Forum + Post files


# --------------------------------------------------------------------------
# format and order
# --------------------------------------------------------------------------

def test_a_changed_header_is_loud(tmp_path):
    """Column indices are pinned, so a re-serialization must fail rather than
    move every value one column over."""
    root = tmp_path / "shifted"
    for (group, name), header in HEADERS.items():
        h = header if name != "Person" else "creationDate|id|firstName"
        _write_part(root / group / name / "part-00000-c000.csv.gz", h, [])
    with pytest.raises(ValueError, match="header changed"):
        list(node_ops(root))


def test_nodes_precede_every_edge_and_the_file_order_is_frozen(mini):
    ops = list(all_ops(mini))
    kinds = [op["op"] for op in ops]
    assert set(kinds[:kinds.count("assert_node")]) == {"assert_node"}
    assert set(kinds[kinds.count("assert_node"):]) == {"assert_edge"}
    # §A8: static types, then Person, Forum, Post, Comment
    assert [n.name for n in NODES] == ["Place", "Organisation", "TagClass",
                                       "Tag", "Person", "Forum", "Post",
                                       "Comment"]
    # standalone edge files alphabetically
    assert [e.name for e in EDGES] == sorted(e.name for e in EDGES)


def test_the_stream_is_deterministic(mini):
    assert list(all_ops(mini)) == list(all_ops(mini))


# --------------------------------------------------------------------------
# real bytes
# --------------------------------------------------------------------------

def test_the_real_static_files_parse_to_their_published_counts(real_tree):
    """`static/TagClass` and `static/Place` from the SF1 distribution. Their
    published counts are 71 and 1,460 nodes, 70 IS_SUBCLASS_OF and 1,454
    IS_PART_OF — the six Continents and the one root class are the nullable-FK
    rows measured at source."""
    nodes = _nodes(real_tree)
    labels: dict[str, int] = {}
    for op in nodes:
        labels[op["label"]] = labels.get(op["label"], 0) + 1
    assert labels["TagClass"] == SF1_NODES["TagClass"] == 71
    assert labels["City"] + labels["Country"] + labels["Continent"] == \
        SF1_NODES["Place"] == 1_460

    edges = _edges(real_tree)
    rels: dict[str, int] = {}
    for op in edges:
        rels[op["rel_type"]] = rels.get(op["rel_type"], 0) + 1
    assert rels["IS_SUBCLASS_OF"] == SF1_EDGES["IS_SUBCLASS_OF"] == 70
    assert rels["IS_PART_OF"] == SF1_EDGES["IS_PART_OF"] == 1_454


def test_real_static_rows_are_zero_valid_time_and_open_ended(real_tree):
    assert all(op["vt_s"] == 0 and op["vt_e"] == OPEN_END
               for op in _nodes(real_tree))


# --------------------------------------------------------------------------
# end to end, and the gate itself
# --------------------------------------------------------------------------

def test_the_mini_tree_builds_a_store_and_the_ops_apply(mini, tmp_path):
    import tgms
    store = tgms.open(tmp_path / "mini-store")
    ops = list(all_ops(mini))
    store._write([op for op in ops if op["op"] == "assert_node"])   # noqa: SLF001
    store._write([op for op in ops if op["op"] == "assert_edge"])   # noqa: SLF001
    stats = store.stats()
    assert stats["n_node_versions"] == len(MINI_NODE_ROWS)
    assert stats["n_edge_versions"] == sum(
        1 for op in ops if op["op"] == "assert_edge")
    store.close()


MINI_NODE_ROWS = [r for (g, n), rows in MINI.items() if n in
                  {x.name for x in NODES} for r in rows]


def test_the_gate_reports_every_row_and_fails_on_any_mismatch():
    """A gate that only speaks when it passes is not a gate."""
    good_labels = dict(SF1_NODES)
    good_stats = {"rel_type_counts": dict(SF1_EDGES),
                  "n_node_versions": sum(SF1_NODES.values()),
                  "n_edge_versions": sum(SF1_EDGES.values())}
    ok, lines = fidelity(good_labels, good_stats)
    assert ok
    assert len(lines) == len(SF1_NODES) + len(SF1_EDGES) + 4
    assert all("MISMATCH" not in ln for ln in lines)

    bad = dict(good_labels)
    bad["Person"] -= 1
    ok2, lines2 = fidelity(bad, good_stats)
    assert not ok2
    assert any("MISMATCH" in ln and "Person" in ln for ln in lines2)
    assert len(lines2) == len(lines)          # still reports every row


def test_the_expected_totals_are_ldbcs_published_ones_plus_the_declared_m7():
    """17,196,776 published edges + 173,014 doubled KNOWS; 2,997,352 nodes.
    The doubling is the single declared exception to 'exactly LDBC's counts',
    and it must stay visible in the arithmetic."""
    assert sum(SF1_NODES.values()) == 2_997_352
    assert sum(SF1_EDGES.values()) == 17_196_776 + 173_014
    assert SF1_EDGES["KNOWS"] == 2 * 173_014
