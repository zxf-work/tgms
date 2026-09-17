#!/usr/bin/env python3
"""The same-data gate of RUNBOOK.md §4.2, implemented.

The runbook's own sketch there calls
``build_snb_store.py --csv-root ... --dry-run-fidelity``; neither that flag
nor a ``--csv-root`` flag exists on ``scripts/build_snb_store.py`` (its CSV
flag is ``--csv`` and it has no dry-run mode), and
``tgms.data.snb_loader.fidelity()`` reads a *built store*'s label counts,
not a CSV root — and its header table is pinned to the **merged-fk**
serialization, so it cannot read the projected-fk tree at all.  What the
runbook *describes* is implemented here directly against both CSV trees:

  * per node type: row count, and the sha256 of the numerically-sorted
    id list (so two trees agree not merely on cardinality but on identity);
  * per relationship type: row count, where the merged-fk side's count for
    an FK-merged relationship is the number of non-empty values in the
    node file's FK column (that is what "merged" means) and the
    projected-fk side's is the row count of the standalone file.

Any mismatch aborts the campaign (design memo §2).
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

MERGED = Path(sys.argv[1])
PROJECTED = Path(sys.argv[2])
OUT = Path(sys.argv[3])

REQUIRED_NODES = 2_997_352
REQUIRED_EDGES = 17_196_776

# node type -> (group, dir name)
NODE_TYPES = [
    ("Place", "static"), ("Organisation", "static"),
    ("TagClass", "static"), ("Tag", "static"),
    ("Person", "dynamic"), ("Forum", "dynamic"),
    ("Post", "dynamic"), ("Comment", "dynamic"),
]

# The 10 relationships that are standalone files in BOTH serializations.
STANDALONE = [
    ("Comment_hasTag_Tag", "dynamic"),
    ("Forum_hasMember_Person", "dynamic"),
    ("Forum_hasTag_Tag", "dynamic"),
    ("Person_hasInterest_Tag", "dynamic"),
    ("Person_knows_Person", "dynamic"),
    ("Person_likes_Comment", "dynamic"),
    ("Person_likes_Post", "dynamic"),
    ("Person_studyAt_University", "dynamic"),
    ("Person_workAt_Company", "dynamic"),
    ("Post_hasTag_Tag", "dynamic"),
]

# The 13 relationships the merged-fk tree folds into a node file's FK column.
# (relationship file name, group, merged node group/dir, merged FK column name)
FK_MERGED = [
    ("Place_isPartOf_Place", "static", "static", "Place", "PartOfPlaceId"),
    ("Organisation_isLocatedIn_Place", "static", "static", "Organisation",
     "LocationPlaceId"),
    ("TagClass_isSubclassOf_TagClass", "static", "static", "TagClass",
     "SubclassOfTagClassId"),
    ("Tag_hasType_TagClass", "static", "static", "Tag", "TypeTagClassId"),
    ("Person_isLocatedIn_City", "dynamic", "dynamic", "Person",
     "LocationCityId"),
    ("Forum_hasModerator_Person", "dynamic", "dynamic", "Forum",
     "ModeratorPersonId"),
    ("Post_hasCreator_Person", "dynamic", "dynamic", "Post",
     "CreatorPersonId"),
    ("Forum_containerOf_Post", "dynamic", "dynamic", "Post",
     "ContainerForumId"),
    ("Post_isLocatedIn_Country", "dynamic", "dynamic", "Post",
     "LocationCountryId"),
    ("Comment_hasCreator_Person", "dynamic", "dynamic", "Comment",
     "CreatorPersonId"),
    ("Comment_isLocatedIn_Country", "dynamic", "dynamic", "Comment",
     "LocationCountryId"),
    ("Comment_replyOf_Post", "dynamic", "dynamic", "Comment", "ParentPostId"),
    ("Comment_replyOf_Comment", "dynamic", "dynamic", "Comment",
     "ParentCommentId"),
]


def _sh(cmd: str, pipefail: bool = True) -> str:
    # `zcat ... | head -1` SIGPIPEs zcat by design, so the header reader asks
    # for pipefail off; every counting pipeline keeps it on.
    argv = ["bash", "-o", "pipefail", "-c", cmd] if pipefail else ["bash", "-c", cmd]
    r = subprocess.run(argv, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd}\n{r.stderr}")
    return r.stdout.strip()


def _dir(root: Path, group: str, name: str) -> str:
    d = root / group / name
    if not d.is_dir():
        raise FileNotFoundError(str(d))
    return str(d)


def _header(d: str) -> list[str]:
    first = sorted(Path(d).glob("part-*.csv.gz"))[0]
    return _sh(f"zcat {first} | head -1", pipefail=False).split("|")


def _nparts(d: str) -> int:
    return len(list(Path(d).glob("part-*.csv.gz")))


def _rows(d: str) -> int:
    """Total data rows: every part-file carries its own header line."""
    total = int(_sh(f"zcat {d}/part-*.csv.gz | wc -l"))
    return total - _nparts(d)


def _id_digest(d: str, col: int) -> str:
    """sha256 of the numerically sorted id list (headers dropped: a header
    cell is never all-digits)."""
    return _sh(
        f"zcat {d}/part-*.csv.gz | awk -F'|' '$({col}) ~ /^[0-9]+$/ "
        f"{{print $({col})}}' | LC_ALL=C sort -n | sha256sum"
    ).split()[0]


def _fk_count(d: str, col: int) -> int:
    """Non-empty FK values in a merged node file's FK column."""
    return int(_sh(
        f"zcat {d}/part-*.csv.gz | awk -F'|' '$({col}) ~ /^[0-9]+$/ "
        f"{{n++}} END {{print n+0}}'"
    ))


def main() -> int:
    report: dict = {"merged_fk_root": str(MERGED),
                    "projected_fk_root": str(PROJECTED),
                    "nodes": {}, "relationships": {}, "failures": []}

    n_merged = n_projected = 0
    for name, group in NODE_TYPES:
        dm, dp = _dir(MERGED, group, name), _dir(PROJECTED, group, name)
        hm, hp = _header(dm), _header(dp)
        cm, cp = hm.index("id") + 1, hp.index("id") + 1
        rm, rp = _rows(dm), _rows(dp)
        sm, sp = _id_digest(dm, cm), _id_digest(dp, cp)
        n_merged += rm
        n_projected += rp
        agree = (rm == rp) and (sm == sp)
        report["nodes"][name] = {
            "merged_rows": rm, "projected_rows": rp,
            "merged_id_sha256": sm, "projected_id_sha256": sp,
            "agree": agree,
        }
        if not agree:
            report["failures"].append(f"node {name}: {rm}/{sm} vs {rp}/{sp}")
        print(f"node {name:14s} merged={rm:9d} projected={rp:9d} "
              f"{'OK' if agree else 'MISMATCH'}", flush=True)

    e_merged = e_projected = 0
    for name, group in STANDALONE:
        dm, dp = _dir(MERGED, group, name), _dir(PROJECTED, group, name)
        rm, rp = _rows(dm), _rows(dp)
        e_merged += rm
        e_projected += rp
        agree = rm == rp
        report["relationships"][name] = {
            "kind": "standalone-both-sides",
            "merged_rows": rm, "projected_rows": rp, "agree": agree}
        if not agree:
            report["failures"].append(f"rel {name}: {rm} vs {rp}")
        print(f"rel  {name:34s} merged={rm:9d} projected={rp:9d} "
              f"{'OK' if agree else 'MISMATCH'}", flush=True)

    for name, group, ngroup, node, fkcol in FK_MERGED:
        dp = _dir(PROJECTED, group, name)
        dn = _dir(MERGED, ngroup, node)
        col = _header(dn).index(fkcol) + 1
        rm, rp = _fk_count(dn, col), _rows(dp)
        e_merged += rm
        e_projected += rp
        agree = rm == rp
        report["relationships"][name] = {
            "kind": f"fk-merged-into-{node}.{fkcol}",
            "merged_rows": rm, "projected_rows": rp, "agree": agree}
        if not agree:
            report["failures"].append(f"rel {name}: {rm} vs {rp}")
        print(f"rel  {name:34s} merged={rm:9d} projected={rp:9d} "
              f"{'OK' if agree else 'MISMATCH'}", flush=True)

    report["totals"] = {
        "merged_nodes": n_merged, "projected_nodes": n_projected,
        "merged_edges": e_merged, "projected_edges": e_projected,
        "required_nodes": REQUIRED_NODES, "required_edges": REQUIRED_EDGES,
    }
    if n_merged != n_projected:
        report["failures"].append(f"node total {n_merged} vs {n_projected}")
    if e_merged != e_projected:
        report["failures"].append(f"edge total {e_merged} vs {e_projected}")
    if n_projected != REQUIRED_NODES:
        report["failures"].append(
            f"node total {n_projected} != required {REQUIRED_NODES}")
    if e_projected != REQUIRED_EDGES:
        report["failures"].append(
            f"edge total {e_projected} != required {REQUIRED_EDGES}")

    # The digest §9's dataset.digest points at: sha256 over the canonical
    # JSON of the per-type findings.
    body = json.dumps({"nodes": report["nodes"],
                       "relationships": report["relationships"],
                       "totals": report["totals"]},
                      sort_keys=True, separators=(",", ":"))
    report["gate_digest_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    report["passed"] = not report["failures"]

    OUT.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"\nnodes {n_merged} / {n_projected} (required {REQUIRED_NODES})")
    print(f"edges {e_merged} / {e_projected} (required {REQUIRED_EDGES})")
    print("GATE " + ("PASSED" if report["passed"] else "FAILED"))
    for f in report["failures"]:
        print("  FAIL " + f)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
