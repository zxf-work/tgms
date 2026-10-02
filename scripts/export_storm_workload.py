#!/usr/bin/env python3
"""Lane X1 — export the committed storm-v2 correction-grid workload.

`docs/design/EXTERNAL_BASELINES_DESIGN_2026-10-02.md` §1 (gates P-EXT1/P-EXT2);
`docs/design/OSDI27_AUDIT_AND_PLAN_2026-09-13.md` §4.3b P-EXT1/P-EXT2.

For each cell (store x mix x age x n_artifacts x seed) of the committed
storm-v2 grid, this script replays the cell **deterministically at this
repo's own harness** (`tgms.eval.storm.Storm`/`build_mix`) and dumps, per
cell, under `<export-root>/<cell-id>/`:

    versions-epoch0.jsonl   every node/edge version at registration time
    artifacts.jsonl         name, op, args for every registered artifact
    eventlog-tail.jsonl     the raw log bytes of every correction batch,
                            as written (`EventLog.append`'s own bytes)
    deltas.jsonl            one line per epoch k=1..batches: tt, correction
                            class/generator/placement, closed/inserted
                            version rows (diffed from the live version table)
    oracle.jsonl            one line per epoch k=0..batches: every
                            artifact's result_digest, and refused names
    export-manifest.json    sha256 of every bundle file, a version-table
                            sha256 per epoch, a cell_digest, tool/commit shas
    digests.json            the equality proof (see below)

**Construction** mirrors `scripts/bench_correction_storm.py` exactly: copy
the named store to a scratch dir, then
`Storm(work_store, n_artifacts=N, seed=S, backend="native",
mix=build_mix(mix, burst_size=10000, burst_after=10, age=age),
measure_ttf="sum")`, then the harness's own bounded-attempt batch loop
(`max_attempts_factor=4` by default). Same seed => same population draw,
same bursts (determinism is asserted by `tests/test_export_storm_workload.py`:
two exports of the same cell spec produce byte-identical
`versions-epoch0.jsonl` and `eventlog-tail.jsonl`).

**`ExportStorm.run_batch_export`** replaces `Storm.run_batch`'s arm-scoring
body with exactly the oracle pass that body also runs (draw -> write ->
refresh every registered artifact, compare `result_digest` with the last
observed value) and nothing else -- no `affected()`/`check_artifact()` call,
since (memo §1.2) those consume no RNG and write nothing to the log, so
skipping them cannot perturb the replay.

**Equality (per cell, both levels attempted; recorded in `digests.json`,
never silently assumed):**

- **L1**: sha256 of the replayed store's final `eventlog.jsonl` equals the
  committed grid row's `dataset.digest` (covers source store + every batch,
  exactly what `bench_correction_storm.py` computes after the last batch).
- **L2**: per-batch `log_bytes` (the eventlog's cumulative size after each
  batch -- a strong per-batch fingerprint of op content and count) and the
  population's `n_registered`/narrowing `per_template_counts`, compared to
  the committed grid row. Where the committed per-batch
  `correction_class`/`generator`/`placement`/`changed`/`refused_count` rows
  also survive in the repo (true today only for the probe cell -- see the
  module-level `NOTE` below), those are compared too and the result is
  plain `"L2"`; where they do not, the result is honestly labelled
  `"L2-partial"` and `digests.json["note"]` says exactly what was and was
  not checked. A cell whose *available* signals disagree is `"FAIL"` and is
  not exported (memo: "a cell that fails is not exported, stop and report"
  -- this script still writes its bundle for forensics, but the top-level
  exit code and the printed summary flag it loudly; do not fold a FAIL into
  a campaign's success count).

**NOTE -- a memo assumption that did not survive contact with the repo**:
`EXTERNAL_BASELINES_DESIGN_2026-10-02.md` §1.4 describes L2 as a per-batch
comparison on `correction_class/generator/placement`, the oracle's own
`changed` list, and `refused_count`, implying those are available from "the
committed record" for every cell. In fact the 36-cell main grid's own
per-batch rows file (`storm-v2-main-grid-2026-09-15-rows.jsonl`) is a
one-line-per-*cell* summary (config + aggregate `summary`), not a
one-line-per-*batch* detail file -- the real per-batch detail lived at the
path the manifest's `"record"` field names
(`/home/xzhang12/storm-v2-work-h/records/task-0/...`), on the iTiger
checkout the PI cleared on 2026-09-24. It is gone. Only `log_bytes` per
batch (via the committed `check_cost_curve`) and the population-level
`n_registered`/`narrowing_coverage.per_template_counts` survive for the 36
main-grid cells. The probe cell is unaffected: its own `-rows.jsonl` *is*
the per-batch detail file and is committed in full, so the probe alone
reaches a full `"L2"` (or `"L1"`) rather than `"L2-partial"`.

**Parameterization for the pending 6 Addendum-A2 cells.** Those are *not*
added by this script's default cell set (PI ruling pending, per the
external-baselines memo's own §0 item A2) but the same command adds them
later without a code change: `--extra-cells <file.json>`, a JSON list of
`{"store", "mix", "age", "n_artifacts", "seed", "batches"}` objects. An
extra cell has no committed grid row, so its `digests.json` reports
`"new cell (no committed digest)"` (or `"L1"` if some other reference
digest is supplied by hand and happens to match) rather than a fabricated
equality claim -- exactly the memo's own note that A2/T1 cells "have no
committed digest; their L1 reference is T1's own record." Once the PI
rules A2 in, this export's own digest IS that reference for the T1
control and both external configurations -- `INDEX.json` says so.

**A second memo assumption that did not survive contact with the repo
(found running the collegemsg-c3-none-n1000-s0 smoke cell on xzgpu,
2026-10-02):** `export_cell` now calls `_maybe_upgrade_manifests` on its
own scratch copy of the store, right after copying it, before any replay.
A store copied onto a shared host can have been written by an engine that
predates this worktree's manifest format; this build's native engine then
opens it **read-only**, and every correction `_write` raises `StateError`
(a `TgmsError`), which `run_batch_export` -- like the harness's own
`Storm.run_batch` -- catches and treats as "no correction was realizable
this attempt". The visible symptom is indistinguishable from an empty mix:
`export_cell`'s bounded-attempt loop exhausts its cap and raises "only
realized 0/N batches ... (mix starved)", even though `Mix.__call__` was
returning candidates on every attempt. `_maybe_upgrade_manifests` is
idempotent and touches only the scratch copy, never the shared source
store under `--stores-dir`.

Usage:

    uv run python scripts/export_storm_workload.py list-cells

    uv run python scripts/export_storm_workload.py export \\
        --cells collegemsg-c3-none-n1000-s0,synth-iv-60k-c1-deep-n1000-s0 \\
        --stores-dir /mnt/project/xzhang/tgms/work/tgms/stores \\
        --export-root /mnt/project/xzhang/tgms/external-v1/export \\
        --build-missing-stores

    uv run python scripts/export_storm_workload.py aggregate \\
        --export-root /mnt/project/xzhang/tgms/external-v1/export

    uv run python scripts/export_storm_workload.py host-snapshot \\
        --out /mnt/project/xzhang/tgms/external-v1/HOST.log --label start
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tgms.artifact.refresh import refresh  # noqa: E402
from tgms.artifact.witness import RefreshHandle  # noqa: E402
from tgms.core.errors import TgmsError  # noqa: E402
from tgms.core.model import OPEN_END, canonical_json  # noqa: E402
from tgms.eval.storm import Storm, build_mix, narrowing_coverage  # noqa: E402

SCHEMA_VERSION = "1.0.0"

def _grid_paths(grid_root: Path) -> tuple[Path, Path, Path]:
    """The three committed files `load_grid_cells` reads, under
    `grid_root/benchmarks/storm-v1/`. `grid_root` is deliberately a
    *separate* knob from `ROOT` (where this script's own `sys.path`
    insertion points, so `import tgms.eval.storm` resolves the harness at
    whatever commit the script itself is checked out at): the grid's own
    commit `fdd393c` (memo §1.2) predates the commit that landed these
    files on `main` (`4c609c9`, 33 commits later) by construction -- a
    worktree pinned at `fdd393c` for harness fidelity does not carry them.
    Point `--grid-root` at any checkout that has `main` (or later) merged
    in; it is read-only reference data, never replayed itself."""
    base = grid_root / "benchmarks" / "storm-v1"
    return (base / "storm-v2-main-grid-2026-09-15-rows.jsonl",
           base / "storm-v2-r18-probe-2026-09-15.json",
           base / "storm-v2-r18-probe-2026-09-15-rows.jsonl")

BUNDLE_FILES: tuple[str, ...] = (
    "versions-epoch0.jsonl", "artifacts.jsonl", "eventlog-tail.jsonl", "deltas.jsonl",
    "oracle.jsonl",
)


# ---------------------------------------------------------------------------
# hashing helpers
# ---------------------------------------------------------------------------

def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _sha256_json(obj: Any) -> str:
    return _sha256_bytes(canonical_json(obj).encode("utf-8"))


def _git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
                               text=True, check=True).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:  # pragma: no cover - environment without git
        return "0" * 40


# ---------------------------------------------------------------------------
# cell identity and the committed grid
# ---------------------------------------------------------------------------

def cell_id(store: str, mix: str, age: str | None, n_artifacts: int, seed: int) -> str:
    return f"{store}-{mix}-{age or 'none'}-n{n_artifacts}-s{seed}"


@dataclass(frozen=True)
class CellSpec:
    cell_id: str
    store: str
    mix: str
    age: str | None
    n_artifacts: int
    seed: int
    batches: int
    committed_dataset_digest: str | None
    committed_log_bytes: tuple[int, ...] | None
    committed_n_registered: int | None
    committed_per_template_counts: dict[str, int] | None
    committed_changed: tuple[tuple[str, ...], ...] | None
    committed_correction_meta: tuple[tuple[str, str, str], ...] | None
    committed_refused_count: tuple[int, ...] | None
    source: str  # "main-grid" | "probe" | "extra"


def load_grid_cells(grid_root: Path = ROOT) -> dict[str, CellSpec]:
    """The 36 committed storm-v2 main-grid cells plus the N=10,000 probe
    cell -- 37 in total, read straight from the committed benchmark files
    under `grid_root` (never hand-copied), keyed by `cell_id`."""
    main_grid_rows, probe_manifest_path, probe_rows_path = _grid_paths(grid_root)
    cells: dict[str, CellSpec] = {}

    for line in main_grid_rows.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        c = row["config"]
        curve = sorted(row["summary"]["check_cost_curve"], key=lambda b: b["batch_index"])
        cid = cell_id(c["store"], c["mix"], c["age"], c["n_artifacts"], c["seed"])
        cells[cid] = CellSpec(
            cell_id=cid, store=c["store"], mix=c["mix"], age=c["age"],
            n_artifacts=c["n_artifacts"], seed=c["seed"], batches=c["batches"],
            committed_dataset_digest=row["dataset"]["digest"],
            committed_log_bytes=tuple(b["log_bytes"] for b in curve),
            committed_n_registered=c["n_registered"],
            committed_per_template_counts=row["summary"]["narrowing_coverage"]["per_template_counts"],
            committed_changed=None, committed_correction_meta=None,
            committed_refused_count=None, source="main-grid",
        )

    probe_manifest = json.loads(probe_manifest_path.read_text())
    pc = probe_manifest["config"]
    probe_rows = sorted(
        (json.loads(line) for line in probe_rows_path.read_text().splitlines() if line.strip()),
        key=lambda r: r["batch_index"])
    pcid = cell_id(pc["store"], pc["mix"], pc["age"], pc["n_artifacts"], pc["seed"])
    cells[pcid] = CellSpec(
        cell_id=pcid, store=pc["store"], mix=pc["mix"], age=pc["age"],
        n_artifacts=pc["n_artifacts"], seed=pc["seed"], batches=pc["batches"],
        committed_dataset_digest=probe_manifest["dataset"]["digest"],
        committed_log_bytes=tuple(r["log_bytes"] for r in probe_rows),
        committed_n_registered=pc["n_registered"],
        committed_per_template_counts=None,
        committed_changed=tuple(tuple(sorted(r["changed"])) for r in probe_rows),
        committed_correction_meta=tuple(
            (r["correction_class"], r["correction_generator"], r["correction_placement"])
            for r in probe_rows),
        committed_refused_count=tuple(r["refused_count"] for r in probe_rows),
        source="probe",
    )
    return cells


def load_extra_cells(path: Path) -> dict[str, CellSpec]:
    """Addendum-A2-style cells with no committed grid row (§0 item A2,
    pending the PI's ruling -- not included by default, added by this flag
    with no code change needed when the ruling lands)."""
    specs = json.loads(path.read_text())
    out: dict[str, CellSpec] = {}
    for e in specs:
        cid = cell_id(e["store"], e["mix"], e.get("age"), e["n_artifacts"], e["seed"])
        out[cid] = CellSpec(
            cell_id=cid, store=e["store"], mix=e["mix"], age=e.get("age"),
            n_artifacts=e["n_artifacts"], seed=e["seed"], batches=e["batches"],
            committed_dataset_digest=e.get("committed_dataset_digest"),
            committed_log_bytes=None, committed_n_registered=None,
            committed_per_template_counts=None, committed_changed=None,
            committed_correction_meta=None, committed_refused_count=None, source="extra",
        )
    return out


# ---------------------------------------------------------------------------
# store resolution (A3: synth-iv-60k is absent on xzgpu)
# ---------------------------------------------------------------------------

def _maybe_upgrade_manifests(store_path: Path) -> None:
    """A store copied onto a shared host can have been written by an older
    engine build (manifest format 1 or 2); this build's native engine
    writes format 3 and opens an older-format store **read-only** -- every
    write then raises `StateError` from `adapter.commit()`.
    `ExportStorm.run_batch_export` (like `Storm.run_batch`) catches
    `TgmsError` (which `StateError` is) around `self._write(...)` and
    returns `None`, so the symptom at the call site is indistinguishable
    from "the mix had nothing to draw this attempt" -- `export_cell`'s
    bounded-attempt loop exhausts its cap and reports "0/N batches
    realized (mix starved)", which is not what happened. (Found live:
    the collegemsg-c3-none-n1000-s0 smoke cell on xzgpu, 2026-10-02 --
    `mix()` returned 38 correction candidates on every attempt; every
    `_write` failed with exactly this `StateError`, because the
    `collegemsg` store under `/mnt/project/xzhang/tgms/work/tgms/stores`
    was written by an engine that predates this worktree's manifest
    format.)

    Mirrors `scripts/longevity_run.py::_maybe_upgrade_manifests`'s own
    feature-detect-then-upgrade shape (there, ahead of a long-running
    writer; here, ahead of the replay), reading the real
    `verify()["manifest_format"]` field. Called only on `store_path`,
    which by the time this runs is always `export_cell`'s own scratch
    copy under a `tempfile.mkdtemp()` dir -- never the shared source
    store under `--stores-dir`, so the fix cannot touch a committed or
    shared store. Idempotent (`upgrade_manifests()` reports `upgraded:
    False` on an already-format-3 store) and "touches nothing else" per
    its own docstring (one checkpoint written, `CURRENT` flipped; the
    logical rows are unchanged, so it cannot perturb the replay the way a
    real content edit would)."""
    import tgms

    probe = tgms.open(store_path, backend="native", read_only=True)
    try:
        fmt = probe.adapter.verify().get("manifest_format")
    finally:
        probe.close()
    if fmt is None or fmt >= 3:
        return
    print(f"  {store_path}: manifest format {fmt} (older engine); running "
          f"upgrade-manifests on this scratch copy before replay ...", flush=True)
    result = subprocess.run(
        [sys.executable, "-m", "tgms.cli", "store", "upgrade-manifests",
         "--store", str(store_path)],
        capture_output=True, text=True, cwd=ROOT)
    if result.returncode != 0:
        raise RuntimeError(
            f"upgrade-manifests failed for {store_path} (rc={result.returncode}): "
            f"{result.stderr.strip()}")


def ensure_store(name: str, stores_dir: Path, *, build_missing: bool) -> tuple[Path, str]:
    path = stores_dir / name
    if path.exists():
        return path, "present"
    if name == "synth-iv-60k" and build_missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(ROOT / "scripts" / "build_synth_iv_store.py"),
               "--out", str(path), "--scale", "60000", "--backend", "native",
               "--digest", "none"]
        subprocess.run(cmd, check=True, cwd=ROOT)
        return path, "rebuilt-synth-iv-60k"
    raise FileNotFoundError(
        f"store {name!r} not found at {path!s}; pass --build-missing-stores to build "
        f"synth-iv-60k fresh (A3), or point --stores-dir at a directory that has it")


# ---------------------------------------------------------------------------
# the replaying subclass (memo §1.2)
# ---------------------------------------------------------------------------

class ExportStorm(Storm):
    """`run_batch_export` is `Storm.run_batch` with the arm-scoring body
    (the `affected()`/`check_artifact()` lookup, the six-arm bookkeeping)
    removed and the oracle pass kept verbatim: draw one correction, write
    it (`self._write`, byte-for-byte what `Storm.run_batch` does), then
    `refresh()` every registered artifact and compare `result_digest`
    against the last value this harness observed for that name."""

    def run_batch_export(self, batch_index: int) -> dict[str, Any] | None:
        corrections = self.mix(self.store, self.sub, self.target, self.rng)
        if not corrections:
            return None
        correction = corrections[self.rng.randrange(len(corrections))]
        tt = self._next_tt()

        log_path = self.store.eventlog.path
        start_size = log_path.stat().st_size
        try:
            self._write(tt, list(correction.ops))
        except TgmsError:
            return None
        end_size = log_path.stat().st_size
        with open(log_path, "rb") as f:
            f.seek(start_size)
            record_bytes = f.read(end_size - start_size)
        self.n_batches += 1

        oracle_changed: list[str] = []
        refused: list[str] = []
        for name in list(self.artifacts):
            record = self.registry.current(name)
            if record is None:
                continue
            handle = RefreshHandle(record.id, record.refresh["kind"], record.refresh["ref"],
                                   record.plan.get("plan_format"),
                                   record.refresh.get("basis_policy", "open"))
            try:
                new_record = refresh(record, handle, self.store, self.registry)
            except TgmsError:
                refused.append(name)
                continue
            new_digest = (new_record.payload or {}).get("result_digest")
            ra = self.artifacts[name]
            if new_digest != ra.last_env.get("result_digest"):
                oracle_changed.append(name)
            ra.last_env = {"result_digest": new_digest}

        return {
            "batch_index": batch_index,
            "tt": tt,
            "correction_class": correction.cls,
            "correction_generator": correction.generator,
            "correction_placement": correction.placement,
            "record_bytes": record_bytes,
            "log_bytes": self.store.eventlog.size(),
            "changed": sorted(oracle_changed),
            "refused": sorted(refused),
            "refused_count": len(refused),
        }


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


def _version_table_sha256(table: dict[str, dict[str, Any]]) -> str:
    rows = [dict(row, vid=vid) for vid, row in sorted(table.items())]
    return _sha256_json(rows)


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
# equality (memo §1.4, honestly adjusted per the module-level NOTE above)
# ---------------------------------------------------------------------------

def score_equality(
    spec: CellSpec, final_log_sha256: str, our_log_bytes: list[int], n_registered_actual: int,
    per_template_counts: dict[str, int], batch_results: list[dict[str, Any]],
) -> dict[str, Any]:
    l1 = (spec.committed_dataset_digest is not None
          and final_log_sha256 == spec.committed_dataset_digest)

    if spec.source == "extra" and spec.committed_dataset_digest is None:
        return {
            "cell_id": spec.cell_id,
            "equality_level": "L1" if l1 else "new cell (no committed digest)",
            "checks": {"l1_eventlog_sha_match": l1, "final_log_sha256": final_log_sha256,
                      "committed_dataset_digest": None},
            "note": "extra cell (Addendum-A2-style); no committed grid row, so there is no "
                    "L1/L2 reference to check against -- this export's own digest becomes "
                    "the reference for the T1 control and both external configurations, "
                    "per the memo's own A2/T1 ruling",
        }

    checks: dict[str, Any] = {
        "l1_eventlog_sha_match": l1,
        "final_log_sha256": final_log_sha256,
        "committed_dataset_digest": spec.committed_dataset_digest,
        "l2_n_registered_match": (n_registered_actual == spec.committed_n_registered),
        "l2_log_bytes_per_batch_match": (
            list(our_log_bytes) == list(spec.committed_log_bytes)
            if spec.committed_log_bytes else None),
        "l2_per_template_counts_match": (
            per_template_counts == spec.committed_per_template_counts
            if spec.committed_per_template_counts is not None else None),
        "l2_full_per_batch_available": spec.committed_changed is not None,
        "l2_full_per_batch_match": None,
    }
    if spec.committed_changed is not None:
        full_ok = True
        for i, r in enumerate(batch_results):
            want_changed = spec.committed_changed[i]
            want_meta = spec.committed_correction_meta[i]
            want_refused = spec.committed_refused_count[i]
            if (tuple(sorted(r["changed"])) != want_changed
                    or (r["correction_class"], r["correction_generator"],
                       r["correction_placement"]) != want_meta
                    or r["refused_count"] != want_refused):
                full_ok = False
                break
        checks["l2_full_per_batch_match"] = full_ok

    hard_ok = bool(checks["l2_n_registered_match"])
    optional = [v for v in (checks["l2_log_bytes_per_batch_match"],
                            checks["l2_per_template_counts_match"],
                            checks["l2_full_per_batch_match"]) if v is not None]

    if l1:
        level = "L1"
    elif not hard_ok:
        level = "FAIL"
    elif optional and not all(optional):
        level = "FAIL"
    elif not optional:
        level = "FAIL"
    elif checks["l2_full_per_batch_match"] is True:
        level = "L2"
    else:
        level = "L2-partial"

    note = (
        "full per-batch check (class/generator/placement/changed/refused_count) available "
        "and compared" if checks["l2_full_per_batch_available"] else
        "the committed per-batch detail file for this cell is not in the repo (main-grid "
        "cells' per-batch rows lived only on the now-cleared iTiger checkout); L2 here is "
        "log_bytes-per-batch + n_registered + narrowing_coverage.per_template_counts only"
    )
    return {"cell_id": spec.cell_id, "equality_level": level, "checks": checks, "note": note}


# ---------------------------------------------------------------------------
# one cell, start to finish
# ---------------------------------------------------------------------------

def export_cell(
    spec: CellSpec, stores_dir: Path, export_root: Path, *, build_missing: bool,
    max_attempts_factor: int = 4,
) -> dict[str, Any]:
    store_path, store_status = ensure_store(spec.store, stores_dir, build_missing=build_missing)
    work_root = Path(tempfile.mkdtemp(prefix=f"export-{spec.cell_id}-"))
    work_store = work_root / "store"
    shutil.copytree(store_path, work_store)
    _maybe_upgrade_manifests(work_store)
    out_dir = export_root / spec.cell_id
    out_dir.mkdir(parents=True, exist_ok=True)

    mix = build_mix(spec.mix, burst_size=10000, burst_after=10, age=spec.age)
    t_start = time.time()
    storm = ExportStorm(work_store, n_artifacts=spec.n_artifacts, seed=spec.seed,
                        backend="native", mix=mix, measure_ttf="sum")
    try:
        node_rows = sorted((v.to_json() for v in storm.store.adapter.all_node_versions()),
                          key=lambda r: r["vid"])
        edge_rows = sorted((v.to_json() for v in storm.store.adapter.all_edge_versions()),
                          key=lambda r: r["vid"])
        with open(out_dir / "versions-epoch0.jsonl", "w") as f:
            for row in node_rows:
                f.write(json.dumps({**row, "kind": "node"}, sort_keys=True) + "\n")
            for row in edge_rows:
                f.write(json.dumps({**row, "kind": "edge"}, sort_keys=True) + "\n")

        with open(out_dir / "artifacts.jsonl", "w") as f:
            for name in storm.artifacts:
                blob = json.loads((work_store / "ops" / f"{name}.json").read_text())
                f.write(json.dumps({"name": name, "op": blob["op"], "args": blob["args"]},
                                   sort_keys=True) + "\n")

        vtable = _version_table(storm)
        version_table_sha256 = [_version_table_sha256(vtable)]
        oracle_rows = [{"epoch": 0,
                       "digests": {n: ra.last_env.get("result_digest")
                                  for n, ra in storm.artifacts.items()},
                       "refused": []}]
        deltas_rows: list[dict[str, Any]] = []
        tail_chunks: list[bytes] = []
        batch_results: list[dict[str, Any]] = []

        attempts = 0
        cap = max(1, spec.batches) * max_attempts_factor
        while len(batch_results) < spec.batches and attempts < cap:
            attempts += 1
            r = storm.run_batch_export(len(batch_results))
            if r is None:
                continue
            batch_results.append(r)
            tail_chunks.append(r["record_bytes"])

            vtable_after = _version_table(storm)
            closed, inserted = _diff_version_tables(vtable, vtable_after)
            deltas_rows.append({
                "epoch": r["batch_index"] + 1, "tt": r["tt"],
                "correction_class": r["correction_class"], "generator": r["correction_generator"],
                "placement": r["correction_placement"], "closed": closed, "inserted": inserted,
            })
            vtable = vtable_after
            version_table_sha256.append(_version_table_sha256(vtable))
            oracle_rows.append({"epoch": r["batch_index"] + 1,
                               "digests": {n: ra.last_env.get("result_digest")
                                          for n, ra in storm.artifacts.items()},
                               "refused": r["refused"]})

        if len(batch_results) < spec.batches:
            raise RuntimeError(
                f"{spec.cell_id}: only realized {len(batch_results)}/{spec.batches} batches "
                f"within {cap} draws (mix starved) -- not exported")

        with open(out_dir / "eventlog-tail.jsonl", "wb") as f:
            for chunk in tail_chunks:
                f.write(chunk)
        with open(out_dir / "deltas.jsonl", "w") as f:
            for row in deltas_rows:
                f.write(json.dumps(row, sort_keys=True) + "\n")
        with open(out_dir / "oracle.jsonl", "w") as f:
            for row in oracle_rows:
                f.write(json.dumps(row, sort_keys=True) + "\n")

        final_log_sha256 = _sha256_file(storm.store.eventlog.path)
        narrowing = narrowing_coverage(storm.registry, storm.artifacts)
        n_registered_actual = len(storm.artifacts)
        wall_s = time.time() - t_start
    finally:
        storm.close()
        shutil.rmtree(work_root, ignore_errors=True)

    digests = score_equality(
        spec, final_log_sha256, [r["log_bytes"] for r in batch_results], n_registered_actual,
        narrowing["per_template_counts"], batch_results)

    file_sha256 = {name: _sha256_file(out_dir / name) for name in BUNDLE_FILES}
    cell_digest = _sha256_json({"files": file_sha256})
    manifest = {
        "schema_version": SCHEMA_VERSION, "cell_id": spec.cell_id,
        "config": {"store": spec.store, "mix": spec.mix, "age": spec.age,
                  "n_artifacts": spec.n_artifacts, "seed": spec.seed, "batches": spec.batches},
        "store_status": store_status, "git_commit": _git_commit(),
        "tool_sha256": _sha256_file(Path(__file__)),
        "files": file_sha256, "version_table_sha256": version_table_sha256,
        "cell_digest": cell_digest, "wall_s": wall_s,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    (out_dir / "export-manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    (out_dir / "digests.json").write_text(json.dumps(digests, indent=2, sort_keys=True) + "\n")

    return {"cell_id": spec.cell_id, "equality_level": digests["equality_level"],
           "out_dir": str(out_dir), "wall_s": wall_s, "store_status": store_status}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_list_cells(args: argparse.Namespace) -> int:
    cells = load_grid_cells(args.grid_root)
    if args.extra_cells:
        cells.update(load_extra_cells(args.extra_cells))
    for cid in sorted(cells):
        spec = cells[cid]
        print(f"{cid}\t{spec.source}\tbatches={spec.batches}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    cells = load_grid_cells(args.grid_root)
    if args.extra_cells:
        cells.update(load_extra_cells(args.extra_cells))

    if args.cells == "all":
        selected = list(cells.values())
    else:
        wanted = [c.strip() for c in args.cells.split(",") if c.strip()]
        missing = [c for c in wanted if c not in cells]
        if missing:
            raise SystemExit(f"unknown cell id(s): {missing} -- run list-cells to see "
                             f"available ids (pass --extra-cells for A2-style cells)")
        selected = [cells[c] for c in wanted]

    args.export_root.mkdir(parents=True, exist_ok=True)
    results = []
    had_error = False
    for spec in selected:
        print(f"[{spec.cell_id}] starting ({spec.source}, {spec.batches} batches)", flush=True)
        t0 = time.time()
        try:
            r = export_cell(spec, args.stores_dir, args.export_root,
                            build_missing=args.build_missing_stores,
                            max_attempts_factor=args.max_attempts_factor)
            print(f"[{spec.cell_id}] done in {time.time() - t0:.1f}s "
                 f"equality={r['equality_level']}", flush=True)
            if r["equality_level"] == "FAIL":
                had_error = True
                print(f"[{spec.cell_id}] ** EQUALITY FAIL -- see digests.json **", flush=True)
        except Exception as exc:  # noqa: BLE001 - one cell's failure must not stop the rest
            had_error = True
            print(f"[{spec.cell_id}] FAILED: {exc}", flush=True)
            r = {"cell_id": spec.cell_id, "equality_level": "ERROR", "error": str(exc)}
        results.append(r)

    print(json.dumps(results, indent=2))
    return 1 if had_error else 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    export_root: Path = args.export_root
    out_path: Path = args.out or (export_root / "INDEX.json")
    index: dict[str, Any] = {}
    for d in sorted(export_root.iterdir()):
        if not d.is_dir():
            continue
        digests_path = d / "digests.json"
        manifest_path = d / "export-manifest.json"
        if not digests_path.exists() or not manifest_path.exists():
            continue
        digests = json.loads(digests_path.read_text())
        manifest = json.loads(manifest_path.read_text())
        size_bytes = sum(f.stat().st_size for f in d.iterdir() if f.is_file())
        is_new_cell = digests["equality_level"] == "new cell (no committed digest)"
        index[d.name] = {
            "paths": {name: str(d / name) for name in BUNDLE_FILES}
            | {"export-manifest.json": str(manifest_path), "digests.json": str(digests_path)},
            "size_bytes": size_bytes,
            "equality_level": digests["equality_level"],
            "cell_digest": manifest["cell_digest"],
            "config": manifest["config"],
            "store_status": manifest.get("store_status"),
            "wall_s": manifest.get("wall_s"),
        }
        if is_new_cell:
            # Addendum-A2 cells (PI-accepted): no committed record exists, so
            # THIS export's own cell_digest is the reference the T1 control and
            # both external configurations (Neo4j, differential dataflow) must
            # match against -- stated here rather than left implicit.
            index[d.name]["reference_role"] = (
                "no committed grid row; this cell's own cell_digest/files sha256 "
                "(above) is the reference digest for the T1 same-host control and "
                "for both P-EXT1/P-EXT2 external configurations")
    out_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
    n_new = sum(1 for v in index.values() if v["equality_level"] == "new cell (no committed digest)")
    print(f"wrote {out_path} ({len(index)} cells, {n_new} new/Addendum-A2 cells with no "
         f"committed reference)")
    return 0


def cmd_host_snapshot(args: argparse.Namespace) -> int:
    now = subprocess.run(["date", "-u"], capture_output=True, text=True,
                        check=False).stdout.strip()
    uptime = subprocess.run(["uptime"], capture_output=True, text=True,
                           check=False).stdout.strip()
    free_g = subprocess.run(["free", "-g"], capture_output=True, text=True,
                           check=False).stdout.strip()
    block = f"=== {args.label} {now} ===\nuptime: {uptime}\nfree -g:\n{free_g}\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "a") as f:
        f.write(block)
    print(block)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list-cells", help="print every known cell id")
    p_list.add_argument("--extra-cells", type=Path, default=None)
    p_list.add_argument("--grid-root", type=Path, default=ROOT,
                        help="checkout to read benchmarks/storm-v1/ from (default: this "
                             "script's own ROOT); separate from ROOT because a worktree "
                             "pinned at the grid's harness commit (fdd393c) predates the "
                             "commit that landed the grid files on main")
    p_list.set_defaults(fn=cmd_list_cells)

    p_export = sub.add_parser("export", help="export one or more cells")
    p_export.add_argument("--cells", default="all",
                          help="comma-separated cell ids, or 'all' (default)")
    p_export.add_argument("--stores-dir", type=Path, default=ROOT / "stores")
    p_export.add_argument("--export-root", type=Path, required=True)
    p_export.add_argument("--build-missing-stores", action="store_true",
                          help="rebuild synth-iv-60k fresh (A3) if absent from --stores-dir")
    p_export.add_argument("--extra-cells", type=Path, default=None,
                          help="JSON file of Addendum-A2-style cells, not added by default")
    p_export.add_argument("--grid-root", type=Path, default=ROOT,
                          help="checkout to read benchmarks/storm-v1/ from; see list-cells")
    p_export.add_argument("--max-attempts-factor", type=int, default=4)
    p_export.set_defaults(fn=cmd_export)

    p_agg = sub.add_parser("aggregate", help="rebuild INDEX.json from every cell's own files")
    p_agg.add_argument("--export-root", type=Path, required=True)
    p_agg.add_argument("--out", type=Path, default=None)
    p_agg.set_defaults(fn=cmd_aggregate)

    p_host = sub.add_parser("host-snapshot", help="append uptime/free -g to a log file")
    p_host.add_argument("--out", type=Path, required=True)
    p_host.add_argument("--label", required=True)
    p_host.set_defaults(fn=cmd_host_snapshot)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
