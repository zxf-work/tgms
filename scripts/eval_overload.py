"""Overload sweep (Lane B5/F2): find where the service surface's
backpressure actually engages, and confirm it recovers once load drops.

**Open-loop, not closed-loop, and that choice is the point.** A closed-loop
client — issue a call, wait for it to finish, issue the next — can never
apply more pressure than its own round-trip lets it; N closed-loop clients
measure whichever throughput N threads happen to sustain, and the
concurrency cap this sweep exists to exercise never actually engages, because
a blocked client cannot also be an arriving one. So each simulated client
here schedules its own call times from a token bucket at a *target* rate,
independent of how long the previous call took — arrivals that outrun
service are exactly what should trip `tgms.tools.limits.ConcurrencyGate`,
and a call issued late against its own schedule is counted (`late_ms`) as
the read-path analogue of queueing, since a read-only sweep has no write
queue to sample (see the `queue_depth` field below).

**What "process" means here.** The task calls for closed-loop clients as
separate *processes*; this implementation uses threads sharing one
`ToolRouter` in one process instead, because the mechanism under test — one
shared `ConcurrencyGate` deciding whether a call is admitted — only exists
once per process (`tgms.tools.server.ToolRouter.__init__`). N separate
processes would each build their own gate and cap their own concurrency
independently, which measures N unrelated limiters, not the shared one a
real `tgms serve`/`tgms webapp` process enforces. `ToolRouter`'s own
docstring already names this posture ("what the executor uses in
experiments — no network hop"); threads against one router are the accurate
model of many concurrent callers hitting one server process. This deviation
is called out explicitly, not left implicit.

Each step measures `n_clients` threads, each an independent token-bucket
schedule, for `--duration-s / n_steps` seconds; the sweep runs `--clients`
in increasing order and finishes with a short low-rate "recovery" step to
confirm throughput/latency return to baseline once load drops.

    uv run python scripts/eval_overload.py --store stores/synth-300k \\
        --clients 1 2 4 8 16 --duration-s 120 --out overload.json
    uv run python scripts/eval_overload.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tgms  # noqa: E402
from tgms.tools.limits import Limits  # noqa: E402
from tgms.tools.server import ToolRouter  # noqa: E402

SCHEMA_VERSION = "1.0.0"


# --------------------------------------------------------------------------- #
# open-loop scheduling                                                        #
# --------------------------------------------------------------------------- #

def token_bucket_schedule(rate_hz: float, duration_s: float) -> list[float]:
    """Deterministic open-loop arrival offsets (seconds from step start):
    one call every `1/rate_hz`, for `duration_s`. A real token bucket also
    allows bursting; this sweep wants steady pressure at a known rate, so it
    uses the bucket's steady-state schedule directly rather than simulating
    token accumulation."""
    if rate_hz <= 0:
        return []
    n = max(1, int(duration_s * rate_hz))
    return [i / rate_hz for i in range(n)]


@dataclass
class CallRecord:
    scheduled_s: float
    late_ms: float
    wall_ms: float
    outcome: str                 # "ok" | "refused" | "error"
    refusal_stage: str | None


@dataclass
class StepResult:
    n_clients: int
    target_rate_hz: float
    duration_s: float
    n_calls: int = 0
    n_ok: int = 0
    n_refused: int = 0
    n_error: int = 0
    throughput_qps: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    late_mean_ms: float = 0.0
    late_p95_ms: float = 0.0
    concurrent_in_flight_p95: float = 0.0
    queue_depth: Any = None  # not applicable to a read-only sweep; see note

    def to_json(self) -> dict[str, Any]:
        return {
            "n_clients": self.n_clients, "target_rate_hz": self.target_rate_hz,
            "duration_s": self.duration_s, "n_calls": self.n_calls,
            "n_ok": self.n_ok, "n_refused": self.n_refused,
            "n_error": self.n_error, "throughput_qps": round(self.throughput_qps, 3),
            "p50_ms": round(self.p50_ms, 3), "p95_ms": round(self.p95_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "late_mean_ms": round(self.late_mean_ms, 3),
            "late_p95_ms": round(self.late_p95_ms, 3),
            "concurrent_in_flight_p95": round(self.concurrent_in_flight_p95, 3),
            "queue_depth": self.queue_depth,
        }


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[idx]


def run_step(router: ToolRouter, op: str, args: dict[str, Any],
            n_clients: int, rate_hz: float, duration_s: float) -> StepResult:
    """One rate step: `n_clients` open-loop threads at `rate_hz` each
    (aggregate arrival rate = `n_clients * rate_hz`), for `duration_s`."""
    schedule = token_bucket_schedule(rate_hz, duration_s)
    records: list[CallRecord] = []
    records_lock = threading.Lock()
    in_flight_samples: list[int] = []
    stop_sampling = threading.Event()

    def sampler() -> None:
        while not stop_sampling.is_set():
            in_flight_samples.append(router._gate.in_flight)
            time.sleep(0.01)

    def client(_client_id: int) -> None:
        t0 = time.perf_counter()
        local: list[CallRecord] = []
        for sched in schedule:
            now = time.perf_counter() - t0
            if sched > now:
                time.sleep(sched - now)
            start = time.perf_counter()
            env = router.call(op, args)
            wall_ms = (time.perf_counter() - start) * 1000
            late_ms = max(0.0, (start - t0) - sched) * 1000
            if "error" not in env:
                outcome, stage = "ok", None
            else:
                details = env.get("details", {}) or {}
                stage = details.get("stage")
                outcome = "refused" if stage == "limit" else "error"
            local.append(CallRecord(sched, late_ms, wall_ms, outcome, stage))
        with records_lock:
            records.extend(local)

    sampler_thread = threading.Thread(target=sampler, daemon=True)
    sampler_thread.start()
    threads = [threading.Thread(target=client, args=(i,)) for i in range(n_clients)]
    wall_start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall_elapsed = time.perf_counter() - wall_start
    stop_sampling.set()
    sampler_thread.join(timeout=1)

    result = StepResult(n_clients=n_clients, target_rate_hz=rate_hz,
                        duration_s=duration_s)
    result.n_calls = len(records)
    result.n_ok = sum(1 for r in records if r.outcome == "ok")
    result.n_refused = sum(1 for r in records if r.outcome == "refused")
    result.n_error = sum(1 for r in records if r.outcome == "error")
    result.throughput_qps = result.n_ok / wall_elapsed if wall_elapsed > 0 else 0.0
    ok_latencies = [r.wall_ms for r in records if r.outcome == "ok"]
    result.p50_ms = _percentile(ok_latencies, 0.50)
    result.p95_ms = _percentile(ok_latencies, 0.95)
    result.p99_ms = _percentile(ok_latencies, 0.99)
    late = [r.late_ms for r in records]
    result.late_mean_ms = statistics.fmean(late) if late else 0.0
    result.late_p95_ms = _percentile(late, 0.95)
    result.concurrent_in_flight_p95 = _percentile(
        [float(x) for x in in_flight_samples], 0.95)
    # Not applicable: this sweep is read-only, so there is no
    # `GroupCommitWriter` bounded queue to sample (that queue is on the
    # write path — `tgms.write.GroupCommitWriter`, exercised by
    # `tests/test_limits.py`, not by this read-side sweep).
    result.queue_depth = None
    return result, records


# --------------------------------------------------------------------------- #
# manifest (benchmarks/schema/result_manifest.schema.json)                    #
# --------------------------------------------------------------------------- #

def _git_commit() -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=5).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                                    capture_output=True, text=True,
                                    timeout=5).stdout.strip())
        return (sha + ("-dirty" if dirty else "")) if sha else "0000000"
    except Exception:
        return "0000000"


def _machine_info() -> dict[str, Any]:
    ram_gb = 0.1
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            if out:
                ram_gb = round(int(out) / (1024 ** 3), 1)
        else:
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        ram_gb = round(int(line.split()[1]) / (1024 ** 2), 1)
                        break
    except Exception:
        pass
    return {"host": platform.node() or "unknown", "platform": platform.platform(),
            "cpus": os.cpu_count() or 1, "ram_gb": ram_gb}


def build_manifest(*, store_path: str, store_digest: str, op: str,
                   args: dict[str, Any], limits: Limits, steps: list[StepResult],
                   recovery: StepResult, dry_run: bool,
                   records_path: str) -> dict[str, Any]:
    steps_json = [s.to_json() for s in steps]
    digest_src = json.dumps({"steps": steps_json, "recovery": recovery.to_json()},
                            sort_keys=True).encode()
    return {
        "schema_version": SCHEMA_VERSION,
        "git_commit": _git_commit(),
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "machine": _machine_info(),
        "config": {"store": store_path, "op": op, "args": args,
                  "max_concurrent": limits.max_concurrent,
                  "max_rows": limits.max_rows, "max_bytes": limits.max_bytes,
                  "dry_run": dry_run},
        "seed": {"value": None,
                "reason": "open-loop arrival scheduling is deterministic "
                         "given (rate, duration); no RNG is used"},
        "dataset": {"name": Path(store_path).name, "digest": store_digest,
                   "digest_kind": "store_digest"},
        "result_digest": hashlib.sha256(digest_src).hexdigest(),
        "protocol": {"warmups": 0, "reps": len(steps),
                    "ceilings": {"max_concurrent": limits.max_concurrent}},
        "record": records_path,
        "steps": steps_json,
        "recovery": recovery.to_json(),
        "dev_host_note": "measured on a development host for functional "
                        "verification of the backpressure mechanism, per "
                        "the task instruction this is NOT a reported "
                        "benchmark result and carries no reproducibility "
                        "claim beyond this host/run.",
    }


def render_table(steps: list[StepResult], recovery: StepResult) -> str:
    header = (f"{'clients':>7} {'rate/client':>11} {'calls':>6} {'ok':>6} "
             f"{'refused':>7} {'qps':>8} {'p50ms':>7} {'p95ms':>7} "
             f"{'p99ms':>7} {'late_p95ms':>10} {'inflight_p95':>12}")
    lines = [header, "-" * len(header)]
    for s in steps:
        j = s.to_json()
        lines.append(f"{j['n_clients']:>7} {j['target_rate_hz']:>11.1f} "
                     f"{j['n_calls']:>6} {j['n_ok']:>6} {j['n_refused']:>7} "
                     f"{j['throughput_qps']:>8.2f} {j['p50_ms']:>7.2f} "
                     f"{j['p95_ms']:>7.2f} {j['p99_ms']:>7.2f} "
                     f"{j['late_p95_ms']:>10.2f} "
                     f"{j['concurrent_in_flight_p95']:>12.2f}")
    lines.append("-" * len(header))
    j = recovery.to_json()
    lines.append(f"{'recover':>7} {j['target_rate_hz']:>11.1f} {j['n_calls']:>6} "
                f"{j['n_ok']:>6} {j['n_refused']:>7} {j['throughput_qps']:>8.2f} "
                f"{j['p50_ms']:>7.2f} {j['p95_ms']:>7.2f} {j['p99_ms']:>7.2f} "
                f"{j['late_p95_ms']:>10.2f} {j['concurrent_in_flight_p95']:>12.2f}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #

def run_sweep(store_path: str, client_counts: list[int], duration_s: float,
             rate_per_client: float, max_concurrent: int | None,
             out_records: Path | None) -> dict[str, Any]:
    store = tgms.open(store_path, read_only=True)
    uid = store.adapter.uids_for([0])[0]
    op, args = "entity_history", {"uid": uid, "limit": 5}
    limits = Limits(max_concurrent=max_concurrent)
    router = ToolRouter(store.adapter, tt_source=store, limits=limits)

    per_step_s = duration_s / (len(client_counts) + 1)  # +1 for the recovery step
    steps: list[StepResult] = []
    all_records: list[dict[str, Any]] = []
    for n in client_counts:
        result, records = run_step(router, op, args, n, rate_per_client, per_step_s)
        steps.append(result)
        all_records.append({"step": "load", "n_clients": n,
                           "records": [r.__dict__ for r in records]})

    # recovery: back down to a single, unhurried client and confirm the
    # numbers look like the n_clients=1 step again, not a degraded tail.
    recovery, recovery_records = run_step(router, op, args, 1,
                                          rate_per_client, per_step_s)
    all_records.append({"step": "recovery", "n_clients": 1,
                       "records": [r.__dict__ for r in recovery_records]})

    store_digest = store.digest()
    store.close()

    records_path_str = "(not written; pass --out to persist raw records)"
    if out_records is not None:
        out_records.parent.mkdir(parents=True, exist_ok=True)
        with open(out_records, "w") as f:
            json.dump(all_records, f, indent=1, default=str)
        records_path_str = str(out_records.relative_to(ROOT)) \
            if out_records.is_relative_to(ROOT) else str(out_records)

    manifest = build_manifest(store_path=store_path, store_digest=store_digest,
                              op=op, args=args, limits=limits, steps=steps,
                              recovery=recovery, dry_run=False,
                              records_path=records_path_str)
    manifest["table"] = render_table(steps, recovery)
    return manifest


def run_dry(tmp_dir: Path) -> dict[str, Any]:
    """A tiny, fast, self-contained sweep — no `stores/synth-300k` needed."""
    from tgms.data.synth import generate

    out_dir = tmp_dir / "synth"
    store_dir = tmp_dir / "store"
    generate(str(out_dir), n_nodes=20, n_events=200, seed=0)
    store = tgms.open(store_dir)
    with open(out_dir / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    store.close()

    return run_sweep(str(store_dir), client_counts=[1, 2], duration_s=1.0,
                     rate_per_client=20.0, max_concurrent=2, out_records=None)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--store", default=None)
    ap.add_argument("--clients", type=int, nargs="+", default=[1, 2, 4, 8, 16])
    ap.add_argument("--duration-s", type=float, default=120.0)
    ap.add_argument("--rate-per-client", type=float, default=25.0,
                    help="open-loop target rate per simulated client, Hz")
    ap.add_argument("--max-concurrent", type=int, default=8,
                    help="the ConcurrencyGate cap under test")
    ap.add_argument("--out", default=None, help="write the manifest JSON here")
    ap.add_argument("--dry-run", action="store_true",
                    help="tiny synthetic store, ~1s total, no --store needed")
    args = ap.parse_args(argv)

    if args.dry_run:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            manifest = run_dry(Path(tmp))
    else:
        if not args.store:
            ap.error("--store is required unless --dry-run")
        out_records = Path(args.out).with_suffix(".records.json") if args.out else None
        manifest = run_sweep(args.store, args.clients, args.duration_s,
                             args.rate_per_client, args.max_concurrent, out_records)

    print(manifest.pop("table"))
    if args.out:
        with open(args.out, "w") as f:
            json.dump(manifest, f, indent=1, default=str)
        print(f"\nmanifest written to {args.out}")
    else:
        print(json.dumps(manifest, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
