"""A/B digest equality between the DuckDB reference and the native engine.

The native backend is only substitutable if it is *indistinguishable* at the
logical level, and `store_digest` is the check that says so (D-023). This
replays one event log into both backends and compares.

Replay — never re-ingest. A fresh ingest stamps transaction times from the
clock at write time, so two independent builds of the same data legitimately
differ; that is precisely why D-023 vaulted event logs and added `tgms
replay`. Comparing two ingests would fail for a reason that has nothing to do
with the engine.

    uv run python scripts/ab_digest.py                    # frozen logs
    uv run python scripts/ab_digest.py --random 6         # randomized logs
    uv run python scripts/ab_digest.py path/to/log.jsonl  # a specific log
"""

from __future__ import annotations

import argparse
import random
import sys
import tempfile
import time
from pathlib import Path

import tgms
from tgms.core.model import EntityRef
from tgms.storage.duckdb_adapter import DuckDBAdapter
from tgms.storage.eventlog import replay
from tgms.storage.native import NativeAdapter

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "benchmarks/frozen-v1"


def synth_log(seed: int, ops: int = 60) -> Path:
    """A log exercising every write shape: asserts, carves, corrections,
    retracts, and the failures that must stay in the log (D-004)."""
    d = Path(tempfile.mkdtemp()) / "src"
    st = tgms.open(d, backend="duckdb")
    rng = random.Random(seed)
    for i in range(ops):
        k = rng.random()
        if k < 0.45:
            st.assert_edge(
                f"n{rng.randrange(6)}", f"n{rng.randrange(6)}", "R",
                {"w": rng.randrange(5)}, vt_s=rng.randrange(50),
                vt_e=rng.randrange(50, 100), disc=f"#{i}",
            )
        elif k < 0.70:
            st.assert_node(
                f"n{rng.randrange(6)}", "N", {"p": rng.randrange(4)},
                vt_s=rng.randrange(50), vt_e=rng.randrange(50, 100),
            )
        elif k < 0.85:
            try:
                st.correct(
                    EntityRef(kind="node", uid=f"n{rng.randrange(6)}"),
                    {"p": 99}, vt_s=rng.randrange(40), vt_e=rng.randrange(40, 90),
                )
            except Exception:
                pass  # a failed batch stays in the log and must re-fail on replay
        else:
            try:
                st.retract(EntityRef(kind="node", uid=f"n{rng.randrange(6)}"),
                           rng.randrange(50))
            except Exception:
                pass
    st.close()
    return d / "eventlog.jsonl"


def compare(log: Path) -> tuple[bool, str, str, float, float]:
    t0 = time.time()
    duck = DuckDBAdapter(":memory:")
    replay(log, duck)
    a = duck.store_digest()
    t_duck = time.time() - t0

    t0 = time.time()
    native = NativeAdapter(Path(tempfile.mkdtemp()) / "native")
    replay(log, native)
    b = native.store_digest()
    t_native = time.time() - t0
    return a == b, a, b, t_duck, t_native


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("logs", nargs="*", type=Path, help="event logs to compare")
    ap.add_argument("--random", type=int, default=0, help="also compare N random logs")
    args = ap.parse_args()

    targets: list[tuple[str, Path]] = [(str(p), p) for p in args.logs]
    if not args.logs:
        targets += [
            (p.name, p) for p in sorted(FROZEN.glob("*.eventlog.jsonl"))
        ]
    targets += [(f"random-seed-{s}", synth_log(s)) for s in range(args.random)]

    if not targets:
        print("nothing to compare", file=sys.stderr)
        return 2

    failures = 0
    for name, log in targets:
        if not log.exists():
            print(f"{name}: MISSING {log}", file=sys.stderr)
            failures += 1
            continue
        same, a, b, t_duck, t_native = compare(log)
        status = "MATCH" if same else "DIFFER"
        print(
            f"{name}: {status}\n"
            f"    duckdb {a[:32]}  ({t_duck:.1f}s)\n"
            f"    native {b[:32]}  ({t_native:.1f}s)"
        )
        failures += not same

    print(f"\n{len(targets) - failures}/{len(targets)} logs agree")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
