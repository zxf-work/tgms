"""XTDB's resource footprints — the cells the D-082 receipts do not cover.

Normalized resource reporting (D-070 item 5) needs three footprints per
system: canonical data, query-ready (disk + indexes + resident memory), and
cold start. For native and DuckDB at 1M these already exist in
`eval-resources-coldwarm-1m-d082.json` (suite VmHWM + fresh-process first
query) and the store receipts. This script measures the missing XTDB cells
against the same reference-log construction:

- store bytes after replay (canonical/query-ready disk — one number, since
  XTDB's Arrow files are its indexes);
- container resident memory after a query pass (docker stats, the JVM heap
  included — that is the honest query-ready floor for a JVM system);
- cold start: `docker stop` + `start` on the same container, time to first
  successful query (the pgwire endpoint answering is XTDB's "ready").

    python scripts/eval_footprints.py --scale 1000000 --density 5 --json out.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import eval_harness as H  # noqa: E402
from eval_bitemporal import build_log  # noqa: E402
from xtdb_baseline import (  # noqa: E402
    replay, start_container, ts, wait_ready,
)

import tempfile  # noqa: E402

import tgms  # noqa: E402


def container_mem_bytes(name: str) -> int:
    r = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", name],
        capture_output=True, text=True)
    # "1.234GiB / 92.7GiB"
    used = r.stdout.split("/")[0].strip()
    units = {"KiB": 2**10, "MiB": 2**20, "GiB": 2**30, "B": 1}
    for suffix, mult in units.items():
        if used.endswith(suffix):
            return int(float(used[: -len(suffix)]) * mult)
    raise ValueError(f"unparseable mem usage: {r.stdout!r}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scale", type=int, default=1_000_000)
    ap.add_argument("--density", type=float, default=5.0)
    ap.add_argument("--port", type=int, default=54331)
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    print(f"reference log at {args.scale}/{args.density}% …", flush=True)
    data, _counts = build_log(args.scale, args.density)

    native_path = Path(tempfile.mkdtemp(prefix="tgms-fp-native-")) / "store"
    H.load_store(native_path, "native", data.log)
    store = tgms.open(native_path, backend="native")
    adapter = store.adapter
    sample_uid = next(iter({v.uid for v in adapter.all_node_versions()}))
    final_tt = max(v.tt_s for v in adapter.all_node_versions())
    store.close()

    name = start_container(args.port)
    rec: dict = {"scale": args.scale, "density_pct": args.density}
    try:
        conn = wait_ready(args.port)
        cur = conn.cursor()
        print("replaying into xtdb …", flush=True)
        t0 = time.perf_counter()
        rec["replay"] = replay(conn, Path(data.log))
        print(f"  {time.perf_counter() - t0:.0f}s", flush=True)

        du = subprocess.run(["docker", "exec", name, "du", "-sb", "/var/lib/xtdb"],
                            capture_output=True, text=True)
        rec["store_bytes"] = int(du.stdout.split()[0])

        # a query pass, then resident memory — the query-ready floor
        for _ in range(20):
            cur.execute("SELECT * FROM nodes WHERE _id = %s", (sample_uid,))
            cur.fetchall()
            cur.execute(
                "SELECT _valid_from, props FROM nodes FOR SYSTEM_TIME AS OF %s "
                "WHERE _id = %s", (ts(final_tt), sample_uid))
            cur.fetchall()
        rec["mem_after_suite_bytes"] = container_mem_bytes(name)
        conn.close()

        # cold start: stop/start the same container, time to first answer
        print("cold start …", flush=True)
        subprocess.run(["docker", "stop", name], capture_output=True, check=True)
        t0 = time.perf_counter()
        subprocess.run(["docker", "start", name], capture_output=True, check=True)
        conn = wait_ready(args.port, timeout_s=600)
        cur = conn.cursor()
        t_first = time.perf_counter()
        cur.execute("SELECT * FROM nodes WHERE _id = %s", (sample_uid,))
        cur.fetchall()
        now = time.perf_counter()
        rec["cold_start_s"] = {
            "to_ready": round(t_first - t0, 2),
            "first_query_ms": round((now - t_first) * 1000, 2),
            "total_to_first_answer": round(now - t0, 2),
        }
        rec["mem_after_cold_query_bytes"] = container_mem_bytes(name)
        print(f"  ready {rec['cold_start_s']['to_ready']}s, first query "
              f"{rec['cold_start_s']['first_query_ms']}ms", flush=True)
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    rec["manifest"] = {
        "commit": subprocess.run(["git", "rev-parse", "HEAD"],
                                 capture_output=True, text=True).stdout.strip(),
        "image": "ghcr.io/xtdb/xtdb",
    }
    if args.json:
        args.json.write_text(json.dumps(rec, indent=1) + "\n")
        print(f"record → {args.json}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
