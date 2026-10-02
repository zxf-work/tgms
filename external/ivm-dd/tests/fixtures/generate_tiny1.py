#!/usr/bin/env python3
"""Generate the `tiny1` test-fixture bundle for the Rust `ivm-dd` crate.

This is a **test-fixture generator**, not a production tool: it builds a
tiny (6-node, 10-edge) TGMS store from scratch, registers all 13 of TGMS's
registered temporal-graph query operators against it (matching the
storm-v2 grid's own operator catalogue, `tgms/eval/storm.py::TEMPLATES`
minus `co_active`, plus the `compute` ∅-scope control), applies 3 small
deliberate corrections, and dumps the exact bundle shape
`scripts/export_storm_workload.py` produces:

    versions-epoch0.jsonl   every node/edge version at registration time
    artifacts.jsonl         name, op, args for every registered artifact
    eventlog-tail.jsonl     left empty here (see note below) — the Rust
                            loader this fixture serves does not read it
    deltas.jsonl            one line per epoch k=1..3: tt, correction
                            class/generator/placement, closed/inserted
                            version rows (diffed from the live version table)
    oracle.jsonl            one line per epoch k=0..3: every artifact's
                            real `result_digest` (computed via the SAME
                            `tgms.artifact.refresh.refresh` production code
                            path `scripts/export_storm_workload.py` uses,
                            never a hand-rolled digest), and refused names
    export-manifest.json    cell_id, cell_digest, config, file sha256s

**Why `eventlog-tail.jsonl` is written empty.** The real export script
accumulates the raw event-log bytes of each correction batch into this
file (for forensic replay of the *exact* bytes written). Per this
fixture's own task spec, the Rust loader this bundle serves does not read
that file at all, so it is written as a zero-byte placeholder rather than
reimplementing that byte-capture plumbing for a file nothing consumes.

**Registration/refresh mechanism**: this script reuses
`tgms.eval.storm.Storm._register_operator_artifact` (the exact
`"operator"`-kind registration idiom production code uses: a `ToolRouter`
call, an `ops/<name>.json` blob, and a registry record built from that
call's own envelope) and `tgms.artifact.refresh.refresh` (the exact
re-execution path `ExportStorm.run_batch_export` in
`scripts/export_storm_workload.py` uses) for every digest in this bundle —
so a digest recorded here is never a hand-rolled stand-in for what
production code would compute.

Run:

    uv run python external/ivm-dd/tests/fixtures/generate_tiny1.py

Writes into `external/ivm-dd/tests/fixtures/tiny1/` (sibling of this
script). Builds its working TGMS store under a temp directory that is
removed when the script exits; nothing under `external/` other than the
5 bundle files (plus this script itself) is touched.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

# ROOT = the TGMS repo checkout (4 parents up from this file:
# external/ivm-dd/tests/fixtures/generate_tiny1.py -> repo root).
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from tgms.artifact.refresh import refresh  # noqa: E402
from tgms.artifact.witness import RefreshHandle  # noqa: E402
from tgms.core.errors import TgmsError  # noqa: E402
from tgms.core.model import OPEN_END, canonical_json  # noqa: E402
from tgms.eval.storm import Storm  # noqa: E402
from tgms.storage.base import make_op  # noqa: E402
from tgms.storage.eventlog import EventLog, replay  # noqa: E402

import tgms  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "tiny1"

BUNDLE_FILES: tuple[str, ...] = (
    "versions-epoch0.jsonl", "artifacts.jsonl", "eventlog-tail.jsonl", "deltas.jsonl",
    "oracle.jsonl",
)


def _sha256_json(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# the tiny graph: 6 nodes, 10 edges, all in microseconds, small round numbers
# ---------------------------------------------------------------------------

NODES: tuple[str, ...] = ("n0", "n1", "n2", "n3", "n4", "n5")

#: (src, dst, vt_s) — every edge is a 1-microsecond point event [vt_s, vt_s+1).
#: rel_type is "R" for all of them. n0/n1/n1 (e_a, e_b, e_c below) form a
#: deliberate M_2node_pingpong triple: n0->n1 @100, n1->n0 @140, n0->n1 @170
#: — strictly ordered by (vt_s, eid) and spanning 70us, inside the delta=100
#: the registered count_temporal_motifs/find_temporal_motif_instances
#: artifacts use, so families 9/10 are non-trivial from epoch 0 on.
EDGES: tuple[tuple[str, str, int], ...] = (
    ("n0", "n1", 100),  # e_a — pingpong leg 1 (u->v)
    ("n1", "n0", 140),  # e_b — pingpong leg 2 (v->u)
    ("n0", "n1", 170),  # e_c — pingpong leg 3 (u->v); corrected below
    ("n1", "n2", 200),  # e_d
    ("n2", "n3", 250),  # e_e
    ("n3", "n4", 300),  # e_f
    ("n2", "n0", 320),  # e_g
    ("n4", "n5", 350),  # e_h
    ("n0", "n2", 150),  # e_i — retracted below
    ("n5", "n0", 500),  # e_j
)

INITIAL_TT = 10


def _build_initial_ops() -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    for uid in NODES:
        ops.append(make_op("assert_node", uid=uid, label="P", props={},
                           vt_s=0, vt_e=OPEN_END, source="ingest", provenance_ref=None))
    for src, dst, vt_s in EDGES:
        ops.append(make_op("assert_edge", src=src, dst=dst, rel_type="R", disc="", props={},
                           vt_s=vt_s, vt_e=vt_s + 1, source="ingest", provenance_ref=None))
    return ops


# ---------------------------------------------------------------------------
# the 13 registered operators (storm-v2 grid catalogue, registration-shaped args)
# ---------------------------------------------------------------------------

ARTIFACTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("entity_history", "entity_history", {"uid": "n0", "include_edges": True}),
    ("version_history", "version_history",
     {"kind": "node", "window": {"t_a": 0, "t_b": 1000}}),
    ("snapshot_subgraph", "snapshot_subgraph",
     {"seeds": ["n0"], "hops": 1, "t_valid": 100}),
    ("diff_snapshots", "diff_snapshots", {"t1": 50, "t2": 250, "scope": None}),
    ("neighborhood_evolution", "neighborhood_evolution",
     {"uid": "n0", "t1": 0, "t2": 600}),
    ("aggregate_events", "aggregate_events",
     {"group_by": [{"dim": "endpoint", "role": "src"}],
      "aggregates": [{"agg": "count"}], "window": {"t_a": 0, "t_b": 1000}}),
    ("graph_metric_timeseries", "graph_metric_timeseries",
     {"metric": "edge_event_count", "window": {"t_a": 0, "t_b": 1000}, "stride": 100}),
    ("burst_detection", "burst_detection",
     {"target": {"kind": "node_activity", "uid": "n0"},
      "window": {"t_a": 0, "t_b": 1000}, "stride": 50}),
    ("count_temporal_motifs", "count_temporal_motifs",
     {"motif": "M_2node_pingpong", "window": {"t_a": 0, "t_b": 1000}, "delta": 100}),
    ("find_temporal_motif_instances", "find_temporal_motif_instances",
     {"motif": "M_2node_pingpong", "window": {"t_a": 0, "t_b": 1000}, "delta": 100,
      "limit": 25}),
    ("temporal_reachability", "temporal_reachability",
     {"src": "n0", "window": {"t_a": 0, "t_b": 1000}}),
    ("temporal_paths", "temporal_paths",
     {"src": "n0", "dst": "n4", "window": {"t_a": 0, "t_b": 1000}, "k": 2, "max_hops": 4}),
    ("compute", "compute", {"fn": "count", "input": [{"x": 1}, {"x": 2}]}),
)


# ---------------------------------------------------------------------------
# the 3 corrections
# ---------------------------------------------------------------------------

def _correction_1_ops() -> list[dict[str, Any]]:
    """Class C (`correct`, whole-interval): e_c (n0->n1 @170) gets its
    believed end time extended from 171 to 185 — closes the old version,
    inserts a corrected one. Read by entity_history(n0)/version_history/
    diff_snapshots; its vt_s (170) is unchanged so the pingpong motif's own
    span (100..170) is untouched."""
    return [make_op("correct", ref={"kind": "edge", "src": "n0", "dst": "n1",
                                   "rel_type": "R", "disc": ""},
                    props={"note": "corrected_end"}, vt_s=170, vt_e=185,
                    source="ingest", provenance_ref=None)]


def _correction_2_ops() -> list[dict[str, Any]]:
    """Class A (`assert_edge`, new identity): a brand-new edge n3->n5 @600
    — pure insert, no closed version. Changes aggregate_events' per-src
    count for n3 and graph_metric_timeseries' edge_event_count bucket."""
    return [make_op("assert_edge", src="n3", dst="n5", rel_type="R", disc="", props={},
                    vt_s=600, vt_e=601, source="ingest", provenance_ref=None)]


def _correction_3_ops() -> list[dict[str, Any]]:
    """Class D (`retract`, full retraction): e_i (n0->n2 @150) retracted at
    t=150 (== its own vt_s), so no left replacement is inserted — a closed
    version with nothing taking its place. Changes entity_history(n0)/
    version_history/aggregate_events' per-src count for n0."""
    return [make_op("retract", ref={"kind": "edge", "src": "n0", "dst": "n2",
                                   "rel_type": "R", "disc": ""},
                    t=150, source="ingest", provenance_ref=None)]


CORRECTIONS: tuple[tuple[int, str, str, str, list[dict[str, Any]]], ...] = (
    (20, "C", "c1_whole", "in-window-read", _correction_1_ops()),
    (30, "A", "a2_disjoint", "new-identity", _correction_2_ops()),
    (40, "D", "d2_full", "in-window-read", _correction_3_ops()),
)


# ---------------------------------------------------------------------------
# version-table diff — copied verbatim in spirit from
# scripts/export_storm_workload.py's `_version_table`/`_diff_version_tables`
# (never imported: this script must not touch that file)
# ---------------------------------------------------------------------------

def _version_table(storm: Storm) -> dict[str, dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}
    for v in storm.store.adapter.all_node_versions():
        table[v.vid] = {"kind": "node", "vt_s": v.vt_s, "vt_e": v.vt_e,
                        "tt_s": v.tt_s, "tt_e": v.tt_e, "uid": v.uid, "label": v.label,
                        "props": v.props, "source": v.source, "provenance_ref": v.provenance_ref}
    for v in storm.store.adapter.all_edge_versions():
        table[v.vid] = {"kind": "edge", "vt_s": v.vt_s, "vt_e": v.vt_e,
                        "tt_s": v.tt_s, "tt_e": v.tt_e, "eid": v.eid, "src": v.src,
                        "dst": v.dst, "rel_type": v.rel_type, "disc": v.disc,
                        "props": v.props, "source": v.source, "provenance_ref": v.provenance_ref}
    return table


def _diff_version_tables(
    before: dict[str, dict[str, Any]], after: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    closed: list[dict[str, Any]] = []
    inserted: list[dict[str, Any]] = []
    for vid, row in after.items():
        prev = before.get(vid)
        if prev is None:
            inserted.append(dict(row, vid=vid))
        elif prev["tt_e"] == OPEN_END and row["tt_e"] != OPEN_END:
            closed.append({"kind": row["kind"], "vid": vid, "tt_e": row["tt_e"]})
    closed.sort(key=lambda r: r["vid"])
    inserted.sort(key=lambda r: r["vid"])
    return closed, inserted


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    store_dir = Path(tempfile.mkdtemp(prefix="tgms-tiny1-store-"))
    try:
        # Write the initial batch straight to the event log, then open a
        # throwaway store handle to replay it into the native adapter and
        # persist it to disk (a brand-new native adapter's replay cursor
        # reports an empty chain, so `Store._recover()` on the *first* open
        # of a store deliberately recovers nothing — see
        # `tests/test_artifact_refresh.py::_open_and_replay`, the same
        # two-step pattern copied here). Close it before `Storm` opens its
        # own handle: the engine is single-writer, so two live handles on
        # the same directory would contend for the writer lock.
        log = EventLog(store_dir / "eventlog.jsonl")
        log.append(INITIAL_TT, _build_initial_ops())
        bootstrap = tgms.open(store_dir, backend="native")
        replay(store_dir / "eventlog.jsonl", bootstrap.adapter, thread_cursor=True)
        bootstrap.close()

        # `n_artifacts=0`: the harness's own random population registration
        # (`Storm._register_population`) is skipped outright; every artifact
        # below is registered by this script's own explicit call, through
        # the exact same `_register_operator_artifact` production method.
        storm = Storm(store_dir, n_artifacts=0, seed=0, backend="native",
                     measure_ttf="sum")
        try:
            last_digest: dict[str, Any] = {}
            for name, op, args in ARTIFACTS:
                _record, env = storm._register_operator_artifact(name, op, args)
                last_digest[name] = env.get("result_digest")

            node_rows = sorted((v.to_json() for v in storm.store.adapter.all_node_versions()),
                               key=lambda r: r["vid"])
            edge_rows = sorted((v.to_json() for v in storm.store.adapter.all_edge_versions()),
                               key=lambda r: r["vid"])
            with open(OUT_DIR / "versions-epoch0.jsonl", "w") as f:
                for row in node_rows:
                    f.write(json.dumps({**row, "kind": "node"}, sort_keys=True) + "\n")
                for row in edge_rows:
                    f.write(json.dumps({**row, "kind": "edge"}, sort_keys=True) + "\n")

            with open(OUT_DIR / "artifacts.jsonl", "w") as f:
                for name, op, args in ARTIFACTS:
                    f.write(json.dumps({"name": name, "op": op, "args": args},
                                       sort_keys=True) + "\n")

            oracle_rows: list[dict[str, Any]] = [
                {"epoch": 0, "digests": dict(last_digest), "refused": []}]
            deltas_rows: list[dict[str, Any]] = []
            changed_by_epoch: dict[int, list[str]] = {}

            vtable = _version_table(storm)
            for epoch_idx, (tt, cls, generator, placement, ops) in enumerate(CORRECTIONS, 1):
                storm._write(tt, ops)
                vtable_after = _version_table(storm)
                closed, inserted = _diff_version_tables(vtable, vtable_after)
                vtable = vtable_after

                changed: list[str] = []
                refused: list[str] = []
                for name, _op, _args in ARTIFACTS:
                    record = storm.registry.current(name)
                    if record is None:
                        continue
                    handle = RefreshHandle(record.id, record.refresh["kind"],
                                          record.refresh["ref"],
                                          record.plan.get("plan_format"),
                                          record.refresh.get("basis_policy", "open"))
                    try:
                        new_record = refresh(record, handle, storm.store, storm.registry)
                    except TgmsError:
                        refused.append(name)
                        continue
                    new_digest = (new_record.payload or {}).get("result_digest")
                    if new_digest != last_digest.get(name):
                        changed.append(name)
                    last_digest[name] = new_digest

                deltas_rows.append({
                    "epoch": epoch_idx, "tt": tt, "correction_class": cls,
                    "generator": generator, "placement": placement,
                    "closed": closed, "inserted": inserted,
                })
                oracle_rows.append({"epoch": epoch_idx, "digests": dict(last_digest),
                                   "refused": sorted(refused)})
                changed_by_epoch[epoch_idx] = sorted(changed)

            with open(OUT_DIR / "deltas.jsonl", "w") as f:
                for row in deltas_rows:
                    f.write(json.dumps(row, sort_keys=True) + "\n")
            with open(OUT_DIR / "oracle.jsonl", "w") as f:
                for row in oracle_rows:
                    f.write(json.dumps(row, sort_keys=True) + "\n")
            # Left empty deliberately — see module docstring.
            (OUT_DIR / "eventlog-tail.jsonl").write_bytes(b"")

            file_sha256 = {name: _sha256_file(OUT_DIR / name) for name in BUNDLE_FILES}
            cell_digest = _sha256_json({"files": file_sha256})
            manifest = {
                "schema_version": "1.0.0",
                "cell_id": "tiny1",
                "config": {
                    "description": "synthetic test fixture for the ivm-dd Rust crate's "
                                   "unit tests — NOT a real storm-v2 grid cell",
                    "generator": "external/ivm-dd/tests/fixtures/generate_tiny1.py",
                    "n_nodes": len(NODES), "n_edges": len(EDGES),
                    "n_artifacts": len(ARTIFACTS), "n_corrections": len(CORRECTIONS),
                },
                "files": file_sha256,
                "cell_digest": cell_digest,
            }
            (OUT_DIR / "export-manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n")

            # -- sanity-check summary -------------------------------------------
            print(f"nodes={len(NODES)} edges={len(EDGES)} artifacts={len(ARTIFACTS)} "
                 f"corrections={len(CORRECTIONS)}")
            print(f"out_dir={OUT_DIR}")
            print("changed artifacts per epoch:")
            for epoch_idx in sorted(changed_by_epoch):
                print(f"  epoch {epoch_idx}: {changed_by_epoch[epoch_idx]}")
            motif_count_digest_epoch0 = oracle_rows[0]["digests"].get("count_temporal_motifs")
            print(f"count_temporal_motifs result_digest @epoch0: {motif_count_digest_epoch0}")
            print()
            print("oracle.jsonl rows:")
            for row in oracle_rows:
                print(json.dumps(row, sort_keys=True))
        finally:
            storm.close()
    finally:
        shutil.rmtree(store_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
