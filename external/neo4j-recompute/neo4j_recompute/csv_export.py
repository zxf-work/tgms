"""`versions-epoch0.jsonl` (X1's export bundle, memo §1.3) -> headerless CSV
part-files + separate header files for `neo4j-admin database import full`
(memo §2.2). Follows the two LDBC-run corrections already proven on xzgpu
(`benchmarks/ldbc-ref-v1/README.md` §3): no trailing `<database>` argument,
and the part-files must be headerless (the header lives in its own file).

Each exported version row carries `kind: "node"|"edge"` (added by X1's
dump) alongside the NV/EV columns in `schema.py`; `uid`/`src`/`dst` seed the
`:E` identity file.
"""
from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path

from .schema import EV_COLUMNS, LONG_COLUMNS, NV_COLUMNS


def _header_for(columns: tuple[str, ...], id_space: str | None = None,
                start_id: str | None = None, end_id: str | None = None,
                rel_type_col: bool = False) -> str:
    parts: list[str] = []
    if start_id:
        parts.append(f":START_ID({start_id})")
    if end_id:
        parts.append(f":END_ID({end_id})")
    for c in columns:
        if id_space and c == id_space[0]:
            parts.append(f"{c}:ID({id_space[1]})")
        elif c in LONG_COLUMNS:
            parts.append(f"{c}:long")
        else:
            parts.append(c)
    if rel_type_col:
        parts.append(":TYPE")
    return ",".join(parts)


def read_version_rows(versions_path: Path) -> Iterable[dict]:
    with versions_path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def export_csv(versions_path: Path, out_dir: Path) -> dict[str, Path]:
    """Write `entities.csv`, `node_versions.csv`, `edge_versions.csv` (each
    headerless) plus their `*-header.csv` files into `out_dir`. Returns the
    path for every file written, keyed by logical name.

    `entities.csv` gets one row per distinct identity seen as a node
    version's `uid` or an edge version's `src`/`dst` — written in sorted
    order so the file (and therefore the loaded `:E` node count) is
    deterministic given the same version table, independent of JSONL row
    order.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    uids: set[str] = set()
    node_rows: list[dict] = []
    edge_rows: list[dict] = []
    for row in read_version_rows(versions_path):
        if row["kind"] == "node":
            uids.add(row["uid"])
            node_rows.append(row)
        elif row["kind"] == "edge":
            uids.add(row["src"])
            uids.add(row["dst"])
            edge_rows.append(row)
        else:
            raise ValueError(f"unknown version kind: {row['kind']!r}")

    paths = {
        "entities": out_dir / "entities.csv",
        "entities_header": out_dir / "entities-header.csv",
        "node_versions": out_dir / "node_versions.csv",
        "node_versions_header": out_dir / "node_versions-header.csv",
        "edge_versions": out_dir / "edge_versions.csv",
        "edge_versions_header": out_dir / "edge_versions-header.csv",
    }

    # `uid:ID(E)` (not bare `:ID(E)`): naming the id-space column "uid" makes
    # the importer store it as a regular, queryable `uid` property on every
    # `:E` node *in addition to* using it for relationship binding — every
    # Cypher text in `queries.py` looks entities up by `{uid:$uid}`.
    paths["entities_header"].write_text("uid:ID(E)\n")
    with paths["entities"].open("w", newline="") as f:
        w = csv.writer(f)
        for uid in sorted(uids):
            w.writerow([uid])

    paths["node_versions_header"].write_text(
        _header_for(NV_COLUMNS, id_space=("vid", "NV")) + "\n")
    with paths["node_versions"].open("w", newline="") as f:
        w = csv.writer(f)
        _write_version_rows(w, node_rows, NV_COLUMNS)

    paths["edge_versions_header"].write_text(
        _header_for(EV_COLUMNS, start_id="E", end_id="E", rel_type_col=True) + "\n")
    with paths["edge_versions"].open("w", newline="") as f:
        w = csv.writer(f)
        for row in edge_rows:
            values = [row["src"], row["dst"]]
            values += _row_values(row, EV_COLUMNS)
            # The Neo4j relationship *type* is always the uniform `EV`
            # (schema §2.1: `(:E)-[:EV]->(:E)`) — `rel_type` is carried as
            # an ordinary property column instead (already in EV_COLUMNS),
            # never used as the graph's own relationship type.
            values.append("EV")
            w.writerow(values)

    return paths


def _row_values(row: dict, columns: tuple[str, ...]) -> list:
    out = []
    for c in columns:
        v = row[c]
        if c == "props":
            # already canonical JSON text in the export; re-serialize isn't
            # needed, but a defensive round-trip keeps a hand-edited fixture
            # (e.g. a test's tiny cell) from writing a non-canonical string.
            v = json.dumps(v, sort_keys=True, separators=(",", ":")) \
                if not isinstance(v, str) else v
        out.append("" if v is None else v)
    return out


def _write_version_rows(w, rows: list[dict], columns: tuple[str, ...]) -> None:
    for row in rows:
        w.writerow(_row_values(row, columns))


def write_import_header_only(out_dir: Path, header_text: str, name: str) -> Path:
    """Escape hatch for tests: write a header file without a data pass."""
    p = out_dir / name
    p.write_text(header_text)
    return p
