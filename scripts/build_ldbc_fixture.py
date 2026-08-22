"""Build the LDBC-shaped fixture store the 29 unlocked plans execute against.

**The honest-disclosure note this fixture must carry, stated first** (M3 plan
§6.4, §7.3): *there is no LDBC data in this repository*. This is a hand-built
store with LDBC's **shape** — its labels, its relationship types, its
multi-hop topology — at a size a reviewer can read in one screen. It is enough
to establish that a plan **compiles, loads, admits and executes**, and it is
**not** evidence about scale. BI11's triangle is trivial here; on a real SNB
instance it would be the canonical refusal case without the `sources` cohort
pushdown. Any report that lets this fixture's success read as a scale result is
misreporting it.

The independent rows (bo*, cm*) do **not** use this fixture — they run against
the real bitcoin-otc and CollegeMsg stores, which is where M3's only genuine
scale evidence lives.

    uv run python scripts/build_ldbc_fixture.py [--out stores/ldbc-fixture]
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = ROOT / "stores/ldbc-fixture"

DAY = 86_400_000_000
BASE = 1_400_000_000_000_000          # a fixed epoch: the store must be replayable

#: **uids are canonical decimal integers**, because LDBC ids are longs and
#: because §2.7's `cast(uid, int)` tie-break is *row-determining* on five of the
#: 29 rows (IS2, IS3, IC2, IC11, IC12) — it sorts under a `Limit` on two of
#: them. A fixture with "p1"-style uids makes those plans raise `E_ARG` instead
#: of executing, which would misreport a spec feature as a plan defect.
PEOPLE = ["1", "2", "3", "4", "5", "6"]
POSTS = ["101", "102", "103"]
COMMENTS = ["201", "202", "203", "204"]
FORUMS = ["301", "302"]
TAGS = ["401", "402"]


def ops() -> list[tuple[int, list[dict[str, Any]]]]:
    """Every write, as `(tt, batch)`. Deterministic and small."""
    nodes: list[dict[str, Any]] = []

    def node(uid: str, label: str, props: dict[str, Any]) -> dict[str, Any]:
        return {"op": "assert_node", "uid": uid, "label": label, "props": props,
                "vt_s": 0, "vt_e": 2**62, "source": "fixture",
                "provenance_ref": None}

    for i, uid in enumerate(PEOPLE, start=1):
        nodes.append(node(uid, "Person", {
            "id": uid, "firstName": f"First{i}", "lastName": f"Last{i}",
            "birthday": BASE - i * 365 * DAY, "creationDate": BASE + i * DAY,
            "gender": "female" if i % 2 else "male",
            "browserUsed": "chrome", "locationIP": f"10.0.0.{i}",
        }))
    for i, uid in enumerate(POSTS, start=1):
        nodes.append(node(uid, "Post", {
            "id": uid, "content": f"post {i} about things",
            "creationDate": BASE + (10 + i) * DAY, "length": 20 + i,
            "language": "en", "imageFile": "",
        }))
    for i, uid in enumerate(COMMENTS, start=1):
        nodes.append(node(uid, "Comment", {
            "id": uid, "content": f"comment {i}",
            "creationDate": BASE + (20 + i) * DAY, "length": 5 + i,
        }))
    for i, uid in enumerate(FORUMS, start=1):
        nodes.append(node(uid, "Forum", {"id": uid, "title": f"Forum {i}",
                                         "creationDate": BASE + (i - 1) * DAY}))
    for i, uid in enumerate(TAGS, start=1):
        nodes.append(node(uid, "Tag", {"id": uid, "name": f"Tag{i}"}))
    nodes.append(node("501", "TagClass", {"id": "501", "name": "Class1"}))
    nodes.append(node("601", "City", {"id": "601", "name": "Cityville"}))
    nodes.append(node("602", "Country", {"id": "602", "name": "Countryland"}))
    nodes.append(node("701", "University", {"id": "701", "name": "Uni"}))
    nodes.append(node("702", "Company", {"id": "702", "name": "Corp"}))

    def edge(src: str, dst: str, rel: str, props: dict[str, Any] | None = None,
             vt_s: int = 0, disc: str = "") -> dict[str, Any]:
        return {"op": "assert_edge", "src": src, "dst": dst, "rel_type": rel,
                "props": props or {}, "vt_s": vt_s, "vt_e": 2**62, "disc": disc,
                "source": "fixture", "provenance_ref": None}

    edges: list[dict[str, Any]] = []
    # a KNOWS graph with a triangle (p1,p2,p3) and a 3-hop chain out to p6
    knows = [("1", "2"), ("2", "3"), ("1", "3"), ("3", "4"),
             ("4", "5"), ("5", "6")]
    for i, (a, b) in enumerate(knows):
        # **one** edge per friendship, as LDBC stores it: Cypher's
        # `(a)-[:KNOWS]-(b)` is undirected, and the plans read it with
        # `Expand(dir="both")`. Writing both directions would double every
        # friend row and turn a fixture artefact into a plan defect.
        edges.append(edge(a, b, "KNOWS", {"creationDate": BASE + i * DAY},
                          vt_s=BASE + i * DAY))
    # messages: m1 by p1, m2 by p2, m3 by p3; comments reply into a chain
    for message, person in (("101", "1"), ("102", "2"), ("103", "3")):
        edges.append(edge(message, person, "HAS_CREATOR",
                          vt_s=BASE + 10 * DAY))
    for comment, person in (("201", "2"), ("202", "3"), ("203", "1"), ("204", "4")):
        edges.append(edge(comment, person, "HAS_CREATOR", vt_s=BASE + 20 * DAY))
    # a REPLY_OF chain: c1 → m1, c2 → c1, c3 → c2 (three hops to the root Post)
    for child, parent in (("201", "101"), ("202", "201"), ("203", "202"), ("204", "102")):
        edges.append(edge(child, parent, "REPLY_OF", vt_s=BASE + 21 * DAY))
    for message in POSTS:
        edges.append(edge("301" if message != "103" else "302", message,
                          "CONTAINER_OF", vt_s=BASE + 11 * DAY))
    edges.append(edge("301", "1", "HAS_MODERATOR", vt_s=BASE))
    edges.append(edge("302", "3", "HAS_MODERATOR", vt_s=BASE))
    for forum, person in (("301", "1"), ("301", "2"), ("302", "3"), ("302", "4")):
        edges.append(edge(forum, person, "HAS_MEMBER",
                          {"joinDate": BASE + 2 * DAY}, vt_s=BASE + 2 * DAY))
    for message, tag in (("101", "401"), ("102", "401"), ("103", "402"), ("201", "402")):
        edges.append(edge(message, tag, "HAS_TAG", vt_s=BASE + 12 * DAY))
    edges.append(edge("401", "501", "HAS_TYPE", vt_s=BASE))
    edges.append(edge("402", "501", "HAS_TYPE", vt_s=BASE))
    for person in PEOPLE:
        edges.append(edge(person, "601", "IS_LOCATED_IN", vt_s=BASE))
    for message in POSTS + COMMENTS:
        edges.append(edge(message, "602", "IS_LOCATED_IN", vt_s=BASE))
    edges.append(edge("601", "602", "IS_PART_OF", vt_s=BASE))
    for person, message in (("2", "101"), ("3", "101"), ("1", "102"), ("4", "103")):
        edges.append(edge(person, message, "LIKES",
                          {"creationDate": BASE + 15 * DAY}, vt_s=BASE + 15 * DAY))
    for person, tag in (("1", "401"), ("2", "401"), ("3", "402")):
        edges.append(edge(person, tag, "HAS_INTEREST", vt_s=BASE))
    edges.append(edge("1", "701", "STUDY_AT", {"classYear": 2010}, vt_s=BASE))
    edges.append(edge("2", "702", "WORK_AT", {"workFrom": 2015}, vt_s=BASE))
    edges.append(edge("701", "601", "IS_LOCATED_IN", vt_s=BASE))
    edges.append(edge("702", "602", "IS_LOCATED_IN", vt_s=BASE))

    return [(1, nodes), (2, edges)]


def build(out: Path) -> Any:
    import tgms

    if out.exists():
        shutil.rmtree(out)
    store = tgms.open(out)
    for _tt, batch in ops():
        # one Store write per batch: the store stamps its own tt, and the
        # fixture is rebuilt by *replay* from the log it writes
        store._write(batch)          # noqa: SLF001 - the fixture builder is a writer
    return store


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    args = ap.parse_args()
    store = build(Path(args.out))
    stats = store.stats()
    print(f"built {args.out}")
    print(f"  entities        {stats['n_entities']}")
    print(f"  node versions   {stats['n_node_versions']}")
    print(f"  edge versions   {stats['n_edge_versions']}")
    print(f"  rel types       {sorted(stats['rel_type_counts'])}")
    print(f"  digest          {store.digest()[:16]}")
    print("\nNOTE: LDBC-shaped, not LDBC data. Establishes that plans compile, "
          "load, admit and execute — never that they scale.")
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
