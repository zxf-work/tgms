#!/usr/bin/env python3
"""Lane C1 — assemble the external-baseline campaign records.

Three campaigns, two input shapes:

**`neo4j-recompute` / `ivm-differential`** read, per cell: the
configuration's own `result.json` (neo4j-recompute or ivm-dd), lane C1's
own `check.json` (`scripts/external_check.py`'s output — the independent
oracle-agreement tally), and the export bundle's `digests.json`
(workload-equality level, `scripts/export_storm_workload.py`) and
`deltas.jsonl` (per-burst `generator`, used only to classify a burst into
an age band — memo §4.1/§3.5/A5/A6). The TGMS side of every EXT1/EXT2
ratio comes from the **committed storm-v2 rows**
(`benchmarks/storm-v1/storm-v2-main-grid-2026-09-15.json`'s `per_cell` +
the probe's own file), read by `load_storm_v2_committed` — the iTiger
cluster numbers, not a same-host measurement. A cell with no committed
storm-v2 row gets `null` predictions with a note saying why, never a
guessed/omitted-silently value. (Reading note A11: on xzgpu the correct
*same-host* reference for every cell is lane T1's `tgms-control` record
below, not this cluster lookup; scoring that substitution is the paper
macro generator's job, not this script's — this script's own ratios stay
against the committed cluster rows, exactly as built and tested.)

**`tgms-control`** (lane T1, Addendum EXT-A A1) reads, per cell: the
same-host harness's own result manifest (`storm-<store>-<task>.json`,
`bench_correction_storm.py`'s normal output — `config`, `summary.arms`,
`machine`, `git_commit`) and lane C1's `t1-equality.json` (the
byte-identical-eventlog check against the matching export bundle). No
check.json, no committed-grid lookup, no EXT1/EXT2 predictions — this
campaign *is* the same-host reference the other two are read against.

Every one of the three writes two files, mirroring
`benchmarks/storm-v1/storm-v2-main-grid-2026-09-15.json` + `-rows.jsonl`'s
own split between a lean top-level summary and a per-cell-detail rows
file:

  `benchmarks/external-v1/neo4j-recompute-<date>.json`   (campaign="neo4j-recompute")
  `benchmarks/external-v1/ivm-differential-<date>.json`  (campaign="ivm-differential")
  `benchmarks/external-v1/tgms-control-<date>.json`      (campaign="tgms-control")
  ...-<date>-rows.jsonl                                   (one line per cell, full detail)

Every number in `summary.predictions_measured` (first two campaigns) or
`summary.per_cell` (all three) is computed from the input files at run
time — never typed.

As of 2026-10-08 all three campaigns have real data to assemble (lanes
N1/D1: 43 cells each against the export bundle at
`/mnt/project/xzhang/tgms/external-v1/` on xzgpu; lane T1: 19 same-host
control cells) — see `benchmarks/external-v1/README.md` for the landed
run. Tested throughout with synthetic fixtures
(`tests/test_external_record.py`), so a later re-run against a wider grid
is a CLI invocation, not new code.
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"

ROOT = Path(__file__).resolve().parent.parent
STORM_V2_MAIN_GRID = ROOT / "benchmarks" / "storm-v1" / "storm-v2-main-grid-2026-09-15.json"
STORM_V2_PROBE = ROOT / "benchmarks" / "storm-v1" / "storm-v2-r18-probe-2026-09-15.json"


# ---------------------------------------------------------------------------
# cell identity (same convention as `scripts/export_storm_workload.py`'s
# `cell_id` — restated rather than imported, since that module is a CLI
# script, not a library this one should import internals from)
# ---------------------------------------------------------------------------

def cell_id(store: str, mix: str, age: str | None, n_artifacts: int, seed: int) -> str:
    age_token = age if age else "none"
    return f"{store}-{mix}-{age_token}-n{n_artifacts}-s{seed}"


# ---------------------------------------------------------------------------
# TGMS's committed numbers (storm-v2 rows — see module docstring)
# ---------------------------------------------------------------------------

def load_storm_v2_committed(main_grid_path: Path = STORM_V2_MAIN_GRID,
                            probe_path: Path | None = STORM_V2_PROBE) -> dict[str, dict]:
    """`{cell_id: {"arms": {<arm>: {"ttf_p50_ms", "false_fresh", "false_stale", ...}}, "source": <relpath>}}`
    from the committed 36-cell main grid's lean `per_cell` list plus the
    N=10,000 probe's own file (same `summary.arms` shape as a `per_cell`
    entry). Neither file is mutated; both are read-only committed records.
    """
    out: dict[str, dict] = {}
    if main_grid_path.exists():
        d = json.loads(main_grid_path.read_text())
        for row in d["per_cell"]:
            cid = cell_id(row["store"], row["mix"], row["age"], row["n_artifacts"], row["seed"])
            out[cid] = {"arms": row["arms"], "source": _relpath(main_grid_path)}
    if probe_path is not None and probe_path.exists():
        p = json.loads(probe_path.read_text())
        c = p["config"]
        cid = cell_id(c["store"], c["mix"], c["age"], c["n_artifacts"], c["seed"])
        out[cid] = {"arms": p["summary"]["arms"], "source": _relpath(probe_path)}
    return out


def _relpath(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


# ---------------------------------------------------------------------------
# age-band classification (memo §3.5/§4.1, reading notes A5/A6)
# ---------------------------------------------------------------------------

def band_for_burst(generator: str, mix: str) -> str | None:
    """`None` for a burst outside the age-band predictions' scope (memo
    A5: "age bands are scored per burst from the age-labelled and c4
    bursts", not per cell — a `c3`/`none` cell's ordinary class-A/B/C
    bursts are never age-banded and never counted here, matching the one
    real cell on hand being explicitly a calibration, not a scored one).
    `age_<band>` generators map directly; `c4_burst` (the mix-c4 deep
    correction, A5) counts as the "deep" band only on a `c4` cell —
    elsewhere a same-named generator (if one ever exists) would not mean
    the same thing, so the mix is checked too."""
    if generator.startswith("age_"):
        return generator[len("age_"):]
    if generator == "c4_burst" and mix == "c4":
        return "deep"
    return None


def load_burst_generators(export_dir: Path) -> dict[int, str]:
    """epoch -> generator, from the export's `deltas.jsonl` (memo §1.3) —
    read independently of whatever a configuration's own `result.json`
    may or may not have recorded for the same field, so a transcription
    slip in either writer cannot silently relabel a burst's band."""
    out: dict[int, str] = {}
    path = export_dir / "deltas.jsonl"
    if not path.exists():
        return out
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            out[row["epoch"]] = row["generator"]
    return out


# ---------------------------------------------------------------------------
# per-cell input bundle
# ---------------------------------------------------------------------------

class CellInput:
    """Everything this script reads for one cell."""

    def __init__(self, export_dir: Path, result_path: Path, check_path: Path):
        self.export_dir = export_dir
        self.result = json.loads(result_path.read_text())
        self.check = json.loads(check_path.read_text())
        digests_path = export_dir / "digests.json"
        self.digests = json.loads(digests_path.read_text()) if digests_path.exists() else {}
        manifest_path = export_dir / "export-manifest.json"
        self.manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        self.generators = load_burst_generators(export_dir)

        self.cell_id = self.check.get("cell_id") or self.manifest.get("cell_id", export_dir.name)
        self.config_kind = self.check["config_kind"]
        self.mix = self.manifest.get("config", {}).get("mix", "")
        self.age = self.manifest.get("config", {}).get("age")
        self.equality_level = self.digests.get("equality_level", "unknown")

    # -- timing, per the memo's scored quantity for each system --------
    def per_burst_timing_ms(self) -> dict[int, float]:
        """epoch -> the memo's scored refresh-wall quantity: `recompute_ms`
        for neo4j (§2.5), `refresh_plus_publish_ms` for ivm-dd (§3.4).
        Epoch 0 (the untimed warm-up/load pass) is excluded, matching both
        writers' own `*_median` computations (neo4j's over `rows[1:]`,
        ivm-dd's over `outcome.bursts`, which never includes epoch 0)."""
        out: dict[int, float] = {}
        if self.config_kind == "neo4j":
            for row in self.result["per_cell"]["per_burst"]:
                out[row["epoch"]] = row["recompute_ms"]
        else:
            for row in self.result["per_burst"]:
                out[row["epoch"]] = row["refresh_plus_publish_ms"]
        return out

    def artifacts_touched(self) -> dict[int, int]:
        """epoch -> how many artifacts this burst's pass touched: every
        registered artifact for neo4j (it always recomputes all of them,
        memo §2.5's "every registered artifact's query... serially"), or
        `artifacts_refreshed_count` for ivm-dd (memo §3.4)."""
        out: dict[int, int] = {}
        if self.config_kind == "neo4j":
            n = self.check["n_registered"]
            for row in self.result["per_cell"]["per_burst"]:
                out[row["epoch"]] = n
        else:
            for row in self.result["per_burst"]:
                out[row["epoch"]] = row["artifacts_refreshed_count"]
        return out

    def versions(self) -> dict[str, Any]:
        if self.config_kind == "neo4j":
            cfg = self.result.get("config", {})
            return {k: cfg[k] for k in ("neo4j_version", "apoc_version", "jdk_version",
                                        "driver_version") if k in cfg}
        return self.result.get("versions", {})

    def host(self) -> dict[str, Any]:
        if self.config_kind == "neo4j":
            return self.result.get("machine", {})
        snaps = self.result.get("host_snapshots", [])
        return snaps[-1] if snaps else {}


# ---------------------------------------------------------------------------
# tgms-control (lane T1, Addendum EXT-A A1) -- the same-host TGMS control
# ---------------------------------------------------------------------------

#: the harness's own per-arm fields this record restates verbatim, never
#: recomputed -- "per-configuration refresh/recompute medians as the
#: harness names them" means exactly these keys, read from each cell's
#: `summary.arms.<arm>` (bench_correction_storm.py's own output), not a
#: field this script invents.
T1_ARM_FIELDS = ("ttf_p50_ms", "ttf_p95_ms", "false_fresh", "false_stale",
                 "avoided_recompute_wall", "avoided_recompute_decision")

_LOADAVG1_RE = re.compile(r"load average:\s*([\d.]+)")


def parse_loadavg1(text: str) -> float | None:
    """The first (1-minute) figure from a `host-snapshot-*.txt`'s `uptime`
    line, e.g. "load average: 1.03, 1.07, 1.08" -> 1.03. `None` if the
    snapshot has no such line (never guessed)."""
    m = _LOADAVG1_RE.search(text)
    return float(m.group(1)) if m else None


def load_t1_equality(t1_cell_dir: Path) -> dict[str, Any]:
    path = t1_cell_dir / "t1-equality.json"
    return json.loads(path.read_text()) if path.exists() else {}


def find_t1_manifest(t1_cell_dir: Path) -> Path:
    """The cell's own `storm-<store>-<task>.json` harness manifest --
    there is exactly one per cell directory, named for the store
    (`storm-collegemsg-0.json`, `storm-synth-iv-60k-0.json`), distinct
    from its own `-rows.jsonl` sibling."""
    candidates = sorted(p for p in t1_cell_dir.glob("storm-*.json")
                        if not p.name.endswith("-rows.jsonl"))
    if not candidates:
        raise ValueError(f"{t1_cell_dir}: no storm-*.json harness manifest found")
    if len(candidates) > 1:
        raise ValueError(f"{t1_cell_dir}: more than one storm-*.json manifest "
                         f"({[c.name for c in candidates]}) -- ambiguous")
    return candidates[0]


class T1CellInput:
    """One same-host TGMS control cell (lane T1): the harness's own result
    manifest (`storm-<store>-<task>.json`, same shape as a committed
    storm-v2 `per_cell` row -- `config`, `summary.arms`, `machine`,
    `git_commit`, ...) plus lane C1's own `t1-equality.json` (the
    byte-identical-eventlog check against the matching export bundle).
    The manifest's own `-rows.jsonl` (per-batch detail) was not read here
    originally -- the T1 (2026-10-05) cells' `summary.arms` medians and
    `t1-equality.json` covered everything the record needed at the time.
    As of the fifth-arc re-measurement (Addendum ARC5-B, 2026-10-08),
    `summary.arms` has no `e2e_refresh_calls`/`check_cache`/
    `check_cache_misses` field at all -- those were added to `ArmOutcome`/
    `BatchResult` (`tgms/eval/storm.py`) as *per-batch* fields, never
    folded into the manifest's own per-cell medians -- so a cell's
    `-rows.jsonl` sidecar is now read too, via `load_batch_rows` below,
    whenever it sits alongside the manifest. A cell without that sidecar
    (e.g. a T1 cell directory that never had its per-batch rows pulled to
    this host) gets explicit `None`/`{}` aggregates, never a guessed
    value -- see `e2e_and_check_cache_aggregates`."""

    def __init__(self, t1_cell_dir: Path):
        self.t1_cell_dir = t1_cell_dir
        manifest_path = find_t1_manifest(t1_cell_dir)
        self.manifest = json.loads(manifest_path.read_text())
        self.manifest_path = manifest_path
        self.rows_path = manifest_path.with_name(manifest_path.stem + "-rows.jsonl")
        self.equality = load_t1_equality(t1_cell_dir)

        cfg = self.manifest.get("config", {})
        store = cfg.get("store", "")
        # the manifest's own `config.store` is a store *path* on xzgpu
        # (t1-stores/<name>), not the bare name `cell_id` uses -- take the
        # last path component, matching `export_storm_workload.py`'s own
        # `cell_id` convention (module docstring).
        self.store_name = store.rsplit("/", 1)[-1] if store else store
        self.mix = cfg.get("mix", "")
        self.age = cfg.get("age")
        self.n_artifacts = cfg.get("n_artifacts")
        self.seed = cfg.get("seed", 0)
        self.cell_id = self.equality.get("cell_id") or cell_id(
            self.store_name, self.mix, self.age, self.n_artifacts, self.seed)
        self.git_commit = self.manifest.get("git_commit") or self.equality.get("git_commit")

    def host_load(self, label: str) -> float | None:
        path = self.t1_cell_dir / f"host-snapshot-{label}.txt"
        return parse_loadavg1(path.read_text()) if path.exists() else None

    def load_batch_rows(self) -> list[dict[str, Any]]:
        """This cell's own raw per-batch rows (`storm-<store>-<task>-
        rows.jsonl`, `BatchResult.to_json()`'s shape) -- `[]` when the
        sidecar is not present next to the manifest, never an error (the
        2026-10-05 T1 cells were summarized from the manifest alone, with
        no per-batch sidecar on this host; a cell that genuinely lacks one
        must get empty/`None` aggregates, not a crash)."""
        if not self.rows_path.exists():
            return []
        return [json.loads(line) for line in self.rows_path.open() if line.strip()]


#: the per-batch fields `BatchResult`/`ArmOutcome` added after the T1
#: (2026-10-05) control landed -- never in the manifest's own
#: `summary.arms` medians (module docstring on `T1CellInput`), so they are
#: computed here from each cell's own `-rows.jsonl`, by median over
#: batches, exactly like every other per-batch aggregate in this file
#: (e.g. `CellInput.per_burst_timing_ms`'s median in `summarize_cell`).
def e2e_and_check_cache_aggregates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """`{"check_cache": ..., "check_cache_misses_median": ...,
    "e2e_refresh_calls_median": {<arm>: ...}}` from a cell's raw per-batch
    rows. `check_cache` is the batches' own setting (`"none"` if every row
    omits the field, matching `BatchResult.check_cache`'s own default);
    `check_cache_misses_median` is `None` when no batch recorded a miss
    count (sum-mode-style `"none"` caching never writes the field, per
    `BatchResult.to_json`). `e2e_refresh_calls_median` only has an entry
    for an arm that recorded the field on at least one batch (end-to-end
    TTF mode only, per `ArmOutcome.to_json`) -- an arm with no entry here
    simply never measured it, not a zero. `rows=[]` (no sidecar found)
    returns the same shape with every value `None`/`{}`, never guessed."""
    if not rows:
        return {"check_cache": None, "check_cache_misses_median": None,
                "e2e_refresh_calls_median": {}}
    check_cache_values = sorted({r.get("check_cache", "none") for r in rows})
    check_cache = check_cache_values[0] if len(check_cache_values) == 1 else check_cache_values
    misses = [r["check_cache_misses"] for r in rows if r.get("check_cache_misses") is not None]
    per_arm_calls: dict[str, list[float]] = {}
    for r in rows:
        for arm_name, arm in r.get("arms", {}).items():
            v = arm.get("e2e_refresh_calls")
            if v is not None:
                per_arm_calls.setdefault(arm_name, []).append(v)
    return {
        "check_cache": check_cache,
        "check_cache_misses_median": statistics.median(misses) if misses else None,
        "e2e_refresh_calls_median": {a: statistics.median(vs) for a, vs in per_arm_calls.items()},
    }


def summarize_t1_cell(ci: T1CellInput) -> dict[str, Any]:
    arms = ci.manifest.get("summary", {}).get("arms", {})
    gr_p50 = arms.get("global-recompute", {}).get("ttf_p50_ms")
    l1_p50 = arms.get("tgms-L1", {}).get("ttf_p50_ms")
    speedup = (gr_p50 / l1_p50) if (gr_p50 is not None and l1_p50) else None
    l1_match = ci.equality.get("l1_eventlog_sha_match")
    equality_level = "L1" if l1_match is True else (
        "unknown" if l1_match is None else "no-L1 (eventlog sha mismatch)")
    agg = e2e_and_check_cache_aggregates(ci.load_batch_rows())
    arms_out = {name: {k: v for k, v in arm.items() if k in T1_ARM_FIELDS}
               for name, arm in arms.items()}
    for arm_name, median_calls in agg["e2e_refresh_calls_median"].items():
        if arm_name in arms_out:
            arms_out[arm_name]["e2e_refresh_calls_median"] = median_calls
    return {
        "cell_id": ci.cell_id,
        "store": ci.store_name, "mix": ci.mix, "age": ci.age,
        "n_artifacts": ci.n_artifacts, "seed": ci.seed,
        "wall_s": ci.manifest.get("config", {}).get("wall_s"),
        "batches_realized": ci.equality.get("batches_realized"),
        "batches_requested": ci.manifest.get("config", {}).get("batches"),
        "equality_level": equality_level,
        "arms": arms_out,
        "speedup_global_recompute_over_l1": speedup,
        "host_load": {"before": ci.host_load("before"), "after": ci.host_load("after")},
        "check_cache": agg["check_cache"],
        "check_cache_misses_median": agg["check_cache_misses_median"],
    }


def full_t1_cell_row(ci: T1CellInput) -> dict[str, Any]:
    """The detailed per-cell row -- the harness's own manifest (minus its
    own per-batch `-rows.jsonl`, which stays wherever that manifest's
    `record` field already points) plus lane C1's `t1-equality.json`, so a
    reader never needs the xzgpu scratch tree to audit one cell."""
    return {
        "cell_id": ci.cell_id,
        "manifest_path": _relpath_or_str(ci.manifest_path),
        "manifest": ci.manifest,
        "t1_equality": ci.equality,
    }


def _relpath_or_str(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def build_t1_record(cells: list[T1CellInput], *, git_commit: str,
                    timestamp_utc: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    per_cell_summary = [summarize_t1_cell(ci) for ci in cells]
    rows = [full_t1_cell_row(ci) for ci in cells]

    eventlog_shas = sorted(ci.equality.get("t1_eventlog_sha256", "") for ci in cells)
    machine = cells[0].manifest.get("machine", {}) if cells else {}

    record = {
        "schema_version": SCHEMA_VERSION,
        "git_commit": git_commit,
        "timestamp_utc": timestamp_utc,
        "machine": machine,
        "config": {"campaign": "tgms-control", "cell_ids": [ci.cell_id for ci in cells]},
        "seed": {"value": None, "reason": "cell identity carries the seed; see cell_id "
                                         "per row"},
        "dataset": {"name": "storm-v2 grid (TGMS same-host control, xzgpu, lane T1)",
                   "digest": _sha256_of(eventlog_shas),
                   "digest_kind": "eventlog_sha"},
        "result_digest": _sha256_of([ci.cell_id for ci in cells]),
        "protocol": {"warmups": 0, "reps": 1,
                    "ceilings": {"note": "per-cell ceilings are in each row's own manifest"}},
        "record": "benchmarks/external-v1/tgms-control-rows.jsonl (date-stamped per run)",
        "n_cells": len(cells),
        "summary": {"per_cell": per_cell_summary},
    }
    return record, rows


# ---------------------------------------------------------------------------
# per-cell summary row (the lean `summary.per_cell` entry)
# ---------------------------------------------------------------------------

def summarize_cell(ci: CellInput) -> dict[str, Any]:
    timing = ci.per_burst_timing_ms()
    values = [timing[e] for e in sorted(timing)]
    touched = ci.artifacts_touched()
    totals = ci.check["totals"]
    return {
        "cell_id": ci.cell_id,
        "config_kind": ci.config_kind,
        "equality_level": ci.equality_level,
        "n_bursts": len(values),
        "refresh_wall_ms": {
            "median": statistics.median(values) if values else None,
            "max": max(values) if values else None,
            "sum": sum(values) if values else None,
        },
        "artifacts_touched_per_burst": {
            "median": statistics.median(touched.values()) if touched else None,
            "all_registered": ci.config_kind == "neo4j",
        },
        "oracle_agreement": {
            "agree": totals["agree"], "disagree": totals["disagree"],
            "not_answered": totals["not_answered"], "oracle_refused": totals["oracle_refused"],
            "n_compared": totals["n_compared"],
        },
        "false_fresh": totals["false_fresh"],
        "false_stale": totals["false_stale"],
        "host_load": ci.host().get("loadavg1"),
        "versions": ci.versions(),
    }


def full_cell_row(ci: CellInput) -> dict[str, Any]:
    """The detailed per-cell row written to `-rows.jsonl` — `result.json`
    and `check.json` merged with the export's equality level, so a reader
    never needs the export bundle itself to audit one cell."""
    return {
        "cell_id": ci.cell_id,
        "config_kind": ci.config_kind,
        "equality_level": ci.equality_level,
        "cell_digest": ci.manifest.get("cell_digest"),
        "mix": ci.mix, "age": ci.age,
        "export_manifest_verified": ci.check.get("export_manifest_verified"),
        "result": ci.result,
        "check": ci.check,
    }


# ---------------------------------------------------------------------------
# predictions (memo §4.2 -> measured fields)
# ---------------------------------------------------------------------------

def ext1_predictions(cells: list[CellInput], committed: dict[str, dict]) -> dict[str, Any]:
    """EXT1 (a)/(b)/(c)/(d) — Neo4j cells only. (c)'s probe cell is
    identified by `n_artifacts==10000` in the export manifest, matching
    the memo's own N=10,000 probe."""
    per_cell: dict[str, Any] = {}
    ratios_gr: list[float] = []
    speedups: list[float] = []
    probe_ratio = None
    disagree_total = 0

    for ci in cells:
        if ci.config_kind != "neo4j":
            continue
        timing = ci.per_burst_timing_ms()
        values = [timing[e] for e in sorted(timing)]
        if not values:
            continue
        median_recompute_ms = statistics.median(values)
        disagree_total += ci.check["totals"]["disagree"]

        row = committed.get(ci.cell_id)
        n_artifacts = ci.manifest.get("config", {}).get("n_artifacts")
        if row is None:
            per_cell[ci.cell_id] = {"note": "no committed storm-v2 row for this cell "
                                            "(needs lane T1 or the main grid) -- "
                                            "ratio_gr/speedup left null",
                                    "ratio_gr": None, "speedup_l1": None}
            continue
        gr = row["arms"].get("global-recompute", {}).get("ttf_p50_ms")
        l1 = row["arms"].get("tgms-L1", {}).get("ttf_p50_ms")
        ratio_gr = (median_recompute_ms / gr) if gr else None
        speedup = (median_recompute_ms / l1) if l1 else None
        per_cell[ci.cell_id] = {"ratio_gr": ratio_gr, "speedup_l1": speedup,
                               "committed_source": row["source"]}
        if ratio_gr is not None:
            ratios_gr.append(ratio_gr)
        if speedup is not None:
            speedups.append(speedup)
        if n_artifacts == 10000:
            probe_ratio = ratio_gr

    return {
        "a_ratio_gr_median": statistics.median(ratios_gr) if ratios_gr else None,
        "a_ratio_gr_min": min(ratios_gr) if ratios_gr else None,
        "a_ratio_gr_max": max(ratios_gr) if ratios_gr else None,
        "a_n_cells": len(ratios_gr),
        "b_speedup_median": statistics.median(speedups) if speedups else None,
        "b_speedup_min": min(speedups) if speedups else None,
        "b_n_cells_meeting_half_margin": sum(
            1 for s in speedups if s >= 2.586  # 0.5 x recStormV2SpeedupN1kSeed0, memo A7
        ),
        "b_n_cells": len(speedups),
        "c_probe_ratio_gr": probe_ratio,
        "d_disagree_total": disagree_total,
        "d_pass": disagree_total == 0 and len(cells) > 0,
        "per_cell": per_cell,
    }


def ext2_predictions(cells: list[CellInput], committed: dict[str, dict],
                     withheld: dict[str, Any] | None) -> dict[str, Any]:
    """EXT2 (a)/(b)/(d) from the ivm-dd cells' age-labelled/c4 bursts
    (memo A5/A6), plus (c) from the withheld-cell check if one was passed
    on the command line."""
    band_ivm_ms: dict[str, list[float]] = {}
    band_l1_ms: dict[str, list[float]] = {}
    disagree_total = 0

    for ci in cells:
        if ci.config_kind != "ivm-dd":
            continue
        disagree_total += ci.check["totals"]["disagree"]
        timing = ci.per_burst_timing_ms()
        row = committed.get(ci.cell_id)
        for epoch, generator in ci.generators.items():
            band = band_for_burst(generator, ci.mix)
            if band is None or epoch not in timing:
                continue
            band_ivm_ms.setdefault(band, []).append(timing[epoch])
            if row is not None:
                l1 = row["arms"].get("tgms-L1", {}).get("ttf_p50_ms")
                if l1 is not None:
                    band_l1_ms.setdefault(band, []).append(l1)

    bands = ["recent", "hours", "days", "deep"]
    per_band: dict[str, Any] = {}
    ratios: dict[str, float | None] = {}
    for band in bands:
        ivm_vals = band_ivm_ms.get(band, [])
        l1_vals = band_l1_ms.get(band, [])
        ivm_med = statistics.median(ivm_vals) if ivm_vals else None
        l1_med = statistics.median(l1_vals) if l1_vals else None
        ratio = (ivm_med / l1_med) if (ivm_med is not None and l1_med) else None
        ratios[band] = ratio
        per_band[band] = {
            "n_bursts_ivm": len(ivm_vals), "ivm_median_ms": ivm_med,
            "n_cells_with_committed_l1": len(l1_vals), "tgms_l1_median_ms": l1_med,
            "ratio_ivm_over_l1": ratio,
            "note": None if (ivm_vals and l1_vals) else (
                "no ivm-dd bursts in this band yet" if not ivm_vals else
                "no committed TGMS tgms-L1 number for a cell in this band -- "
                "needs lane T1 (Addendum EXT-A A2) for recent/hours/days, or "
                "the committed c3/c4 deep cells for deep"),
        }

    crossover = next((b for b in bands if ratios.get(b) is not None and ratios[b] > 1), None)

    out = {
        "a_b_per_band": per_band,
        "b_crossover_band": crossover,
        "d_disagree_total": disagree_total,
        "d_pass": disagree_total == 0 and len(cells) > 0,
        "c_withheld": withheld,
    }
    return out


# ---------------------------------------------------------------------------
# record assembly
# ---------------------------------------------------------------------------

def build_record(campaign: str, cells: list[CellInput], *, git_commit: str,
                 timestamp_utc: str, committed: dict[str, dict],
                 withheld: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Assembles a `neo4j-recompute` or `ivm-differential` record. For
    `tgms-control` (lane T1, a different cell-input shape with no
    committed-grid lookup or EXT1/EXT2 predictions), see `build_t1_record`
    instead -- kept as a separate function rather than overloaded into
    this one so neither signature grows optional parameters the other
    campaign never uses."""
    if campaign not in ("neo4j-recompute", "ivm-differential"):
        raise ValueError(f"unknown campaign {campaign!r}")

    per_cell_summary = [summarize_cell(ci) for ci in cells]
    rows = [full_cell_row(ci) for ci in cells]

    if campaign == "neo4j-recompute":
        predictions = {"ext1": ext1_predictions(cells, committed)}
        dataset_name = "storm-v2 grid (Neo4j 5.26 recompute)"
    else:
        predictions = {"ext2": ext2_predictions(cells, committed, withheld)}
        dataset_name = "storm-v2 grid (differential-dataflow IVM)"

    cell_digests = sorted(ci.manifest.get("cell_digest", "") for ci in cells)
    record = {
        "schema_version": SCHEMA_VERSION,
        "git_commit": git_commit,
        "timestamp_utc": timestamp_utc,
        "machine": (cells[0].host() if cells and cells[0].config_kind == "neo4j"
                   else {"host": "xzgpu", "platform": "unknown", "cpus": 1, "ram_gb": 1.0,
                        "note": "placeholder -- filled from the per-cell host snapshot "
                                "when the campaign's own machine field differs by cell"}),
        "config": {"campaign": campaign, "cell_ids": [ci.cell_id for ci in cells]},
        "seed": {"value": None, "reason": "cell identity carries the seed; see cell_id "
                                         "per row"},
        "dataset": {"name": dataset_name,
                   "digest": _sha256_of(cell_digests),
                   "digest_kind": "manifest"},
        "result_digest": _sha256_of([ci.cell_id for ci in cells]),
        "protocol": {"warmups": 1, "reps": 1,
                    "ceilings": {"note": "per-cell ceilings are in each row's own result"}},
        "record": f"benchmarks/external-v1/{campaign}-rows.jsonl (date-stamped per run)",
        "n_cells": len(cells),
        "summary": {
            "per_cell": per_cell_summary,
            "predictions_measured": predictions,
        },
    }
    return record, rows


def _sha256_of(obj: Any) -> str:
    import hashlib
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def write_record(record: dict[str, Any], rows: list[dict[str, Any]], out_dir: Path,
                 campaign: str, date: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    record_path = out_dir / f"{campaign}-{date}.json"
    rows_path = out_dir / f"{campaign}-{date}-rows.jsonl"
    record = dict(record)
    record["record"] = str(rows_path.relative_to(ROOT)) if rows_path.is_relative_to(ROOT) \
        else str(rows_path)
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    with rows_path.open("w") as f:
        for row in rows:
            f.write(json.dumps(row, sort_keys=True) + "\n")
    return record_path, rows_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--campaign", required=True,
                    choices=["neo4j-recompute", "ivm-differential", "tgms-control"])
    ap.add_argument("--cell", nargs=3, action="append", default=[],
                    metavar=("EXPORT_DIR", "RESULT_JSON", "CHECK_JSON"),
                    help="one exported cell's bundle dir, its configuration's "
                        "result.json, and lane C1's check.json; repeatable "
                        "(neo4j-recompute / ivm-differential only)")
    ap.add_argument("--t1-cell", action="append", default=[], type=Path,
                    metavar="T1_CELL_DIR",
                    help="one lane-T1 same-host-control cell directory "
                        "(contains storm-<store>-<task>.json + "
                        "t1-equality.json); repeatable (tgms-control only)")
    ap.add_argument("--withheld-check", type=Path, default=None,
                    help="check.json's \"withheld\" section (from "
                        "external_check.py --withheld-result ...), for EXT2 (c)")
    ap.add_argument("--git-commit", default="unknown")
    ap.add_argument("--timestamp-utc", default=None, help="default: now, UTC")
    ap.add_argument("--out-dir", type=Path, default=ROOT / "benchmarks" / "external-v1")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD, default: today (UTC)")
    ap.add_argument("--storm-v2-main-grid", type=Path, default=STORM_V2_MAIN_GRID)
    ap.add_argument("--storm-v2-probe", type=Path, default=STORM_V2_PROBE)
    args = ap.parse_args(argv)

    import datetime
    now = datetime.datetime.now(datetime.UTC)
    timestamp_utc = args.timestamp_utc or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    date = args.date or now.strftime("%Y-%m-%d")

    if args.campaign == "tgms-control":
        if args.cell:
            ap.error("--cell is for neo4j-recompute/ivm-differential; use --t1-cell")
        t1_cells = [T1CellInput(d) for d in args.t1_cell]
        record, rows = build_t1_record(t1_cells, git_commit=args.git_commit,
                                       timestamp_utc=timestamp_utc)
    else:
        if args.t1_cell:
            ap.error("--t1-cell is for tgms-control; use --cell")
        cells = [CellInput(Path(e), Path(r), Path(c)) for e, r, c in args.cell]
        committed = load_storm_v2_committed(args.storm_v2_main_grid, args.storm_v2_probe)
        withheld = json.loads(args.withheld_check.read_text()) if args.withheld_check else None

        record, rows = build_record(args.campaign, cells, git_commit=args.git_commit,
                                    timestamp_utc=timestamp_utc, committed=committed,
                                    withheld=withheld)
    record_path, rows_path = write_record(record, rows, args.out_dir, args.campaign, date)
    print(f"wrote {record_path} ({len(rows)} cell rows -> {rows_path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
