"""Resource-axis evaluation: the four measurements the plan's resource
sections ask for, on the stores the phase-0 harness defines.

* **threads** (§14.3) — the thread-scaling curve of the parallel scan.
  `TGMS_SCAN_THREADS` overrides the engine's worker count (scan.rs); each
  point runs in a fresh subprocess with the variable in its environment,
  so propagation into the Rust extension is by inheritance, not by
  `putenv` timing. DuckDB gets a column because its knob is one flag away
  (`SET threads`). Every thread count must produce the same result hash —
  the run fails otherwise, which measures the engine's "byte-identical
  output to the serial loop by construction" claim, not just its speed.

* **coldwarm** (§15) — three cache states per query, coldest last:
  `warm` (in-process repetition, the published protocol), `process_cold`
  (fresh process, OS page cache warm: what a new client pays), and
  `cold` (fresh process *and* page cache evicted: first query ever).
  Eviction is user-space — `posix_fadvise(fd, 0, 0, POSIX_FADV_DONTNEED)`
  over every store file — because the measurement host offers no root for
  `drop_caches`. DONTNEED drops clean pages, and a read-only store's pages
  are clean, but the kernel is free to keep pages another process holds;
  the record therefore states the method rather than claiming a sterile
  cold state.

* **memcap** (§14.2) — the query suite under a hard memory ceiling, to
  find where the unbounded segment cache (a known roadmap item) actually
  hurts. Preferred enforcement is a rootless-Docker cgroup
  (`--memory=CAP --memory-swap=CAP`, so the cap is memory, not
  memory+swap); the store and venv are bind-mounted at their host paths
  so the editable install resolves unchanged. Where the host's cgroup
  setup cannot enforce limits rootless (cgroup v1), the fallback is
  `RLIMIT_AS`, which caps *address space* — mmapped store files count
  against it even when resident bytes do not, so it is a strictly harsher
  approximation and is labeled as such in the record.

* **readers** (§14.4) — N reader processes over one store, each looping
  the same query mix inside a common wall-clock window (barrier start).
  Reports per-reader per-query medians and aggregate throughput. This is
  the measurement behind the "lock-free reads off immutable segments"
  claim: per-reader latency should hold roughly flat until the readers'
  own scan threads oversubscribe the cores.

Stores are built once per scale by the phase-0 generator (same reference
event log, replayed per D-023) and cached under --workdir with their
registry parameters, so every mode measures the same bytes.

    uv run python scripts/eval_resources.py threads  --scale 1000000 --json out.json
    uv run python scripts/eval_resources.py coldwarm --scale 10000000 --json out.json
    uv run python scripts/eval_resources.py memcap   --scale 10000000 --caps 2g,4g,8g --json out.json
    uv run python scripts/eval_resources.py readers  --scale 10000000 --readers 1,2,4,8,16 --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import eval_harness as H  # noqa: E402  (query registry, dataset generator, protocol)

ROOT = Path(__file__).resolve().parents[1]

#: §14.3 measures the parallel scan, so the set is the full-window
#: scan-heavy queries — the ones eval_phase0 attributes to that stage.
THREAD_QUERIES = ("series.count", "coactive.narrow", "motif.filtered")

#: §15/§14.2 want a representative slice: a postings point lookup, a
#: traversal, two full-window scans, and the materialization-heavy diff.
REPR_QUERIES = ("hist.single", "snap.hop2", "series.count",
                "coactive.narrow", "diff.global", "motif.filtered")

#: §14.4's mix: point + traversal + two scan shapes, looped as one unit.
READER_MIX = ("hist.single", "snap.hop2", "series.count", "coactive.narrow")


# --------------------------------------------------------------------------- #
# store cache: one reference log per scale, one replayed store per backend
# --------------------------------------------------------------------------- #

def default_workdir() -> Path:
    return Path(os.environ.get("TMPDIR", "/tmp")) / "tgms-eval-resources"


def ensure_dataset(workdir: Path, scale: int) -> tuple[H.Dataset, Path]:
    """The reference event log for `scale`, built once and cached."""
    d = workdir / f"scale-{scale}"
    d.mkdir(parents=True, exist_ok=True)
    meta_p, log_p = d / "meta.json", d / "eventlog.jsonl"
    if meta_p.exists() and log_p.exists():
        m = json.loads(meta_p.read_text())
        return H.Dataset(log=log_p, scale=m["scale"], tt_epoch1=m["tt_epoch1"],
                         t0=m["t0"], t1=m["t1"],
                         filter_uids=tuple(m["filter_uids"]), name=m["name"]), d
    print(f"  building reference log at {scale:,} events ...", flush=True)
    data = H.build_dataset(scale)
    shutil.copy2(data.log, log_p)
    meta_p.write_text(json.dumps({
        "scale": data.scale, "tt_epoch1": data.tt_epoch1, "t0": data.t0,
        "t1": data.t1, "filter_uids": list(data.filter_uids),
        "name": data.name}) + "\n")
    return H.Dataset(log=log_p, scale=data.scale, tt_epoch1=data.tt_epoch1,
                     t0=data.t0, t1=data.t1, filter_uids=data.filter_uids,
                     name=data.name), d


def ensure_store(d: Path, backend: str, log: Path) -> Path:
    """Replay the reference log into `backend` once; the .ok marker commits
    the cache entry only after a complete replay, so an interrupted build
    is rebuilt rather than trusted."""
    store, ok = d / f"store-{backend}", d / f"store-{backend}.ok"
    if ok.exists():
        return store
    if store.exists():
        shutil.rmtree(store)
    print(f"  replaying into {backend} store ...", flush=True)
    t0 = time.perf_counter()
    H.load_store(store, backend, log)
    ok.write_text(f"replayed in {time.perf_counter() - t0:.1f}s\n")
    return store


def queries_for(data: H.Dataset, ids: tuple[str, ...]) -> list[dict[str, Any]]:
    reg = {q.id: q for q in H.registry(data.t0, data.t1, data.tt_epoch1,
                                       data.filter_uids)}
    return [{"id": i, "op": reg[i].op, "args": reg[i].args} for i in ids]


# --------------------------------------------------------------------------- #
# the worker: one process, one suite — every mode shells out to this
# --------------------------------------------------------------------------- #

def evict_store_pages(store: Path) -> int:
    """Ask the kernel to drop the page cache for every file under `store`.

    User-space eviction (plan §15 under a no-root constraint): DONTNEED
    drops clean pages, and a read-only store's pages are clean. Returns
    the number of files advised.
    """
    n = 0
    for p in sorted(store.rglob("*")):
        if not p.is_file():
            continue
        fd = os.open(p, os.O_RDONLY)
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
        finally:
            os.close(fd)
        n += 1
    return n


def _vm_status() -> dict[str, int]:
    """VmRSS/VmHWM in kB — the numbers of record for memory (ru_maxrss is
    fork-polluted, per the §13 postmortem). Empty off Linux."""
    out: dict[str, int] = {}
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:", "VmSize:")):
                k, v = line.split(":", 1)
                out[k.lower() + "_kb"] = int(v.split()[0])
    except OSError:
        pass
    return out


def _open_adapter(cfg: dict[str, Any]):
    from tgms.temporal.algebra import ensure_all_registered
    ensure_all_registered()
    backend = cfg["backend"]
    if backend == "duckdb" and cfg.get("duckdb_threads"):
        from tgms.storage.duckdb_adapter import DuckDBAdapter
        return DuckDBAdapter(Path(cfg["store"]) / "store.duckdb",
                             threads=int(cfg["duckdb_threads"]))
    import tgms
    return tgms.open(Path(cfg["store"]), backend=backend).adapter


def run_suite(cfg: dict[str, Any]) -> dict[str, Any]:
    """Execute one suite per the config; see `_suite_config` for the shape."""
    from tgms.temporal.algebra import call_operator

    if cfg.get("evict"):
        evict_store_pages(Path(cfg["store"]))
    t0 = time.perf_counter()
    adapter = _open_adapter(cfg)
    open_ms = (time.perf_counter() - t0) * 1e3

    queries = cfg["queries"]
    warmup = cfg.get("warmup", H.WARMUP)
    reps = cfg.get("reps")            # fixed count; None = fast/slow protocol
    duration = cfg.get("duration_s")  # loop the mix for this long instead

    results: list[dict[str, Any]] = []
    timings: dict[str, list[float]] = {q["id"]: [] for q in queries}
    errors: dict[str, str] = {}

    def one(q: dict[str, Any]):
        return call_operator(adapter, q["op"], dict(q["args"]))

    if duration is None:
        for q in queries:
            try:
                for _ in range(warmup):
                    payload = one(q)
                t = time.perf_counter()
                payload = one(q)
                first = (time.perf_counter() - t) * 1e3
                ts = [first]
                n = reps if reps is not None else (
                    H.REPS_FAST if first < 1000.0 else H.REPS_SLOW)
                for _ in range(n - 1):
                    t = time.perf_counter()
                    payload = one(q)
                    ts.append((time.perf_counter() - t) * 1e3)
                p50, p95 = H._p50_p95(ts)
                results.append({
                    "query": q["id"], "ok": True,
                    "hash": H.canonical_hash(payload),
                    "p50_ms": p50, "p95_ms": p95, "first_ms": round(ts[0], 3),
                    "rows": H._answer_size(payload),
                    "timings_ms": [round(x, 3) for x in ts]})
            except Exception as e:  # a refusal is data, not a crash
                results.append({"query": q["id"], "ok": False,
                                "error": f"{type(e).__name__}: {e}"[:160]})
        wall_s = None
        done = sum(len(r.get("timings_ms", [])) for r in results)
    else:
        for _ in range(warmup):     # one warm pass over the whole mix
            for q in queries:
                try:
                    one(q)
                except Exception:
                    pass
        if cfg.get("start_at"):     # barrier: readers overlap by wall clock
            while time.time() < cfg["start_at"]:
                time.sleep(0.01)
        t_begin = time.perf_counter()
        deadline = t_begin + duration
        done = 0
        while time.perf_counter() < deadline:
            for q in queries:
                try:
                    t = time.perf_counter()
                    one(q)
                    timings[q["id"]].append((time.perf_counter() - t) * 1e3)
                    done += 1
                except Exception as e:
                    errors[q["id"]] = f"{type(e).__name__}: {e}"[:160]
        wall_s = time.perf_counter() - t_begin
        for q in queries:
            ts = timings[q["id"]]
            if not ts:
                results.append({"query": q["id"], "ok": False,
                                "error": errors.get(q["id"], "no completions")})
                continue
            p50, p95 = H._p50_p95(ts)
            results.append({"query": q["id"], "ok": True, "p50_ms": p50,
                            "p95_ms": p95, "count": len(ts),
                            "timings_ms": [round(x, 3) for x in ts[:2000]]})

    return {"open_ms": round(open_ms, 3), "results": results,
            "wall_s": round(wall_s, 3) if wall_s is not None else None,
            "queries_done": done, **_vm_status()}


def _suite_config(store: Path, backend: str, queries: list[dict[str, Any]],
                  **kw: Any) -> dict[str, Any]:
    return {"store": str(store), "backend": backend, "queries": queries, **kw}


def spawn_suite(cfg: dict[str, Any], env_extra: dict[str, str] | None = None,
                argv: list[str] | None = None) -> subprocess.Popen:
    cmd = argv or [sys.executable, str(Path(__file__).resolve()), "_suite"]
    env = {**os.environ, **(env_extra or {})}
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env)
    assert p.stdin is not None
    p.stdin.write(json.dumps(cfg))
    p.stdin.close()
    p.stdin = None  # communicate() must not flush a closed pipe
    return p


def collect(p: subprocess.Popen, timeout_s: float | None = None,
            docker_name: str | None = None) -> dict[str, Any]:
    try:
        out, err = p.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        # killing the docker *client* leaves the container running; kill it
        # by name so a thrashing capped run cannot outlive its budget
        if docker_name:
            subprocess.run(["docker", "kill", docker_name],
                           capture_output=True, timeout=60)
        p.kill()
        out, err = p.communicate()
        return {"failed": True, "timed_out": True, "timeout_s": timeout_s,
                "stderr": (err or "").strip()[-2000:]}
    if p.returncode != 0:
        return {"failed": True, "returncode": p.returncode,
                "stderr": err.strip()[-2000:]}
    return json.loads(out)


# --------------------------------------------------------------------------- #
# mode: threads (§14.3)
# --------------------------------------------------------------------------- #

def cmd_threads(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    data, d = ensure_dataset(args.workdir, args.scale)
    queries = queries_for(data, THREAD_QUERIES)
    counts = [int(x) for x in args.threads.split(",")]
    systems = [s.strip() for s in args.systems.split(",") if s.strip()]

    records, mismatches = [], []
    hashes: dict[tuple[str, str], set[str]] = {}
    for backend in systems:
        store = ensure_store(d, backend, data.log)
        for t in counts:
            env = {"TGMS_SCAN_THREADS": str(t)} if backend == "native" else None
            cfg = _suite_config(store, backend, queries,
                                duckdb_threads=t if backend == "duckdb" else None)
            r = collect(spawn_suite(cfg, env_extra=env))
            records.append({"backend": backend, "threads": t, **r})
            for res in r.get("results", []):
                if res.get("ok"):
                    hashes.setdefault((backend, res["query"]), set()).add(res["hash"])
                    print(f"  {backend:>7} t={t:<3} {res['query']:<18} "
                          f"p50 {res['p50_ms']:>9.1f} ms", flush=True)
                else:
                    print(f"  {backend:>7} t={t:<3} {res['query']:<18} "
                          f"FAILED {res.get('error')}", flush=True)
    for (backend, qid), hs in sorted(hashes.items()):
        if len(hs) > 1:
            mismatches.append(f"{backend}/{qid}")
    if mismatches:
        print(f"\n  HASHES DIFFER ACROSS THREAD COUNTS: {', '.join(mismatches)}")
    else:
        print("\n  all thread counts agree on every result hash")
    return (1 if mismatches else 0), {
        "mode": "threads", "thread_counts": counts, "systems": systems,
        "queries": [q["id"] for q in queries], "records": records,
        "hash_agree_across_counts": not mismatches}


# --------------------------------------------------------------------------- #
# mode: coldwarm (§15)
# --------------------------------------------------------------------------- #

def cmd_coldwarm(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    data, d = ensure_dataset(args.workdir, args.scale)
    queries = queries_for(data, REPR_QUERIES)
    systems = [s.strip() for s in args.systems.split(",") if s.strip()]
    if not hasattr(os, "posix_fadvise"):
        print("  WARNING: no posix_fadvise on this platform — the 'cold' "
              "state cannot be produced here (Linux only)")
    records = []
    for backend in systems:
        store = ensure_store(d, backend, data.log)
        # warm: the published protocol, in one process
        warm = collect(spawn_suite(_suite_config(store, backend, queries)))
        records.append({"backend": backend, "state": "warm", **warm})
        for res in warm.get("results", []):
            if res.get("ok"):
                print(f"  {backend:>7} warm         {res['query']:<18} "
                      f"p50 {res['p50_ms']:>9.1f} ms", flush=True)
        # colder states: fresh process per trial, one query, one shot
        for state, evict in (("process_cold", False), ("cold", True)):
            for q in queries:
                trials = []
                for _ in range(args.trials):
                    cfg = _suite_config(store, backend, [q], warmup=0, reps=1,
                                        evict=evict)
                    trials.append(collect(spawn_suite(cfg)))
                firsts = [t["results"][0]["first_ms"] for t in trials
                          if t.get("results") and t["results"][0].get("ok")]
                opens = [t["open_ms"] for t in trials if "open_ms" in t]
                rec = {"backend": backend, "state": state, "query": q["id"],
                       "first_ms": sorted(firsts), "open_ms": sorted(opens),
                       "median_first_ms":
                           round(statistics.median(firsts), 3) if firsts else None,
                       "median_open_ms":
                           round(statistics.median(opens), 3) if opens else None,
                       "trials": trials}
                records.append(rec)
                print(f"  {backend:>7} {state:<12} {q['id']:<18} "
                      f"first {rec['median_first_ms']} ms "
                      f"(open {rec['median_open_ms']} ms)", flush=True)
    return 0, {"mode": "coldwarm", "systems": systems, "trials": args.trials,
               "eviction": "posix_fadvise(fd,0,0,POSIX_FADV_DONTNEED) per store "
                           "file (user-space; no root on the host)",
               "queries": [q["id"] for q in queries], "records": records}


# --------------------------------------------------------------------------- #
# mode: memcap (§14.2)
# --------------------------------------------------------------------------- #

def docker_memory_enforced() -> bool:
    """Can Docker actually enforce --memory here? Read the limit back from
    the container's own cgroup — an allocation canary is wrong on a host
    with swap but no swap-limit support (cgroup v1: memory is capped, the
    overflow swaps, so the canary survives while the cap is real)."""
    code = ("import pathlib\n"
            "for p in ('/sys/fs/cgroup/memory/memory.limit_in_bytes',"
            " '/sys/fs/cgroup/memory.max'):\n"
            "    q = pathlib.Path(p)\n"
            "    if q.exists():\n"
            "        print(q.read_text().strip()); break\n")
    try:
        r = subprocess.run(
            ["docker", "run", "--rm", "--memory", "256m",
             "python:3.12-slim", "python", "-c", code],
            capture_output=True, text=True, timeout=300)
        return int(r.stdout.strip()) <= 512 * 1024 * 1024
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return False


def _parse_cap(cap: str) -> int:
    units = {"g": 1 << 30, "m": 1 << 20, "k": 1 << 10}
    return int(float(cap[:-1]) * units[cap[-1].lower()]) if cap[-1].lower() in units \
        else int(cap)


def _site_packages() -> str:
    import sysconfig
    return sysconfig.get_paths()["purelib"]


def cmd_memcap(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    data, d = ensure_dataset(args.workdir, args.scale)
    queries = queries_for(data, REPR_QUERIES)
    store = ensure_store(d, "native", data.log)
    caps = [c.strip() for c in args.caps.split(",") if c.strip()]

    use_docker = args.enforce == "docker" or (
        args.enforce == "auto" and docker_memory_enforced())
    method = "docker" if use_docker else "rlimit_as"
    print(f"  enforcement: {method}", flush=True)

    # reduced repetitions: under a 2 GB cap a 10M scan can take minutes,
    # and the question is where the knee is, not a tight median
    proto = {"warmup": args.warmup, "reps": args.reps}
    records = []
    # uncapped reference under the identical reduced protocol
    ref = collect(spawn_suite(_suite_config(store, "native", queries, **proto)))
    records.append({"cap": None, "method": "none", **ref})
    for res in ref.get("results", []):
        if res.get("ok"):
            print(f"  uncapped     {res['query']:<18} p50 {res['p50_ms']:>9.1f} ms",
                  flush=True)

    def run_capped(cap: str, qs: list[dict[str, Any]], tag: str) -> dict[str, Any]:
        cfg = _suite_config(store, "native", qs, **proto)
        name = None
        if use_docker:
            name = f"tgms-memcap-{tag}-{os.getpid()}"
            mounts = sorted({str(ROOT), str(store.parents[1]),
                             _site_packages().split("/lib/")[0]})
            cmd = ["docker", "run", "--rm", "-i", "--name", name,
                   "--network", "none",
                   "--memory", cap, "--memory-swap", cap]
            for m in mounts:
                cmd += ["-v", f"{m}:{m}:ro"]
            # site-packages via PYTHONPATH skips .pth processing, so the
            # editable install does not resolve from it — the repo root is
            # appended explicitly instead (mounted at its host path).
            cmd += ["-e", f"PYTHONPATH={_site_packages()}:{ROOT}",
                    "python:3.12-slim", "python",
                    str(Path(__file__).resolve()), "_suite"]
            p = spawn_suite(cfg, argv=cmd)
        else:
            cfg["rlimit_as_bytes"] = _parse_cap(cap)
            p = spawn_suite(cfg)
        r = collect(p, timeout_s=args.cap_timeout, docker_name=name)
        r["oom"] = bool(r.get("failed")) and (
            r.get("returncode") in (137, -9)
            or "MemoryError" in r.get("stderr", ""))
        return r

    for cap in caps:
        if args.per_query:
            # one container per query: a whole-suite OOM says the *suite*
            # exceeds the cap; this says which queries do on their own
            for q in queries:
                r = run_capped(cap, [q], f"{cap}-{q['id'].replace('.', '-')}")
                records.append({"cap": cap, "method": method,
                                "query": q["id"], **r})
                if r.get("failed"):
                    what = ("timed out" if r.get("timed_out")
                            else "OOM-killed" if r["oom"] else "FAILED")
                    print(f"  cap {cap:<6} {q['id']:<18} {what} "
                          f"(rc={r.get('returncode')})", flush=True)
                else:
                    res = r["results"][0]
                    msg = (f"p50 {res['p50_ms']:>9.1f} ms  "
                           f"hwm {r.get('vmhwm_kb', 0) // 1024} MB"
                           if res.get("ok") else f"FAILED {res.get('error')}")
                    print(f"  cap {cap:<6} {q['id']:<18} {msg}", flush=True)
            continue
        r = run_capped(cap, queries, cap)
        records.append({"cap": cap, "method": method, **r})
        if r.get("failed"):
            what = ("timed out (thrash budget exceeded)" if r.get("timed_out")
                    else "OOM-killed" if r["oom"] else "FAILED")
            print(f"  cap {cap:<6} {what} (rc={r.get('returncode')})", flush=True)
        else:
            for res in r.get("results", []):
                msg = (f"p50 {res['p50_ms']:>9.1f} ms" if res.get("ok")
                       else f"FAILED {res.get('error')}")
                print(f"  cap {cap:<6} {res['query']:<18} {msg}", flush=True)
    swap_kb = None
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("SwapTotal:"):
                swap_kb = int(line.split()[1])
    except OSError:
        pass
    return 0, {"mode": "memcap", "caps": caps, "method": method,
               "per_query": bool(args.per_query),
               "host_swap_kb": swap_kb,
               "enforcement_note":
                   "docker --memory bounds residency; on a cgroup-v1 kernel "
                   "without swap-limit support the overflow may swap rather "
                   "than OOM, which is the working-set>RAM behavior §14.2 asks "
                   "about. RLIMIT_AS instead caps address space (mmapped store "
                   "files count), a strictly harsher approximation.",
               "protocol_note": f"reduced reps: warmup={args.warmup}, "
                                f"reps={args.reps} (feasibility under caps)",
               "queries": [q["id"] for q in queries], "records": records}


# --------------------------------------------------------------------------- #
# mode: readers (§14.4)
# --------------------------------------------------------------------------- #

def cmd_readers(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    data, d = ensure_dataset(args.workdir, args.scale)
    queries = queries_for(data, READER_MIX)
    store = ensure_store(d, "native", data.log)
    counts = [int(x) for x in args.readers.split(",")]

    # one warm pre-pass so every config starts from the same page cache
    collect(spawn_suite(_suite_config(store, "native", queries, warmup=1, reps=1)))

    records = []
    for n in counts:
        start_at = time.time() + args.barrier_s
        procs = [spawn_suite(_suite_config(
            store, "native", queries, warmup=1,
            duration_s=args.duration, start_at=start_at)) for _ in range(n)]
        readers = [collect(p) for p in procs]
        done = sum(r.get("queries_done", 0) for r in readers if not r.get("failed"))
        walls = [r["wall_s"] for r in readers if r.get("wall_s")]
        agg_qps = done / max(walls) if walls else 0.0
        per_query = {}
        for q in queries:
            p50s = [res["p50_ms"] for r in readers if not r.get("failed")
                    for res in r["results"]
                    if res["query"] == q["id"] and res.get("ok")]
            per_query[q["id"]] = {
                "reader_p50s_ms": [round(x, 3) for x in sorted(p50s)],
                "median_reader_p50_ms":
                    round(statistics.median(p50s), 3) if p50s else None}
        records.append({"readers": n, "aggregate_qps": round(agg_qps, 2),
                        "queries_done": done, "per_query": per_query,
                        "raw": readers})
        line = " ".join(f"{q['id']}={per_query[q['id']]['median_reader_p50_ms']}"
                        for q in queries)
        print(f"  n={n:<3} agg {agg_qps:8.2f} q/s | median reader p50: {line}",
              flush=True)
    return 0, {"mode": "readers", "reader_counts": counts,
               "duration_s": args.duration,
               "queries": [q["id"] for q in queries], "records": records}


# --------------------------------------------------------------------------- #

def manifest(args: argparse.Namespace, data: H.Dataset) -> dict[str, Any]:
    m = H.manifest(data, systems=["native"])
    m["protocol"]["resource_mode"] = args.mode
    return m


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_suite":
        # the worker path must work under a bare container python: no argparse
        # niceties, and rlimit is applied before any large import
        cfg = json.load(sys.stdin)
        if cfg.get("rlimit_as_bytes"):
            import resource
            cap = int(cfg["rlimit_as_bytes"])
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        # anything the store or an operator prints must not corrupt the
        # one-JSON-on-stdout contract
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        try:
            result = run_suite(cfg)
        finally:
            sys.stdout = real_stdout
        print(json.dumps(result))
        return 0

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["threads", "coldwarm", "memcap", "readers"])
    ap.add_argument("--scale", type=int, default=1_000_000)
    ap.add_argument("--workdir", type=Path, default=default_workdir())
    ap.add_argument("--json", type=Path)
    ap.add_argument("--systems", default="native,duckdb",
                    help="threads/coldwarm only")
    ap.add_argument("--threads", default="1,2,4,8,16,32")
    ap.add_argument("--trials", type=int, default=5, help="cold trials per query")
    ap.add_argument("--caps", default="2g,4g,8g")
    ap.add_argument("--enforce", choices=["auto", "docker", "rlimit"],
                    default="auto")
    ap.add_argument("--warmup", type=int, default=2, help="memcap warmups")
    ap.add_argument("--reps", type=int, default=5, help="memcap reps")
    ap.add_argument("--cap-timeout", type=float, default=3600.0,
                    help="wall budget per capped suite before it is killed")
    ap.add_argument("--per-query", action="store_true",
                    help="memcap: one container per query instead of one "
                         "per cap, to attribute an OOM to a query")
    ap.add_argument("--readers", default="1,2,4,8,16")
    ap.add_argument("--duration", type=float, default=45.0,
                    help="seconds each reader loops the mix")
    ap.add_argument("--barrier-s", type=float, default=20.0,
                    help="lead time for readers to open + warm before the window")
    args = ap.parse_args()

    args.workdir.mkdir(parents=True, exist_ok=True)
    data, _ = ensure_dataset(args.workdir, args.scale)
    meta = manifest(args, data)
    print(f"resource harness [{args.mode}] on {data.name} @ {data.scale:,} events")
    print(f"  commit {meta['commit']}{' (dirty)' if meta['dirty'] else ''} | "
          f"{meta['platform']} | {meta['cpu_count']} cores")
    if not meta["on_measurement_host"]:
        print(f"  WARNING: not {H.MEASUREMENT_HOST} — development run, timings "
              f"are not comparable with reported numbers")
    print()

    rc, payload = {"threads": cmd_threads, "coldwarm": cmd_coldwarm,
                   "memcap": cmd_memcap, "readers": cmd_readers}[args.mode](args)
    if args.json:
        args.json.write_text(json.dumps(
            {"manifest": meta, **payload}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        print(f"  wrote {args.json}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
