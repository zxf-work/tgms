#!/usr/bin/env python3
"""Standing state of a maintained dataflow view vs TGMS's lineage registry,
for the same registered artifacts (P-AXES axis A).

Two subcommands, each writing one JSON document; neither is a timed run.

    # the view: ivm-dd on an exported cell, single worker, sampled at 1 s
    python scripts/axes_standing_state.py view \\
        --ivm-dd <binary> --cell <export cell dir> --out <dir>

    # TGMS: register the cell's population, then measure a separate
    # check process over that registry and event log
    python scripts/axes_standing_state.py tgms \\
        --store <store dir> --n-artifacts 1000 --batches-mode end-to-end \\
        --work <scratch dir> --out <file.json>

**view.** Runs `ivm-dd run <cell> --out <dir> --state-report` and samples
the child's `/proc/<pid>/status` (`VmRSS`, `VmHWM`) every `--interval`
seconds. `ivm-dd` itself reports `VmRSS`/`VmHWM` and its arrangements' live
record counts at fixed points (after the bundle load, after epoch 0 plus
registration, after burst 1, at the end; `state-report.json`), and its
stderr `STATE` lines are timestamped on the same clock as the samples.

**tgms.** Process 1 (the harness, `tgms.eval.storm.Storm`, the same
registration `scripts/bench_correction_storm.py` performs) copies the store,
registers the population and records the registry file's bytes, its
in-memory entry count and its own resident memory; then runs one burst
(`Storm.run(1)`) and records them again. Process 2 is the check process:
a fresh interpreter that opens only the registry (truncated back to its
post-registration bytes, i.e. every artifact at the generation the burst
found) and the event log (which holds the burst's correction), and runs
that burst's check pass exactly as the harness does — `affected()` over the
burst's batch, then `check_artifact` at level 0 and level 1 for every
survivor with one `ChainCache` — recording `VmRSS`/`VmHWM` after imports,
after the registry fold and after the check pass. It never opens the
storage engine: checking freshness needs the registry and the log only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _status_kb(pid: int | str = "self") -> dict[str, int | None]:
    out: dict[str, int | None] = {"vm_rss_kb": None, "vm_hwm_kb": None}
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    out["vm_rss_kb"] = int(line.split()[1])
                elif line.startswith("VmHWM:"):
                    out["vm_hwm_kb"] = int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return out


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sample_child(cmd: list[str], interval: float, *, env: dict[str, str] | None = None,
                  ) -> dict[str, Any]:
    """Run `cmd`, sampling its `/proc/<pid>/status` every `interval` seconds;
    collect its stdout and its stderr lines, each stamped with seconds since
    launch on the sampler's clock."""
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env)
    stderr_lines: list[dict[str, Any]] = []
    stdout_lines: list[str] = []

    def _drain_err() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append({"t_s": time.monotonic() - t0, "line": line.rstrip("\n")})

    def _drain_out() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            stdout_lines.append(line.rstrip("\n"))

    threads = [threading.Thread(target=_drain_err, daemon=True),
               threading.Thread(target=_drain_out, daemon=True)]
    for t in threads:
        t.start()
    samples: list[dict[str, Any]] = []
    while proc.poll() is None:
        s = _status_kb(proc.pid)
        if s["vm_rss_kb"] is not None:
            samples.append({"t_s": time.monotonic() - t0, **s})
        time.sleep(interval)
    for t in threads:
        t.join()
    return {"returncode": proc.returncode, "wall_s": time.monotonic() - t0,
            "samples": samples, "stderr": stderr_lines, "stdout": stdout_lines}


# ---------------------------------------------------------------------------
# view
# ---------------------------------------------------------------------------

def cmd_view(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cmd = [str(args.ivm_dd), "run", str(args.cell), "--out", str(out), "--state-report"]
    started = _utc()
    run = _sample_child(cmd, args.interval)
    report_path = out / "state-report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else None
    state_lines = [{"t_s": e["t_s"], **json.loads(e["line"][len("STATE "):])}
                   for e in run["stderr"] if e["line"].startswith("STATE ")]
    rss = [s["vm_rss_kb"] for s in run["samples"]]
    doc = {
        "kind": "view", "cell": str(args.cell), "command": cmd, "started_utc": started,
        "finished_utc": _utc(), "returncode": run["returncode"], "wall_s": run["wall_s"],
        "interval_s": args.interval, "state_report": report, "state_lines": state_lines,
        "samples": run["samples"],
        "sampled_rss_kb_max": max(rss) if rss else None,
        "sampled_rss_kb_last": rss[-1] if rss else None,
        "stdout": run["stdout"],
        "stderr_other": [e for e in run["stderr"] if not e["line"].startswith("STATE ")][-50:],
    }
    _write_json(out / "view-standing-state.json", doc)
    print(f"wrote {out / 'view-standing-state.json'} (exit {run['returncode']})")
    return 0 if run["returncode"] == 0 else 1


# ---------------------------------------------------------------------------
# tgms: process 1 (harness) and process 2 (check process)
# ---------------------------------------------------------------------------

def _registry_line_bytes(path: Path, limit: int) -> tuple[int, list[int]]:
    """`(header line bytes, [record line bytes])` of the registry's first
    `limit` bytes; the file opens with one format header line."""
    with open(path, "rb") as f:
        data = f.read(limit)
    lines = [line for line in data.split(b"\n") if line]
    header = 0
    if lines and json.loads(lines[0]).get("format") == "tgms-artifact-registry":
        header = len(lines.pop(0)) + 1
    return header, [len(line) + 1 for line in lines]


def _fold_entries(registry: Any) -> dict[str, int]:
    return {"names": len(registry._by_name),
            "entries": sum(len(h) for h in registry._by_name.values())}


def cmd_tgms(args: argparse.Namespace) -> int:
    from tgms.eval.storm import Storm, build_mix

    work = Path(args.work)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    store = work / "store"
    shutil.copytree(args.store, store)
    started = _utc()
    proc1: dict[str, Any] = {"after_start": _status_kb()}

    t0 = time.monotonic()
    storm = Storm(store, n_artifacts=args.n_artifacts, seed=args.seed, backend="native",
                  mix=build_mix(args.mix, burst_size=10_000, burst_after=10),
                  measure_ttf=args.measure_ttf, check_cache="chain")
    registration_s = time.monotonic() - t0
    reg_path = storm.registry.path
    reg_bytes = reg_path.stat().st_size
    n_registered = len(storm.artifacts)
    header_bytes, lines = _registry_line_bytes(reg_path, reg_bytes)
    lines_sorted = sorted(lines)
    registration = {
        "n_artifacts_requested": args.n_artifacts, "n_registered": n_registered,
        "n_registration_skipped": storm.n_registration_skipped,
        "registry_bytes": reg_bytes, "registry_header_bytes": header_bytes,
        "registry_record_lines": len(lines),
        "registry_bytes_per_artifact": reg_bytes / n_registered if n_registered else None,
        "registry_record_bytes_mean": sum(lines) / len(lines) if lines else None,
        "registry_line_bytes_median": lines_sorted[len(lines_sorted) // 2] if lines else None,
        "registry_line_bytes_min": lines_sorted[0] if lines else None,
        "registry_line_bytes_max": lines_sorted[-1] if lines else None,
        "fold": _fold_entries(storm.registry), "registration_s": registration_s,
        "eventlog_bytes": storm.store.eventlog.size(),
    }
    proc1["after_registration"] = _status_kb()

    t1 = time.monotonic()
    rows = storm.run(1)
    burst_s = time.monotonic() - t1
    proc1["after_burst_1"] = _status_kb()
    burst = {
        "batches": len(rows), "burst_s": burst_s,
        "registry_bytes": reg_path.stat().st_size, "fold": _fold_entries(storm.registry),
        "eventlog_bytes": storm.store.eventlog.size(),
        "candidate_survivors": rows[0].candidate_survivors if rows else None,
        "changed_count": len(rows[0].oracle_changed) if rows else None,
    }
    names_registered = sorted(storm.artifacts)
    storm.close()

    # the check process's directory: the registry as the burst found it (its
    # post-registration prefix) beside the log that now holds the correction
    check_dir = work / "check"
    check_dir.mkdir()
    shutil.copy2(store / "eventlog.jsonl", check_dir / "eventlog.jsonl")
    with open(reg_path, "rb") as src, open(check_dir / reg_path.name, "wb") as dst:
        dst.write(src.read(reg_bytes))

    cmd = [sys.executable, str(Path(__file__).resolve()), "tgms-check",
           "--check-dir", str(check_dir), "--out", str(work / "check-process.json")]
    run = _sample_child(cmd, args.interval, env=dict(os.environ))
    check = (json.loads((work / "check-process.json").read_text())
             if run["returncode"] == 0 else None)

    doc = {
        "kind": "tgms", "store": str(args.store), "seed": args.seed, "mix": args.mix,
        "measure_ttf": args.measure_ttf, "check_cache": "chain", "backend": "native",
        "started_utc": started, "finished_utc": _utc(),
        "registration": registration, "burst_1": burst, "harness_process": proc1,
        "check_process": check,
        "check_process_samples": run["samples"], "check_process_returncode": run["returncode"],
        "check_process_stderr": [e["line"] for e in run["stderr"]][-50:],
        "names_registered_sha256": hashlib.sha256(
            "\n".join(names_registered).encode()).hexdigest(),
    }
    _write_json(Path(args.out), doc)
    if not args.keep_work:
        shutil.rmtree(work, ignore_errors=True)
    print(f"wrote {args.out} (check process exit {run['returncode']})")
    return 0 if run["returncode"] == 0 else 1


def cmd_tgms_check(args: argparse.Namespace) -> int:
    """Process 2. Imports first, so the baseline reading includes them."""
    from tgms.artifact.lookup import affected
    from tgms.artifact.registry import Registry
    from tgms.artifact.witness import check_artifact
    from tgms.storage.eventlog import EventLog
    from tgms.tgir.check import ChainCache

    check_dir = Path(args.check_dir)
    doc: dict[str, Any] = {"after_imports": _status_kb()}
    t0 = time.monotonic()
    log = EventLog(check_dir / "eventlog.jsonl")
    registry = Registry(check_dir, log=log, read_only=True)
    doc["registry_load_s"] = time.monotonic() - t0
    doc["fold"] = _fold_entries(registry)
    doc["after_registry_load"] = _status_kb()

    batch = None
    for batch in log.batches():
        pass
    if batch is None:
        raise SystemExit("event log holds no batch")
    cache = ChainCache()
    t1 = time.monotonic()
    lr = affected(batch, registry)
    stale = {"L0": 0, "L1": 0}
    checks = 0
    for record in lr.affected:
        current = registry.current(record.name)
        if current is None:
            continue
        for arm, level1 in (("L0", False), ("L1", True)):
            verdict = check_artifact(current, log, level1=level1, chain_cache=cache)
            checks += 1
            if not verdict.actionable_fresh:
                stale[arm] += 1
    doc["check_pass_s"] = time.monotonic() - t1
    doc["candidate_survivors"] = len(lr.affected)
    doc["checks"] = checks
    doc["not_fresh"] = stale
    doc["chain_cache"] = {"hits": cache.hits, "misses": cache.misses}
    doc["batch_tt"] = batch.get("tt")
    doc["after_check_pass"] = _status_kb()
    _write_json(Path(args.out), doc)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("view", help="ivm-dd standing state on one exported cell")
    v.add_argument("--ivm-dd", required=True, type=Path)
    v.add_argument("--cell", required=True, type=Path)
    v.add_argument("--out", required=True, type=Path)
    v.add_argument("--interval", type=float, default=1.0)

    t = sub.add_parser("tgms", help="TGMS registry + check-process standing state")
    t.add_argument("--store", required=True, type=Path)
    t.add_argument("--n-artifacts", type=int, required=True)
    t.add_argument("--seed", type=int, default=0)
    t.add_argument("--mix", default="c1")
    t.add_argument("--measure-ttf", default="end-to-end", choices=("sum", "end-to-end"))
    t.add_argument("--work", required=True, type=Path)
    t.add_argument("--out", required=True, type=Path)
    t.add_argument("--interval", type=float, default=1.0)
    t.add_argument("--keep-work", action="store_true")

    c = sub.add_parser("tgms-check", help=argparse.SUPPRESS)
    c.add_argument("--check-dir", required=True, type=Path)
    c.add_argument("--out", required=True, type=Path)

    args = ap.parse_args(argv)
    return {"view": cmd_view, "tgms": cmd_tgms, "tgms-check": cmd_tgms_check}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
