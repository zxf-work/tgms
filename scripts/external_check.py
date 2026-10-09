#!/usr/bin/env python3
"""Lane C1 — the external-baseline oracle checker.

For one exported cell (the bundle `scripts/export_storm_workload.py`
writes: `oracle.jsonl`, `artifacts.jsonl`, `export-manifest.json`, ...) and
one configuration's `result.json` (either `external/neo4j-recompute`'s or
`external/ivm-dd`'s — design memo `EXTERNAL_BASELINES_DESIGN_2026-10-02.md`
§4.1 states both carry the same per-burst quantities under different field
names), this script independently reproduces the oracle-agreement tally
and writes `check.json`.

**Why this is independent, not a re-print of the configuration's own
`gates.oracle_agreement`.** Both writers (`neo4j_recompute.record.build_result`,
`ivm_dd::record::build_result_json`) compute their own agreement count
in-process against whatever digest map they happen to be holding. This
script instead rebuilds the comparison from the export's own ground truth
(`oracle.jsonl`) plus only the configuration's *disagreement/timeout*
reports (the parts that cannot be re-derived without re-running the
configuration) — so a bug in either writer's own bookkeeping (not
excluding a TGMS-oracle-refused name from its comparison, say) does not
silently pass through unchecked. One concrete instance found while
writing this: `ivm_dd::record::oracle_agreement` (src/record.rs) compares
every name present in `oracle.jsonl`'s `digests` map, including names the
same epoch's `refused` list names — it never reads `refused` at all. On
every cell seen so far `refused` is empty (TGMS's own full-recompute
oracle never gave up), so this has not yet produced a wrong number, but it
is a latent bug in that crate worth a ledger entry; this checker does not
patch `record.rs` (out of lane C1's scope) and instead excludes refused
names itself, so a future cell with real refusals is scored correctly
regardless.

**Refusal categories** (memo §2.6/§3.1/§1.5, A8):
  - ``oracle_refused`` — TGMS's own full-recompute oracle pass could not
    produce a ground-truth digest for this artifact at this epoch (budget
    exceeded, etc.; `oracle.jsonl`'s own `refused` list). Excluded from
    the comparison entirely — there is nothing to check an answer against.
  - ``not_expressible`` — the external configuration's query language
    cannot express this operator family at all (a whole-family, scope-time
    removal, not a per-burst event). Not currently populated by either
    writer (13/13 expressible, memo §2.3/§3.3); supported here for
    forward compatibility if a configuration ever lists one.
  - ``not_answered`` — the configuration attempted the artifact this burst
    and did not get an answer in time (Neo4j's `db.transaction.timeout`,
    memo §2.5).
  - ``disagree`` — the configuration answered, but its formatted payload's
    digest differs from the oracle's.
  - ``agree`` — digest equal. (Not listed by name — only the other three
    categories are small enough, and actionable enough, to name; the
    convention both per-cell writers already use.)

**"changed" per burst** is not carried in the export bundle (deltas.jsonl
has no such field — `export_storm_workload.py`'s `run_batch_export`
computes it but only to score workload equality, then drops it). This
script recomputes it the only way that is actually available after the
fact: a running last-known-digest map per artifact (exactly
`ExportStorm.run_batch_export`'s own `ra.last_env`), fed by `oracle.jsonl`
in epoch order; "changed at epoch k" = digest differs from the map's value
going into epoch k. A refused epoch leaves the map unchanged (the exporter
itself never updates `last_env` on a refusal), so this reconstruction is
exact, not an approximation.

**False-fresh / false-stale** (memo §4.1's shared table; the full-recompute
vs. incrementally-maintained distinction, not the withheld-correction
cell's distinct definition in §3.5 — see `check_withheld` for that):
  - full-recompute configuration (``neo4j``): `false_fresh = 0` always (a
    fresh recompute every burst cannot serve a stale value); `false_stale
    = n_compared - n_changed` (artifacts recomputed whose value did not
    actually need it — wasted work, not a correctness defect).
  - incrementally-maintained configuration (``ivm`` / ``ivm-dd``):
    `false_fresh = n_disagree` (the view claims "fresh" once it has
    stepped past the burst's epoch, memo §3.4; any mismatch at that point
    is exactly a stale value served as fresh); `false_stale` is not
    meaningful outside the withheld-correction cell's feeder design (memo
    §3.5) and is reported as `null`.
  Epoch 0 is the warm-up/baseline pass (no correction applied yet, memo
  §2.5/§3.4); it is still scored for oracle agreement (both per-cell
  writers include it in their own totals) but has no "changed" set and so
  no false-fresh/false-stale figure (`null`).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# bundle loading
# ---------------------------------------------------------------------------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def load_oracle_rows(export_dir: Path) -> list[dict]:
    """`oracle.jsonl`, sorted by epoch; validates every row carries the
    same artifact-name set (registration happens once, at epoch 0 — memo
    §1.1 — so this must hold for every real export)."""
    rows = sorted(load_jsonl(export_dir / "oracle.jsonl"), key=lambda r: r["epoch"])
    if not rows:
        raise ValueError(f"{export_dir}/oracle.jsonl: no rows")
    names0 = set(rows[0]["digests"])
    for row in rows[1:]:
        names = set(row["digests"])
        if names != names0:
            raise ValueError(
                f"{export_dir}/oracle.jsonl: epoch {row['epoch']}'s artifact set "
                f"differs from epoch {rows[0]['epoch']}'s (added "
                f"{sorted(names - names0)}, removed {sorted(names0 - names)})")
    return rows


def verify_export_manifest(export_dir: Path) -> tuple[bool, list[str]]:
    """Recompute sha256 of every file the manifest names and compare.
    Returns (verified, problems) — `problems` is empty iff every file
    listed is present and matches; a file this checker doesn't need
    (e.g. `versions-epoch0.jsonl`, large and unused here) is still
    verified if the manifest lists it, since a cheap full check is the
    whole point of "read only cells whose export-manifest.json hashes
    verify"."""
    manifest_path = export_dir / "export-manifest.json"
    if not manifest_path.exists():
        return False, [f"{manifest_path}: missing"]
    manifest = json.loads(manifest_path.read_text())
    files = manifest.get("files", {})
    if not files:
        return False, [f"{manifest_path}: no \"files\" entry to verify against"]
    problems = []
    for name, want in files.items():
        path = export_dir / name
        if not path.exists():
            problems.append(f"{name}: file missing")
            continue
        got = sha256_file(path)
        if got != want:
            problems.append(f"{name}: sha256 mismatch (manifest {want}, on disk {got})")
    return (len(problems) == 0), problems


# ---------------------------------------------------------------------------
# configuration result.json — per-burst extraction (memo §4.1 shared fields)
# ---------------------------------------------------------------------------

@dataclass
class BurstReport:
    """What one burst's result tells us about disagreement/timeout, as far
    as that configuration's own `result.json` can tell us. `*_names` is
    `None` when the writer did not attribute names to epochs (ivm-dd's
    `not_answered`, a bare count) — `*_count` is always known."""
    epoch: int
    disagree_names: set[str] | None
    disagree_count: int
    not_answered_names: set[str] | None
    not_answered_count: int
    oracle_refused_reported: set[str] | None = None  # cross-check only


def detect_config_kind(result: dict) -> str:
    if "per_cell" in result and "cypher" in result.get("config", {}):
        return "neo4j"
    if "per_burst" in result and "routing" in result.get("config", {}):
        return "ivm-dd"
    # fall back on shape alone, in case a future writer renames fields
    if isinstance(result.get("per_cell"), dict) and "per_burst" in result["per_cell"]:
        return "neo4j"
    if isinstance(result.get("per_burst"), list):
        return "ivm-dd"
    raise ValueError("result.json: cannot detect configuration kind (neither "
                     "neo4j-recompute's nor ivm-dd's shape recognized)")


def extract_neo4j_bursts(result: dict) -> dict[int, BurstReport]:
    out: dict[int, BurstReport] = {}
    for row in result["per_cell"]["per_burst"]:
        out[row["epoch"]] = BurstReport(
            epoch=row["epoch"],
            disagree_names=set(row.get("disagree", [])),
            disagree_count=len(row.get("disagree", [])),
            not_answered_names=set(row.get("not_answered", [])),
            not_answered_count=len(row.get("not_answered", [])),
            oracle_refused_reported=set(row.get("oracle_refused", [])),
        )
    return out


def extract_ivmdd_bursts(result: dict) -> dict[int, BurstReport]:
    """ivm-dd's `per_burst` list only carries maintenance bookkeeping
    (`artifacts_refreshed`), not disagreement — that lives in the
    top-level `gates.oracle_agreement.disagree` (a flat list across every
    epoch, each entry carrying its own `epoch`) and `.not_answered` (a
    bare total, memo §4.1 / `ivm_dd::record::build_result_json`). Per-burst
    `not_answered_count` is 0 when the total is 0 (the only way a sum of
    non-negative integers is 0 is if every term is), else `None`
    (unattributable — reported honestly, not guessed)."""
    epochs = [0] + [b["epoch"] for b in result["per_burst"]]
    disagree_by_epoch: dict[int, set[str]] = {e: set() for e in epochs}
    for d in result["gates"]["oracle_agreement"]["disagree"]:
        disagree_by_epoch.setdefault(d["epoch"], set()).add(d["artifact"])
    total_not_answered = result["gates"]["oracle_agreement"]["not_answered"]
    na_count = 0 if total_not_answered == 0 else None
    out: dict[int, BurstReport] = {}
    for e in epochs:
        out[e] = BurstReport(
            epoch=e,
            disagree_names=disagree_by_epoch.get(e, set()),
            disagree_count=len(disagree_by_epoch.get(e, set())),
            not_answered_names=(set() if na_count == 0 else None),
            not_answered_count=(na_count if na_count is not None
                                else (total_not_answered if e == epochs[-1] else 0)),
        )
    # The unattributed total (if any) is parked on the last epoch so sums
    # stay correct; every other epoch's count is the honest 0 it must be
    # when there's nothing left to distribute. See totals() below, which
    # uses the configuration's own grand total directly rather than
    # re-summing these per-epoch placeholders, so this placement never
    # double counts.
    return out


# ---------------------------------------------------------------------------
# oracle-agreement reconstruction
# ---------------------------------------------------------------------------

def check_cell(export_dir: Path, result: dict, *, config_kind: str | None = None,
              require_manifest: bool = True) -> dict[str, Any]:
    manifest_path = export_dir / "export-manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    verified, problems = verify_export_manifest(export_dir)
    if require_manifest and not verified:
        raise ValueError(f"{export_dir}: export-manifest.json does not verify: "
                         f"{'; '.join(problems)}")

    oracle_rows = load_oracle_rows(export_dir)
    names = sorted(oracle_rows[0]["digests"])
    n_registered = len(names)

    kind = config_kind or detect_config_kind(result)
    if kind == "neo4j":
        bursts = extract_neo4j_bursts(result)
    elif kind == "ivm-dd":
        bursts = extract_ivmdd_bursts(result)
    else:
        raise ValueError(f"unknown config_kind {kind!r}")

    per_burst: list[dict[str, Any]] = []
    last_digest: dict[str, str] = {}
    notes: list[str] = list(problems) if problems else []

    totals = dict(n_compared=0, agree=0, disagree=0, not_answered=0,
                  oracle_refused=0, false_fresh=0)
    false_stale_total = 0.0
    false_stale_defined = (kind == "neo4j")

    for row in oracle_rows:
        epoch = row["epoch"]
        digests = row["digests"]
        refused = set(row.get("refused", []))
        compared = set(names) - refused

        b = bursts.get(epoch)
        if b is None:
            raise ValueError(f"result.json has no burst for epoch {epoch} "
                             f"(oracle.jsonl covers epochs "
                             f"{oracle_rows[0]['epoch']}..{oracle_rows[-1]['epoch']})")

        if b.disagree_names is not None:
            disagree = b.disagree_names & compared
            stray = b.disagree_names - compared
        else:
            disagree, stray = set(), set()
        if stray:
            notes.append(f"epoch {epoch}: configuration reported disagree for "
                        f"{sorted(stray)}, which the oracle refused this epoch "
                        f"— excluded from scoring, not double-counted")

        if b.not_answered_names is not None:
            not_answered = b.not_answered_names & compared
            na_count = len(not_answered)
        else:
            not_answered = None
            na_count = b.not_answered_count

        if b.oracle_refused_reported is not None and b.oracle_refused_reported != refused:
            notes.append(f"epoch {epoch}: configuration's own oracle_refused "
                        f"{sorted(b.oracle_refused_reported)} != export's "
                        f"oracle.jsonl refused {sorted(refused)}")

        agree_count = len(compared) - len(disagree) - na_count

        if epoch == oracle_rows[0]["epoch"]:
            changed = None  # baseline pass, no correction applied yet
        else:
            changed = {n for n in compared if digests.get(n) != last_digest.get(n)}

        if changed is None:
            false_fresh = None
            false_stale = None
        elif kind == "neo4j":
            false_fresh = 0
            false_stale = len(compared) - len(changed)
        else:  # ivm-dd / incrementally maintained
            false_fresh = len(disagree) if b.disagree_names is not None else b.disagree_count
            false_stale = None

        per_burst.append({
            "epoch": epoch,
            "n_compared": len(compared),
            "n_oracle_refused": len(refused),
            "oracle_refused": sorted(refused),
            "agree": agree_count,
            "disagree": sorted(disagree),
            "disagree_count": len(disagree),
            "not_answered": sorted(not_answered) if not_answered is not None else None,
            "not_answered_count": na_count,
            "not_answered_attribution": "exact" if b.not_answered_names is not None else "total_only",
            "n_changed": (len(changed) if changed is not None else None),
            "false_fresh": false_fresh,
            "false_stale": false_stale,
        })

        totals["n_compared"] += len(compared)
        totals["agree"] += agree_count
        totals["disagree"] += len(disagree)
        totals["not_answered"] += na_count
        totals["oracle_refused"] += len(refused)
        if false_fresh is not None:
            totals["false_fresh"] += false_fresh
        if false_stale is not None:
            false_stale_total += false_stale

        last_digest.update(digests)

    totals["false_stale"] = false_stale_total if false_stale_defined else None

    return {
        "schema_version": SCHEMA_VERSION,
        "cell_id": manifest.get("cell_id", export_dir.name),
        "cell_digest": manifest.get("cell_digest"),
        "export_manifest_verified": verified,
        "config_kind": kind,
        "n_registered": n_registered,
        "epochs": [r["epoch"] for r in oracle_rows],
        "per_burst": per_burst,
        "totals": totals,
        "notes": notes,
    }


def tally_line(check: dict[str, Any]) -> str:
    t = check["totals"]
    return (f"{check['cell_id']} ({check['config_kind']}): "
           f"{t['agree']}/{t['n_compared']} agree, {t['disagree']} disagree, "
           f"{t['not_answered']} not_answered, {t['oracle_refused']} oracle_refused "
           f"(n_registered={check['n_registered']}, epochs={len(check['epochs'])})")


# ---------------------------------------------------------------------------
# the withheld-correction cell (memo §3.5) — EXT2 (c)
# ---------------------------------------------------------------------------

def check_withheld(withheld_result: dict, *, tgms_false_fresh: int | None = None,
                   tgms_stale_marked: int | None = None) -> dict[str, Any]:
    """Reads `ivm_dd::withheld`'s `withheld-result.json`
    (`{"cell_id", "inter_burst_wall_ms", "feeders": [{"feeder",
    "probe_reports_complete_through_10", "signalled", "false_fresh_count",
    "artifacts", "held_artifacts", "refused_answers", "hold_ms_median",
    "hold_ms_min", "hold_ms_max", "hold_bursts_median", "held"}, ...]}`)
    and restates its two feeders' numbers next to TGMS's own (memo §3.5):
    TGMS's `false_fresh` is 0 by contract and its stale-marked count comes
    from a separate `ext_export.py --check-at 10` run this script does not
    itself have — pass them in if available, else they are reported `null`
    with a note (never fabricated).

    The hold-duration fields (`ivm_f_watermark_held_artifacts` and
    siblings, memo P-EXT2-H frozen 2026-10-09T14:30:27Z) are read with
    `.get(...)`, not direct indexing, so a `withheld-result.json` written
    by the pre-P-EXT2-H binary (no hold fields at all — e.g. the original
    2026-10-07 run) still reads here, those fields simply coming back
    `None`.

    Exercised against real withheld-cell data by lane D-W (2026-10-09, the
    `synth-iv-60k-c4-deep-n1000-s0` withheld-correction cell, hold-duration
    extension); still covered independently by
    `tests/test_external_check.py`'s synthetic fixture, matching
    `withheld.rs`'s own field names exactly.
    """
    feeders = {f["feeder"]: f for f in withheld_result["feeders"]}
    watermark = feeders["F-watermark"]
    out = {
        "cell_id": withheld_result["cell_id"],
        "ivm_f_epoch_false_fresh": feeders["F-epoch"]["false_fresh_count"],
        "ivm_f_epoch_probe_complete_through_10": feeders["F-epoch"]["probe_reports_complete_through_10"],
        "ivm_f_watermark_false_fresh": watermark["false_fresh_count"],
        "ivm_f_watermark_probe_complete_through_10": watermark["probe_reports_complete_through_10"],
        "ivm_f_watermark_unanswerable": not watermark["probe_reports_complete_through_10"],
        # Hold-duration fields (memo P-EXT2-H, frozen 2026-10-09T14:30:27Z,
        # `ivm_dd::withheld::compute_hold` / `cmd_withheld`). `.get(...)`
        # rather than direct indexing: a `withheld-result.json` written by
        # the pre-P-EXT2-H binary (e.g. the original 2026-10-07 run) has
        # none of these keys, and this reader stays usable against that
        # older shape too, surfacing the gap as `None` rather than raising.
        "ivm_f_watermark_held_artifacts": watermark.get("held_artifacts"),
        "ivm_f_watermark_refused_answers": watermark.get("refused_answers"),
        "ivm_f_watermark_hold_ms_median": watermark.get("hold_ms_median"),
        "ivm_f_watermark_hold_ms_min": watermark.get("hold_ms_min"),
        "ivm_f_watermark_hold_ms_max": watermark.get("hold_ms_max"),
        "ivm_f_watermark_hold_bursts_median": watermark.get("hold_bursts_median"),
        "ivm_inter_burst_wall_ms": withheld_result.get("inter_burst_wall_ms"),
        "tgms_false_fresh": tgms_false_fresh,
        "tgms_stale_marked": tgms_stale_marked,
        "notes": [],
    }
    if tgms_false_fresh is None or tgms_stale_marked is None:
        out["notes"].append(
            "TGMS's own false_fresh/stale_marked at R10 (memo §3.5: "
            "`ext_export.py --check-at 10`'s `check_artifact` pass) was not "
            "supplied to this run — pass --tgms-false-fresh/--tgms-stale-marked "
            "once that file exists; left null rather than assumed 0")
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("export_dir", type=Path, help="exported cell bundle directory")
    ap.add_argument("result_json", type=Path, help="a configuration's result.json "
                    "(neo4j-recompute or ivm-dd)")
    ap.add_argument("--out", type=Path, help="write check.json here (default: "
                    "print to stdout)")
    ap.add_argument("--config-kind", choices=["neo4j", "ivm-dd"], default=None,
                    help="skip auto-detection")
    ap.add_argument("--allow-unverified-manifest", action="store_true",
                    help="proceed even if export-manifest.json's hashes don't "
                        "verify (default: refuse — lane X1b may still be writing "
                        "this cell)")
    ap.add_argument("--withheld-result", type=Path, default=None,
                    help="also score the withheld-correction cell's "
                        "withheld-result.json (memo §3.5, EXT2 (c))")
    ap.add_argument("--tgms-false-fresh", type=int, default=None)
    ap.add_argument("--tgms-stale-marked", type=int, default=None)
    args = ap.parse_args(argv)

    result = json.loads(args.result_json.read_text())
    check = check_cell(args.export_dir, result, config_kind=args.config_kind,
                       require_manifest=not args.allow_unverified_manifest)

    if args.withheld_result is not None:
        withheld = json.loads(args.withheld_result.read_text())
        check["withheld"] = check_withheld(
            withheld, tgms_false_fresh=args.tgms_false_fresh,
            tgms_stale_marked=args.tgms_stale_marked)

    text = json.dumps(check, indent=2, sort_keys=True) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        sys.stdout.write(text)
    print(tally_line(check), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
