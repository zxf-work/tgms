"""LDBC SNB `composite-merged-fk` → TGMS event-log loader (E13).

**This is not an LDBC Benchmark, this is not an implementation of an LDBC
Benchmark, and nothing this module produces is an LDBC Benchmark Result.**
LDBC material is used under CC-BY 4.0.

The mapping is frozen, rule by rule, in
`docs/design/PAPER_A_EVIDENCE_FREEZE.md` §A3-A9 (as amended by its §E addendum
2). Nothing here is a judgement made at implementation time: where a rule had a
choice, the freeze records the alternative that was rejected and why. The three
rules a reader is most likely to think are bugs:

* **`uid = id * 8 + hierarchy_tag`** (§A4 as amended). LDBC guarantees id
  disjointness only *within* an inheritance hierarchy — `data.tex:444`, "all
  subclasses use the same id space, e.g. there cannot be a Comment and a Post
  with id 1 at the same time" — and says nothing across hierarchies, where
  collisions are certain (Place 0, Organisation 0, Tag 0 and Forum 0 all exist
  at SF1). The first frozen encoding used a 1e13 stride per hierarchy and was
  **wrong**: it sized the bands by entity *count*, and LDBC ids are sparse, so
  SF1's 10,295 Persons carry ids up to 37,383,395,354,990. The interleave is
  collision-free at any magnitude, so it retires the assumption class rather
  than re-tuning its constant. It stays order-preserving within a hierarchy
  (strictly monotone in `id`), which is what `ORDER BY id` and §2.7's
  `cast(uid, int)` need — the latter is row-determining on five of the 21 plans
  and a sort key under a `Limit` on two.
* **`KNOWS` is written twice** (§A3 M7), because `tgms/tgir/eval/pattern.py`
  binds `src`/`dst` positionally and never consults `directed`, while Cypher's
  `(a)-[:KNOWS]-(b)` is undirected. +173,014 edge versions at SF1, +1.0%.
* **valid time never closes** (§A3 M8). The initial snapshot only; SNB's deletes
  are destructive and cascading with no tombstone, and `deletionDate` is
  serialized only in `raw` mode, "not intended for use with any LDBC workload".

Two clocks, and only one of them is LDBC's: `vt_s` is the mapping below, and
transaction time is whatever the store stamps at write. Independently built
stores of the same data legitimately differ in `tt` and in every derived id
(D-023), so a store is reproduced by **replay of the log this loader writes**,
never by a second ingest.

    uv run python scripts/build_snb_store.py --csv <initial_snapshot> --out stores/snb-sf1
"""

from __future__ import annotations

import calendar
import gzip
from pathlib import Path
from typing import Any, Iterator

Op = dict[str, Any]

#: Valid time is open for every version: the initial snapshot asserts what is
#: true from the entity's creation onward, and nothing in it ever ends (M8).
OPEN_END = 2 ** 62

# --------------------------------------------------------------------------
# §A4 (amended) — identity
# --------------------------------------------------------------------------

#: The seven LDBC id spaces, tagged in **frozen alphabetical order**. Eight
#: slots for seven hierarchies: the spare keeps the arithmetic a shift.
HIERARCHY_TAG: dict[str, int] = {
    "Forum": 0,
    "Message": 1,
    "Organisation": 2,
    "Person": 3,
    "Place": 4,
    "Tag": 5,
    "TagClass": 6,
}
HIERARCHY_STRIDE = 8

#: Concrete label -> the id space it draws from. `Post` and `Comment` share one
#: space (verified at SF1: 1,121,226 distinct Post ids, 1,739,438 distinct
#: Comment ids, **intersection 0**, union 2,860,664), which is why IS2/IS6/IS7
#: may treat them as one `Message` population.
LABEL_HIERARCHY: dict[str, str] = {
    "Post": "Message", "Comment": "Message",
    "City": "Place", "Country": "Place", "Continent": "Place",
    "University": "Organisation", "Company": "Organisation",
    "Person": "Person", "Forum": "Forum", "Tag": "Tag", "TagClass": "TagClass",
}


def snb_uid(hierarchy: str, ldbc_id: str | int) -> str:
    """**The one definition site.** The parameter binder calls this too — an
    id-valued parameter compared against an unencoded uid matches nothing, and
    matches nothing *silently*, which is the failure this function exists to
    make impossible to write twice."""
    return str(int(ldbc_id) * HIERARCHY_STRIDE + HIERARCHY_TAG[hierarchy])


def uid_to_ldbc_id(uid: str | int) -> int:
    """Inverse, for the report and for debugging a row by eye."""
    return int(uid) // HIERARCHY_STRIDE


# --------------------------------------------------------------------------
# time
# --------------------------------------------------------------------------

_EPOCH_DAY: dict[str, int] = {}


def parse_ts(s: str) -> int:
    """`2010-01-03T15:10:41.499+00:00` -> microseconds since the Unix epoch.

    Hand-parsed rather than `fromisoformat`'d because this runs 20.4 million
    times; the day prefix is cached, which is where the win is (SF1 spans ~2,300
    distinct days against 20.4M rows).

    The `+00:00` offset is **asserted, not assumed**: a non-UTC offset would
    shift `vt_s` silently, and every temporal predicate in the 21 plans reads
    `vt_s`.
    """
    # The whole shape is checked, not just the offset. An earlier version tested
    # only `s[19] == "."` and the `+00:00` tail, which accepted
    # `2010-01-03 15:10:41.499+00:00` — a space for the `T` — and parsed it
    # silently, because the day prefix and every field offset still line up.
    if (len(s) != 29 or s[4] != "-" or s[7] != "-" or s[10] != "T"
            or s[13] != ":" or s[16] != ":" or s[19] != "." or s[23:] != "+00:00"):
        raise ValueError(
            f"unexpected LDBC timestamp shape {s!r}: the loader is frozen "
            f"against 'YYYY-MM-DDTHH:MM:SS.mmm+00:00' and will not guess"
        )
    day = s[:10]
    base = _EPOCH_DAY.get(day)
    if base is None:
        base = calendar.timegm((int(day[0:4]), int(day[5:7]), int(day[8:10]),
                                0, 0, 0, 0, 0, 0))
        _EPOCH_DAY[day] = base
    secs = base + int(s[11:13]) * 3600 + int(s[14:16]) * 60 + int(s[17:19])
    return secs * 1_000_000 + int(s[20:23]) * 1000


def parse_date(s: str) -> int:
    """`1984-03-11` -> microseconds at midnight UTC. Used for `birthday`, which
    M4's sibling rule keeps an ordinary property rather than a clock."""
    return calendar.timegm((int(s[0:4]), int(s[5:7]), int(s[8:10]),
                            0, 0, 0, 0, 0, 0)) * 1_000_000


# --------------------------------------------------------------------------
# the schema, as the serialization actually ships it
# --------------------------------------------------------------------------
# Column indices are 0-based into the `|`-split row. They are pinned here
# rather than resolved from the header so that a serialization change is a
# loud failure (`_check_header`) instead of a silent re-binding.

class Node:
    """A node file: the label, the id, when it starts, what it carries, and the
    foreign keys merged into it."""

    def __init__(self, name: str, group: str, label: str | int, hierarchy: str,
                 id_col: int, created_col: int | None, header: str,
                 props: dict[str, tuple[int, str]],
                 fks: tuple[tuple[str, int, str, bool, bool], ...] = ()):
        self.name, self.group, self.label, self.hierarchy = name, group, label, hierarchy
        self.id_col, self.created_col, self.header = id_col, created_col, header
        self.props, self.fks = props, fks


#: `props`: name -> (column, kind) with kind in {str, int, ts, date}.
#: `fks`: (rel_type, column, target hierarchy, nullable, reversed).
#:   `reversed` puts the *referenced* entity on the src side — only
#:   `CONTAINER_OF` needs it (the edge is Forum->Post; the FK lives on Post).
#: Every FK edge takes the **creation time of the row carrying the FK** (§A3 M3
#: as sharpened by §E addendum 2: the moment the relationship is recorded). For
#: `CONTAINER_OF` that is the Post's own creationDate, so a containment edge
#: cannot predate its post, and no side table is needed — the loader streams.

NODES: tuple[Node, ...] = (
    # --- static: no creationDate anywhere, so vt_s = 0 (M2). Zero is the only
    #     value that cannot exclude a static entity from a query window.
    Node("Place", "static", 3, "Place", 0, None,
         "id|name|url|type|PartOfPlaceId",
         {"id": (0, "int"), "name": (1, "str"), "url": (2, "str"),
          "type": (3, "str")},
         (("IS_PART_OF", 4, "Place", True, False),)),
    Node("Organisation", "static", 1, "Organisation", 0, None,
         "id|type|name|url|LocationPlaceId",
         {"id": (0, "int"), "type": (1, "str"), "name": (2, "str"),
          "url": (3, "str")},
         (("IS_LOCATED_IN", 4, "Place", False, False),)),
    Node("TagClass", "static", "TagClass", "TagClass", 0, None,
         "id|name|url|SubclassOfTagClassId",
         {"id": (0, "int"), "name": (1, "str"), "url": (2, "str")},
         (("IS_SUBCLASS_OF", 3, "TagClass", True, False),)),
    Node("Tag", "static", "Tag", "Tag", 0, None,
         "id|name|url|TypeTagClassId",
         {"id": (0, "int"), "name": (1, "str"), "url": (2, "str")},
         (("HAS_TYPE", 3, "TagClass", False, False),)),
    # --- dynamic
    Node("Person", "dynamic", "Person", "Person", 1, 0,
         "creationDate|id|firstName|lastName|gender|birthday|locationIP|"
         "browserUsed|LocationCityId|language|email",
         {"creationDate": (0, "ts"), "id": (1, "int"), "firstName": (2, "str"),
          "lastName": (3, "str"), "gender": (4, "str"),
          "birthday": (5, "date"), "locationIP": (6, "str"),
          "browserUsed": (7, "str"), "language": (9, "str"),
          "email": (10, "str")},
         (("IS_LOCATED_IN", 8, "Place", False, False),)),
    Node("Forum", "dynamic", "Forum", "Forum", 1, 0,
         "creationDate|id|title|ModeratorPersonId",
         {"creationDate": (0, "ts"), "id": (1, "int"), "title": (2, "str")},
         (("HAS_MODERATOR", 3, "Person", False, False),)),
    Node("Post", "dynamic", "Post", "Message", 1, 0,
         "creationDate|id|imageFile|locationIP|browserUsed|language|content|"
         "length|CreatorPersonId|ContainerForumId|LocationCountryId",
         {"creationDate": (0, "ts"), "id": (1, "int"), "imageFile": (2, "str"),
          "locationIP": (3, "str"), "browserUsed": (4, "str"),
          "language": (5, "str"), "content": (6, "str"), "length": (7, "int")},
         (("HAS_CREATOR", 8, "Person", False, False),
          ("CONTAINER_OF", 9, "Forum", False, True),
          ("IS_LOCATED_IN", 10, "Place", False, False))),
    Node("Comment", "dynamic", "Comment", "Message", 1, 0,
         "creationDate|id|locationIP|browserUsed|content|length|"
         "CreatorPersonId|LocationCountryId|ParentPostId|ParentCommentId",
         {"creationDate": (0, "ts"), "id": (1, "int"), "locationIP": (2, "str"),
          "browserUsed": (3, "str"), "content": (4, "str"),
          "length": (5, "int")},
         (("HAS_CREATOR", 6, "Person", False, False),
          ("IS_LOCATED_IN", 7, "Place", False, False),
          # a Comment replies to exactly one parent, and which column carries
          # it says whether the parent is a Post or a Comment. Both are the
          # Message space, so both encode identically.
          ("REPLY_OF", 8, "Message", True, False),
          ("REPLY_OF", 9, "Message", True, False))),
)


class Edge:
    """A standalone edge file. Every one of these carries its **own**
    `creationDate` (§A3 M1 as restated in §E addendum 2) — the serialization
    timestamps four relations the schema table does not list as timestamped
    (`HAS_TAG`, `HAS_INTEREST`, `STUDY_AT`, `WORK_AT`), and reading the data's
    own timestamp is strictly more faithful than inheriting one."""

    def __init__(self, name: str, rel_type: str, header: str,
                 src: tuple[int, str], dst: tuple[int, str],
                 props: dict[str, tuple[int, str]] | None = None,
                 both_ways: bool = False):
        self.name, self.rel_type, self.header = name, rel_type, header
        self.src, self.dst = src, dst
        self.props = props or {}
        self.both_ways = both_ways


#: Alphabetical by directory name — that **is** the frozen order (§A8).
EDGES: tuple[Edge, ...] = (
    Edge("Comment_hasTag_Tag", "HAS_TAG", "creationDate|CommentId|TagId",
         (1, "Message"), (2, "Tag")),
    Edge("Forum_hasMember_Person", "HAS_MEMBER", "creationDate|ForumId|PersonId",
         (1, "Forum"), (2, "Person")),
    Edge("Forum_hasTag_Tag", "HAS_TAG", "creationDate|ForumId|TagId",
         (1, "Forum"), (2, "Tag")),
    Edge("Person_hasInterest_Tag", "HAS_INTEREST",
         "creationDate|personId|interestId", (1, "Person"), (2, "Tag")),
    # M7: one CSV row, two edge versions. Cypher's KNOWS is undirected and the
    # pattern evaluator does not consult `directed`.
    Edge("Person_knows_Person", "KNOWS", "creationDate|Person1Id|Person2Id",
         (1, "Person"), (2, "Person"), both_ways=True),
    Edge("Person_likes_Comment", "LIKES", "creationDate|PersonId|CommentId",
         (1, "Person"), (2, "Message")),
    Edge("Person_likes_Post", "LIKES", "creationDate|PersonId|PostId",
         (1, "Person"), (2, "Message")),
    # classYear / workFrom are ordinary integer properties, never valid time
    # (§A3 M4): BI20 reads `classYear` as a path weight, IC11 reads
    # `workFrom` as a scalar predicate.
    Edge("Person_studyAt_University", "STUDY_AT",
         "creationDate|PersonId|UniversityId|classYear",
         (1, "Person"), (2, "Organisation"), {"classYear": (3, "int")}),
    Edge("Person_workAt_Company", "WORK_AT",
         "creationDate|PersonId|CompanyId|workFrom",
         (1, "Person"), (2, "Organisation"), {"workFrom": (3, "int")}),
    Edge("Post_hasTag_Tag", "HAS_TAG", "creationDate|PostId|TagId",
         (1, "Message"), (2, "Tag")),
)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _parts(root: Path, group: str, name: str) -> list[Path]:
    """Spark part-files, in a **sorted** order so the event log is
    byte-reproducible across runs and machines (D-023)."""
    d = root / group / name
    if not d.is_dir():
        raise FileNotFoundError(f"missing SNB directory: {d}")
    return sorted(d.glob("part-*.csv.gz"))


def _check_header(got: str, want: str, where: Path) -> None:
    if got != want:
        raise ValueError(
            f"{where}: header changed.\n  expected {want!r}\n  got      {got!r}\n"
            f"The column indices in snb_loader.py are pinned to the expected "
            f"header; re-binding them silently would move every value one "
            f"column over."
        )


def _rows(paths: list[Path], header: str) -> Iterator[list[str]]:
    """Every part carries its own header line; blank parts are legal."""
    for p in paths:
        with gzip.open(p, "rt", encoding="utf-8", newline="") as f:
            first = f.readline()
            if not first:
                continue                       # an empty part-file
            _check_header(first.rstrip("\n"), header, p)
            for line in f:
                line = line.rstrip("\n")
                if line:
                    yield line.split("|")


def _coerce(raw: str, kind: str) -> Any:
    if kind == "int":
        return int(raw)
    if kind == "ts":
        return parse_ts(raw)
    if kind == "date":
        return parse_date(raw)
    return raw


def _props(row: list[str], spec: dict[str, tuple[int, str]]) -> dict[str, Any]:
    """M10: every remaining column becomes a property under its LDBC name.
    Text is carried verbatim, never truncated — BI12 reads `length`, and a
    truncated corpus would understate the store SF1 exists to produce."""
    out: dict[str, Any] = {}
    for name, (col, kind) in spec.items():
        raw = row[col]
        if raw != "":
            out[name] = _coerce(raw, kind)
    return out


# --------------------------------------------------------------------------
# the two passes
# --------------------------------------------------------------------------

def node_ops(root: Path) -> Iterator[Op]:
    """Pass 1 — every node assertion, in the frozen file order (§A8)."""
    for spec in NODES:
        paths = _parts(root, spec.group, spec.name)
        for row in _rows(paths, spec.header):
            label = row[spec.label] if isinstance(spec.label, int) else spec.label
            vt_s = 0 if spec.created_col is None else parse_ts(row[spec.created_col])
            yield {"op": "assert_node",
                   "uid": snb_uid(spec.hierarchy, row[spec.id_col]),
                   "label": label,
                   "props": _props(row, spec.props),
                   "vt_s": vt_s, "vt_e": OPEN_END,
                   "source": "ldbc-snb-sf1", "provenance_ref": None}


def edge_ops(root: Path) -> Iterator[Op]:
    """Pass 2 — the FK-merged edges in the node files' frozen order, then the
    standalone edge files alphabetically.

    Nodes are asserted before any edge (rather than interleaving each node with
    its own foreign keys) so that no edge in the log names an identity the log
    has not yet introduced. The store tolerates the other order; the log reads
    better in this one, and A8's order is honoured within each pass.
    """
    for spec in NODES:
        if not spec.fks:
            continue
        paths = _parts(root, spec.group, spec.name)
        for row in _rows(paths, spec.header):
            vt_s = 0 if spec.created_col is None else parse_ts(row[spec.created_col])
            own = snb_uid(spec.hierarchy, row[spec.id_col])
            for rel, col, target, nullable, reversed_ in spec.fks:
                raw = row[col]
                if raw == "":
                    if not nullable:
                        raise ValueError(
                            f"{spec.name}: {rel} foreign key in column {col} is "
                            f"empty on a row the schema declares mandatory"
                        )
                    continue
                other = snb_uid(target, raw)
                src, dst = (other, own) if reversed_ else (own, other)
                yield {"op": "assert_edge", "src": src, "dst": dst,
                       "rel_type": rel, "props": {},
                       "vt_s": vt_s, "vt_e": OPEN_END, "disc": "",
                       "source": "ldbc-snb-sf1", "provenance_ref": None}

    for espec in EDGES:
        paths = _parts(root, "dynamic", espec.name)
        scol, shier = espec.src
        dcol, dhier = espec.dst
        for row in _rows(paths, espec.header):
            vt_s = parse_ts(row[0])
            a = snb_uid(shier, row[scol])
            b = snb_uid(dhier, row[dcol])
            props = _props(row, espec.props)
            yield {"op": "assert_edge", "src": a, "dst": b,
                   "rel_type": espec.rel_type, "props": props,
                   "vt_s": vt_s, "vt_e": OPEN_END, "disc": "",
                   "source": "ldbc-snb-sf1", "provenance_ref": None}
            if espec.both_ways:
                yield {"op": "assert_edge", "src": b, "dst": a,
                       "rel_type": espec.rel_type, "props": props,
                       "vt_s": vt_s, "vt_e": OPEN_END, "disc": "",
                       "source": "ldbc-snb-sf1", "provenance_ref": None}


def all_ops(root: Path) -> Iterator[Op]:
    yield from node_ops(root)
    yield from edge_ops(root)


# --------------------------------------------------------------------------
# expected counts — the fidelity gate's other half
# --------------------------------------------------------------------------

#: LDBC's published SF1 figures
#: (`ldbc_snb_docs` `tables/table-number-of-entities-bi-initial.tex`),
#: independently confirmed against the CSVs before the build. `KNOWS` is the
#: published 173,014 **doubled** by M7 — the single declared exception, and the
#: reason the edge total exceeds the published 17,196,776.
SF1_NODES: dict[str, int] = {
    "Comment": 1_739_438, "Post": 1_121_226, "Forum": 100_827, "Tag": 16_080,
    "Person": 10_295, "Organisation": 7_955, "Place": 1_460, "TagClass": 71,
}
SF1_EDGES: dict[str, int] = {
    "HAS_TAG": 3_256_648, "HAS_MEMBER": 2_909_768, "IS_LOCATED_IN": 2_878_914,
    "HAS_CREATOR": 2_860_664, "LIKES": 1_870_268, "REPLY_OF": 1_739_438,
    "CONTAINER_OF": 1_121_226, "KNOWS": 346_028, "HAS_INTEREST": 238_052,
    "HAS_MODERATOR": 100_827, "WORK_AT": 22_044, "HAS_TYPE": 16_080,
    "STUDY_AT": 8_309, "IS_PART_OF": 1_454, "IS_SUBCLASS_OF": 70,
}
SF1_PUBLISHED_EDGE_TOTAL = 17_196_776      # before M7
SF1_KNOWS_PUBLISHED = 173_014

#: `Organisation` and `Place` are serialized with the "map hierarchy to single
#: table" strategy, so their concrete label is the row's own `type`
#: discriminator rather than the file name. The gate counts by concrete label,
#: which is what M6 asserts into the store, so these roll up.
LABEL_ROLLUP: dict[str, str] = {
    "City": "Place", "Country": "Place", "Continent": "Place",
    "University": "Organisation", "Company": "Organisation",
}


def store_label_counts(store: Any) -> dict[str, int]:
    """Node versions per concrete label, rolled up to the eight LDBC types.

    `stats()` carries `rel_type_counts` but no label histogram, so the node half
    of the gate streams `all_node_versions()` — the abstract adapter API, so the
    count is the same question on either backend.
    """
    counts: dict[str, int] = {}
    for version in store.adapter.all_node_versions():
        label = version.label if hasattr(version, "label") else version["label"]
        label = LABEL_ROLLUP.get(label, label)
        counts[label] = counts.get(label, 0) + 1
    return counts


def fidelity(label_counts: dict[str, int],
             stats: dict[str, Any]) -> tuple[bool, list[str]]:
    """Compare a built store against LDBC's published counts.

    Exact or nothing: a mismatch is a mapping defect or a data surprise, and
    either way it blocks the campaign. Returns `(ok, lines)` with one line per
    row so the report prints the table whether it passed or failed — a gate
    that only speaks when it passes is not a gate.
    """
    lines, ok = [], True
    for label, want in sorted(SF1_NODES.items(), key=lambda kv: -kv[1]):
        got = label_counts.get(label, 0)
        ok &= got == want
        lines.append(f"node {label:<13} want {want:>10,}  got {got:>10,}  "
                     f"{'ok' if got == want else 'MISMATCH'}")
    rel = stats.get("rel_type_counts") or {}
    for name, want in sorted(SF1_EDGES.items(), key=lambda kv: -kv[1]):
        got = rel.get(name, 0)
        ok &= got == want
        lines.append(f"edge {name:<13} want {want:>10,}  got {got:>10,}  "
                     f"{'ok' if got == want else 'MISMATCH'}")
    n_nodes, n_edges = sum(SF1_NODES.values()), sum(SF1_EDGES.values())
    gn, ge = sum(label_counts.values()), sum(rel.values())
    ok &= gn == n_nodes and ge == n_edges
    lines.append(f"TOTAL nodes       want {n_nodes:>10,}  got {gn:>10,}  "
                 f"{'ok' if gn == n_nodes else 'MISMATCH'}")
    lines.append(f"TOTAL edges       want {n_edges:>10,}  got {ge:>10,}  "
                 f"{'ok' if ge == n_edges else 'MISMATCH'}   "
                 f"(published {SF1_PUBLISHED_EDGE_TOTAL:,} + "
                 f"{SF1_KNOWS_PUBLISHED:,} M7 KNOWS)")
    # one version per entity: the snapshot asserts each entity exactly once, so
    # a surplus here means a duplicate id survived the hierarchy encoding
    nv, ne = stats.get("n_node_versions"), stats.get("n_edge_versions")
    ok &= nv == n_nodes and ne == n_edges
    lines.append(f"n_node_versions   want {n_nodes:>10,}  got {nv:>10,}  "
                 f"{'ok' if nv == n_nodes else 'MISMATCH'}")
    lines.append(f"n_edge_versions   want {n_edges:>10,}  got {ne:>10,}  "
                 f"{'ok' if ne == n_edges else 'MISMATCH'}")
    return ok, lines


__all__ = [
    "EDGES", "HIERARCHY_STRIDE", "HIERARCHY_TAG", "LABEL_HIERARCHY", "NODES",
    "OPEN_END", "SF1_EDGES", "SF1_NODES", "all_ops", "edge_ops", "fidelity",
    "node_ops", "parse_date", "parse_ts", "snb_uid", "store_label_counts",
    "uid_to_ldbc_id",
]
