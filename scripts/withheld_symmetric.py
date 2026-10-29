#!/usr/bin/env python3
"""P-WH-SYM: the symmetric withheld-correction experiment.

The dataflow baseline's withheld-correction cell delays burst 10's changelog
by one interval (delivered with burst 11) and reports what the maintained
view says at the burst-10 read point. This script runs the same schedule
against TGMS, whose freshness contract is defined relative to the *event
log*: burst 10's correction is appended to the log only after burst 11's
reads, and the artifacts it changes are read at the burst-10 read point
(correction not in the log), at the burst-11 read point (still not in the
log) and at the first check after the correction lands.

Mechanism (standalone replay of an exported cell bundle, not a harness
option): the base store is copied to a scratch directory, the bundle's
registered artifacts are re-registered with the correction-storm harness's
own registration idiom, and the bundle's batches are appended burst by burst
with `Storm._write`, running the harness's own per-burst cycle (candidate
pre-filter + `check_artifact`, then refresh of every registered artifact).
The only change relative to the harness cycle is the order of the log
appends. `--schedule inorder` replays the unmodified order and must
reproduce the control run's per-burst TGMS invalidation counts; it is the
fidelity check on the replay itself.

Output: one JSON record and one JSONL file of per-artifact rows (the
affected artifacts and a seeded control sample, at every read point).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import shutil
import socket
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CELL_ID = "synth-iv-60k-c4-deep-n1000-s0"
HELD_EPOCH = 10      # the burst whose correction is delayed
CONTROL_SEED = 0
SCHEDULES = ("delayed", "inorder")

#: Read points of the delayed schedule, in the order they occur.
RP_BEFORE = "R10"                 # burst-10 read point; correction not in the log
RP_BURST11 = "R11"                # burst-11 read point; correction still not in the log
RP_AFTER = "R11-after-arrival"    # first check after the correction lands


# -- pure helpers (unit-tested without a store) -------------------------------

def changed_names(oracle: dict[int, dict[str, str | None]], epoch: int) -> list[str]:
    """Artifacts whose oracle answer differs between epoch-1 and `epoch`."""
    prev, cur = oracle[epoch - 1], oracle[epoch]
    return sorted(n for n in cur if cur[n] != prev.get(n))


def pick_control(oracle: dict[int, dict[str, str | None]], epoch: int, k: int,
                 seed: int = CONTROL_SEED) -> list[str]:
    """`k` artifacts whose answer does NOT change at `epoch`, drawn with a
    seeded RNG from the name-sorted unaffected population."""
    affected = set(changed_names(oracle, epoch))
    pool = sorted(n for n in oracle[epoch] if n not in affected)
    if len(pool) < k:
        raise ValueError(f"only {len(pool)} unaffected artifacts, need {k}")
    return sorted(random.Random(seed).sample(pool, k))


def schedule_events(schedule: str, last_epoch: int = 11) -> list[tuple[str, int]]:
    """The ordered events of a replay: `("write", epoch)`, `("late", epoch)`
    (the delayed burst appended with a fresh tt), `("read", label-index)` is
    encoded as `("read", epoch)` with the read-point label resolved by
    `read_label`. Every write is followed by the harness's cycle; reads are
    placed exactly where the module docstring says."""
    if schedule not in SCHEDULES:
        raise ValueError(f"unknown schedule {schedule!r}")
    ev: list[tuple[str, int]] = []
    if schedule == "inorder":
        for e in range(1, last_epoch + 1):
            ev.append(("write", e))
        return ev
    for e in range(1, HELD_EPOCH):
        ev.append(("write", e))
    ev.append(("idle", HELD_EPOCH))               # the burst-10 interval, no append
    for e in range(HELD_EPOCH + 1, last_epoch + 1):
        ev.append(("write", e))
    ev.append(("late", HELD_EPOCH))
    return ev


def row_flags(*, fresh: bool, served: str | None, log_truth: str | None,
              oracle_truth: str | None) -> dict[str, bool]:
    """Per-artifact scoring at one read point. `false_fresh_log`: the verdict
    is FRESH although recomputing over the log as it stands gives another
    answer (the contract violation). `stale_vs_eventual`: FRESH although the
    served answer differs from the eventual-truth oracle (the plain dataflow
    notion of a false-fresh)."""
    return {
        "false_fresh_log": bool(fresh and log_truth is not None and served != log_truth),
        "stale_vs_eventual": bool(fresh and oracle_truth is not None and served != oracle_truth),
    }


def count_rows(rows: list[dict[str, Any]], read_point: str, group: str) -> dict[str, int]:
    sel = [r for r in rows if r["read_point"] == read_point and r["group"] == group]
    return {
        "n": len(sel),
        "fresh": sum(1 for r in sel if r["verdict_l0"] == "fresh"),
        "possibly_stale": sum(1 for r in sel if r["verdict_l0"] != "fresh"),
        "undecidable": sum(1 for r in sel if r["verdict_l0"] == "undecidable"),
        "fresh_l1": sum(1 for r in sel if r["verdict_l1"] == "fresh"),
        "possibly_stale_l1": sum(1 for r in sel if r["verdict_l1"] != "fresh"),
        "false_fresh_log": sum(1 for r in sel if r["false_fresh_log"]),
        "stale_vs_eventual": sum(1 for r in sel if r["stale_vs_eventual"]),
    }


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# -- bundle -------------------------------------------------------------------

def _jsonl(path: Path) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_bundle(bundle: Path) -> dict[str, Any]:
    artifacts = _jsonl(bundle / "artifacts.jsonl")
    tail = _jsonl(bundle / "eventlog-tail.jsonl")
    deltas = _jsonl(bundle / "deltas.jsonl")
    oracle_rows = _jsonl(bundle / "oracle.jsonl")
    oracle = {int(r["epoch"]): r["digests"] for r in oracle_rows}
    refused = {int(r["epoch"]): list(r.get("refused", [])) for r in oracle_rows}
    batches = {}
    for i, rec in enumerate(tail):
        epoch = int(deltas[i]["epoch"])
        if int(deltas[i]["tt"]) != int(rec["tt"]):
            raise ValueError(f"epoch {epoch}: deltas tt != eventlog-tail tt")
        batches[epoch] = {"tt": int(rec["tt"]), "ops": rec["ops"]}
    return {"artifacts": artifacts, "batches": batches, "oracle": oracle, "refused": refused}


# -- the replay ---------------------------------------------------------------

class Replay:
    def __init__(self, store_dir: Path, bundle: dict[str, Any], schedule: str) -> None:
        from tgms.artifact.lookup import affected
        from tgms.artifact.refresh import refresh
        from tgms.eval.storm import Storm
        from tgms.tgir.check import ChainCache

        self._affected, self._refresh = affected, refresh
        self.bundle, self.schedule = bundle, schedule
        self.storm = Storm(store_dir, n_artifacts=0, seed=0, backend="native", arms=(),
                           measure_ttf="sum", check_cache="none")
        self.chain_cache = ChainCache()
        self.store, self.registry = self.storm.store, self.storm.registry
        self.names: list[str] = []
        self.served: dict[str, str | None] = {}
        self.skipped: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self.epochs: list[dict[str, Any]] = []
        self.read_points: dict[str, dict[str, Any]] = {}
        self.late_tt: int | None = None

    # registration -----------------------------------------------------------
    def register(self) -> None:
        from tgms.core.errors import TgmsError
        for a in self.bundle["artifacts"]:
            try:
                _rec, env = self.storm._register_operator_artifact(a["name"], a["op"], a["args"])
            except TgmsError:
                self.skipped.append(a["name"])
                continue
            self.names.append(a["name"])
            self.served[a["name"]] = env.get("result_digest")

    # one batch: the harness's own write + candidate pre-filter + check -------
    def _append(self, tt: int, ops: list[dict[str, Any]]) -> dict[str, Any]:
        return self.storm._write(tt, list(ops))

    def _candidate_check(self, batch: dict[str, Any]) -> dict[str, Any]:
        from tgms.artifact.witness import check_artifact
        lr = self._affected(batch, self.registry)
        cand = sorted(r.name for r in lr.affected)
        inv: dict[str, list[str]] = {"L0": [], "L1": []}
        for name in cand:
            rec = self.registry.current(name)
            for key, l1 in (("L0", False), ("L1", True)):
                v = check_artifact(rec, self.store.eventlog, level1=l1,
                                   chain_cache=self.chain_cache)
                if not v.actionable_fresh:
                    inv[key].append(name)
        return {"candidates": len(cand), "invalidated_l0": len(inv["L0"]),
                "invalidated_l1": len(inv["L1"])}

    def maintain(self) -> int:
        """The harness's oracle pass: refresh every registered artifact."""
        from tgms.artifact.witness import RefreshHandle
        n = 0
        for name in self.names:
            rec = self.registry.current(name)
            h = RefreshHandle(rec.id, rec.refresh["kind"], rec.refresh["ref"],
                              rec.plan.get("plan_format"), rec.refresh.get("basis_policy", "open"))
            new = self._refresh(rec, h, self.store, self.registry)
            self.served[name] = (new.payload or {}).get("result_digest")
            n += 1
        return n

    def log_truth(self) -> dict[str, str | None]:
        """What each artifact's operator returns over the log as it stands now."""
        from tgms.tools.server import ToolRouter
        router = ToolRouter(self.store.adapter, tt_source=self.store)
        out: dict[str, str | None] = {}
        for a in self.bundle["artifacts"]:
            if a["name"] not in self.served:
                continue
            env = router.call(a["op"], a["args"])
            out[a["name"]] = None if "error" in env else env.get("result_digest")
        return out

    # a read point -------------------------------------------------------------
    def read(self, label: str, oracle_epoch: int, expected_log_epoch: int | None,
             affected: list[str], control: list[str]) -> None:
        from tgms.artifact.witness import check_artifact
        oracle_e = self.bundle["oracle"][oracle_epoch]
        expected = self.bundle["oracle"].get(expected_log_epoch) if expected_log_epoch else None
        truth = self.log_truth()
        group_of = {n: "affected" for n in affected} | {n: "control" for n in control}
        all_counts = {"n": 0, "fresh": 0, "possibly_stale": 0, "false_fresh_log": 0,
                      "stale_vs_eventual": 0}
        log_vs_expected_mismatch = 0
        for name in self.names:
            rec = self.registry.current(name)
            vs = {}
            for key, l1 in (("l0", False), ("l1", True)):
                vs[key] = check_artifact(rec, self.store.eventlog, level1=l1,
                                         chain_cache=self.chain_cache)
            states = [v.state for _sid, v in vs["l0"].steps.per_step]
            verdict_l0 = ("fresh" if vs["l0"].actionable_fresh else
                          "undecidable" if "undecidable" in states else "possibly-stale")
            verdict_l1 = "fresh" if vs["l1"].actionable_fresh else "possibly-stale"
            fl = row_flags(fresh=vs["l0"].actionable_fresh, served=self.served[name],
                           log_truth=truth.get(name), oracle_truth=oracle_e.get(name))
            if expected is not None and truth.get(name) != expected.get(name):
                log_vs_expected_mismatch += 1
            all_counts["n"] += 1
            all_counts["fresh" if verdict_l0 == "fresh" else "possibly_stale"] += 1
            all_counts["false_fresh_log"] += fl["false_fresh_log"]
            all_counts["stale_vs_eventual"] += fl["stale_vs_eventual"]
            if name in group_of:
                wit_tts = sorted({w.tt for w in vs["l0"].steps.witnesses})
                self.rows.append({
                    "read_point": label, "name": name, "group": group_of[name],
                    "oracle_epoch": oracle_epoch,
                    "changes_at_held_epoch": name in set(affected),
                    "changes_at_epoch11": (self.bundle["oracle"][11].get(name)
                                           != self.bundle["oracle"][10].get(name)),
                    "verdict_l0": verdict_l0, "verdict_l1": verdict_l1,
                    "served_digest": self.served[name], "log_digest": truth.get(name),
                    "eventual_digest": oracle_e.get(name),
                    "witness_tts": wit_tts[:4], "n_witnesses": len(vs["l0"].steps.witnesses),
                    **fl})
        self.read_points[label] = {"oracle_epoch": oracle_epoch,
                                   "registry": all_counts,
                                   "log_vs_expected_mismatch": log_vs_expected_mismatch,
                                   "log_tt": self.store.eventlog.last_tt()}

    # the schedule -------------------------------------------------------------
    def run(self, affected: list[str], control: list[str]) -> None:
        b = self.bundle["batches"]
        last_epoch = 11
        events = schedule_events(self.schedule, last_epoch)
        for kind, e in events:
            if kind == "write":
                batch = self._append(b[e]["tt"], b[e]["ops"])
                cyc = self._candidate_check(batch)
                self.epochs.append({"epoch": e, "kind": "write", **cyc})
                if self.schedule == "inorder" and e in (HELD_EPOCH, last_epoch):
                    self.read(f"R{e}", e, e, affected, control)
                elif self.schedule == "delayed" and e == last_epoch:
                    # the log holds 1..9 and 11; the held burst is absent
                    self.read(RP_BURST11, last_epoch, None, affected, control)
                self.maintain()
            elif kind == "idle":
                # burst-10 interval: nothing arrives; the read happens at its end
                self.epochs.append({"epoch": e, "kind": "idle"})
                self.read(RP_BEFORE, HELD_EPOCH, HELD_EPOCH - 1, affected, control)
            elif kind == "late":
                self.late_tt = self.store.eventlog.last_tt() + 1
                batch = self._append(self.late_tt, b[e]["ops"])
                cyc = self._candidate_check(batch)
                self.epochs.append({"epoch": e, "kind": "late", "tt": self.late_tt, **cyc})
                self.read(RP_AFTER, last_epoch, last_epoch, affected, control)
        # convergence: after maintenance the served answers equal the epoch-11 oracle
        self.maintain()
        oracle_end = self.bundle["oracle"][last_epoch]
        self.converged = sum(1 for n in self.names if self.served[n] != oracle_end.get(n))

    def close(self) -> None:
        self.storm.close()


def counts_block(rows: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, int]]]:
    rps = sorted({r["read_point"] for r in rows})
    return {rp: {g: count_rows(rows, rp, g) for g in ("affected", "control")} for rp in rps}


def run_once(bundle_dir: Path, base_store: Path, work: Path, schedule: str,
             n_control: int) -> dict[str, Any]:
    bundle = load_bundle(bundle_dir)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    store = work / "store"
    shutil.copytree(base_store, store)
    affected = changed_names(bundle["oracle"], HELD_EPOCH)
    control = pick_control(bundle["oracle"], HELD_EPOCH, n_control)
    t0 = time.time()
    rp = Replay(store, bundle, schedule)
    rp.register()
    reg_ok = sum(1 for n in rp.names if rp.served[n] == bundle["oracle"][0].get(n))
    rp.run(affected, control)
    wall = time.time() - t0
    out = {
        "schedule": schedule, "wall_s": round(wall, 1),
        "registered": len(rp.names), "skipped_registration": len(rp.skipped),
        "registration_digest_matches_oracle0": reg_ok,
        "affected": affected, "control": control,
        "epochs": rp.epochs, "read_points": rp.read_points,
        "counts": counts_block(rp.rows), "rows": rp.rows,
        "late_tt": rp.late_tt, "end_state_mismatch_vs_oracle11": rp.converged,
    }
    rp.close()
    shutil.rmtree(work, ignore_errors=True)
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bundle", required=True, type=Path)
    ap.add_argument("--store", required=True, type=Path, help="pristine base store (copied)")
    ap.add_argument("--work", required=True, type=Path)
    ap.add_argument("--schedule", choices=SCHEDULES, default="delayed")
    ap.add_argument("--n-control", type=int, default=39)
    ap.add_argument("--out", required=True, type=Path, help="per-run JSON (counts + rows)")
    args = ap.parse_args(argv)
    res = run_once(args.bundle, args.store, args.work, args.schedule, args.n_control)
    res["host"] = socket.gethostname()
    res["timestamp_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    args.out.write_text(json.dumps(res, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"schedule": res["schedule"], "wall_s": res["wall_s"],
                      "counts": res["counts"],
                      "end_state_mismatch_vs_oracle11": res["end_state_mismatch_vs_oracle11"]},
                     indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
