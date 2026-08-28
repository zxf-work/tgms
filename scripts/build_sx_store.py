"""Build an sx-mathoverflow / sx-superuser store, then gate it against SNAP's
published stats (`docs/eval/DATASET_CARDS.md`, `docs/DECISIONS.md` D-089).

**SERVER-SIDE EXECUTION ONLY.** Per `docs/design/M5_CAMPAIGN_FREEZE_2026-08-27.md`
§A2/§A10: *"Every scored store is built server-side on xzgpu with a receipt
... New stores are built server-side, never on a laptop, with a build
receipt."* This script does not enforce that mechanically (it cannot know
what host it is on beyond `platform.node()`, which it stamps into the
receipt for a human or CI check to gate on) — it is a rule for whoever runs
this script, not a precondition the script itself refuses to start
without. Do not run this against `sx-superuser` or the scored
`sx-mathoverflow` build off a laptop; a local smoke run against a throwaway
`--out` directory, discarded and disclosed per §A10's "local shakedown"
rule, is the only laptop-side use this script condones.

**The loading rule, verbatim from `DATASET_CARDS.md` / D-089**: three raw
files per dataset, one per edge type (`A2Q` answer-to-question, `C2Q`
comment-to-question, `C2A` comment-to-answer), streamed in that **fixed
order**. The recorded event log is therefore deterministic, but valid time
interleaves across types — a real tt(cid: at ingest) vs vt (event
timestamp) workload the single-file datasets (CollegeMsg, bitcoinotc) don't
produce. This script adds no mapping decision of its own beyond the file
order and the `rel_type` tag each file gets; it batches, writes, and
checks, mirroring `scripts/build_snb_store.py`'s own discipline.

**Node identity.** The sx datasets ship no separate node file — a "node" is
just an integer id that appears as a `SRC` or `DST` column value somewhere
across the three files. Nodes are therefore never explicitly asserted;
they ride `Store.ingest_events`' implicit dense-id registration (the same
path `tgms/data/loaders.py::ingest_dataset` already uses for CollegeMsg and
bitcoinotc), and the verification gate below counts them the same way
`Store.stats()['n_entities']` does.

**Raw file format** (verified against `data_raw/sx-mathoverflow-a2q.txt.gz`
at authoring time): `SRC DST UNIX_SECONDS`, one edge per line, whitespace
separated — SNAP's own temporal-edge format, identical to
`tgms/data/loaders.py::snap_edge_stream`'s CollegeMsg/bitcoinotc format.
Unix seconds are converted to microseconds (`* 1_000_000`), matching
`snap_edge_stream`'s own convention, so a store built here compares on the
same `vt` units as every other TGMS store.

    uv run python scripts/build_sx_store.py \\
        --dataset sx-mathoverflow --raw-dir data_raw --out stores/sx-mathoverflow

Exit status is 0 only if the fidelity gate passes against SNAP's published
counts (`DATASET_CARDS.md` / D-089). A mismatch is a mapping defect or a
data surprise; it is printed in full and it blocks.
"""

from __future__ import annotations

import argparse
import gzip
import json
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: The three edge types, in the **fixed** streaming order (D-089 / the
#: DATASET_CARDS.md loading rule). Never re-ordered — re-ordering changes
#: the recorded event log's byte content and every derived id downstream
#: of it (D-023: independently built stores of the same data legitimately
#: differ; changing the read order is the same kind of divergence, self-
#: inflicted).
EDGE_TYPES: tuple[str, ...] = ("a2q", "c2q", "c2a")
REL_TYPE_OF: dict[str, str] = {"a2q": "A2Q", "c2q": "C2Q", "c2a": "C2A"}

#: SNAP's published per-dataset stats, exactly as `DATASET_CARDS.md` and
#: D-089 record them — the fidelity gate's other half, restated here so a
#: mismatch is caught before the build is ever cited elsewhere.
EXPECTED: dict[str, dict[str, Any]] = {
    "sx-mathoverflow": {
        "events": 506_550, "nodes": 24_818, "days": 2_350,
        "per_type": {"A2Q": 107_581, "C2Q": 203_639, "C2A": 195_330},
    },
    "sx-superuser": {
        "events": 1_443_339, "nodes": 194_085, "days": 2_773,
        "per_type": {"A2Q": 430_033, "C2Q": 479_067, "C2A": 534_239},
    },
}

DAY_US = 86_400 * 1_000_000


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


def _raw_path(raw_dir: Path, dataset: str, edge_type: str) -> Path:
    return raw_dir / f"{dataset}-{edge_type}.txt.gz"


def edge_events(raw_dir: Path, dataset: str) -> Iterator[dict[str, Any]]:
    """Pass over the three raw files, **in `EDGE_TYPES` order**. Each line
    is `SRC DST UNIX_SECONDS`; `rel_type` is the file's own type tag, never
    read off the row (the sx files carry no type column of their own —
    the file *is* the type, per SNAP's own split)."""
    for edge_type in EDGE_TYPES:
        path = _raw_path(raw_dir, dataset, edge_type)
        if not path.exists():
            raise FileNotFoundError(
                f"missing raw file for {dataset!r} edge type {edge_type!r}: {path}")
        rel_type = REL_TYPE_OF[edge_type]
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(("#", "%")):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    raise ValueError(
                        f"{path}: malformed row (want 'SRC DST UNIX_SECONDS'): {line!r}")
                s, d, t = parts[:3]
                yield {"src": f"n{s}", "dst": f"n{d}", "rel_type": rel_type,
                      "vt_s": int(t) * 1_000_000}


def counted(stream: Iterator[dict[str, Any]], per_type: dict[str, int],
           t0_box: list[int | None]) -> Iterator[dict[str, Any]]:
    """Tallies per-`rel_type` counts and the min `vt_s` seen, as records are
    emitted — the same "count while streaming, never materialize" discipline
    `build_snb_store.py::tally` uses, since holding 500k-1.4M dicts in a
    Python list just to count them is pure waste."""
    for rec in stream:
        per_type[rec["rel_type"]] = per_type.get(rec["rel_type"], 0) + 1
        if t0_box[0] is None or rec["vt_s"] < t0_box[0]:
            t0_box[0] = rec["vt_s"]
        yield rec


def _identity(store: Any, mode: str) -> dict[str, Any]:
    """Verbatim in spirit from `build_snb_store.py::_identity` — `manifest`
    (default) is the generation counter plus the manifest sha the engine
    already stamps its own verify walks with; `full` is `Store.digest()`,
    the replay-equivalence digest, practical at this scale (500k-1.4M
    versions, unlike SF1's 20.4M) but still opt-in rather than default."""
    if mode == "none":
        return {"mode": "none"}
    if mode == "full":
        return {"mode": "full", "store_digest": store.digest()}
    out: dict[str, Any] = {"mode": "manifest", "store_identity": store.store_identity}
    for name in ("generation",):
        try:
            out[name] = getattr(store.adapter, name)
        except Exception:                              # noqa: BLE001
            pass
    try:
        out["manifest_sha"] = store.adapter._store.manifest_sha()  # noqa: SLF001
    except Exception:                                  # noqa: BLE001
        pass
    return out


@dataclass
class BuildResult:
    per_type: dict[str, int]
    total_events: int
    n_entities: int
    n_edge_versions: int
    days: int
    wall_s: float
    compact_s: float
    compactions: int
    digest: dict[str, Any]
    store_bytes: int


def build(raw_dir: Path, out: Path, dataset: str, backend: str,
         node_label: str = "User", compact_every: int = 100_000,
         digest_mode: str = "manifest") -> BuildResult:
    import tgms

    if out.exists():
        raise SystemExit(f"{out} exists — refusing to write into a live store. "
                         f"Remove it deliberately, then re-run.")
    store = tgms.open(out, backend=backend)
    per_type: dict[str, int] = {}
    t0_box: list[int | None] = [None]
    t0 = time.time()

    stream = counted(edge_events(raw_dir, dataset), per_type, t0_box)
    # `ingest_events` chunks internally at `Store.INGEST_CHUNK` (50,000); at
    # sx scale (500k-1.4M events -> 10-29 chunks) that is far short of the
    # regime where `build_snb_store.py::COMPACT_EVERY_OPS` was needed
    # (SF1's ~81,600 batches at a 250-op batch size) — DATASET_CARDS.md /
    # D-089 already record a 6s smoke ingest at mathoverflow scale on a
    # laptop with no compaction at all. `compact_every` is offered anyway,
    # for hygiene parity with `build_snb_store.py` and for `sx-superuser`'s
    # ~29 chunks, and is a no-op cost-wise at this scale either way.
    store.ingest_events(stream, node_label=node_label)

    compact = getattr(store.adapter, "compact", None)
    gc = getattr(store.adapter, "gc", None)
    compact_s = 0.0
    compactions = 0
    if compact_every and compact is not None and gc is not None:
        tc = time.time()
        compact()
        gc(keep_last=2)
        compact_s = time.time() - tc
        compactions = 1

    wall = time.time() - t0
    stats = store.stats()
    digest = _identity(store, digest_mode)
    store_bytes = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    store.close()

    total_events = sum(per_type.values())
    days = ((stats.get("vt_max", 0) - t0_box[0]) // DAY_US) if t0_box[0] is not None else 0
    return BuildResult(
        per_type=per_type, total_events=total_events,
        n_entities=stats.get("n_entities", 0), n_edge_versions=stats.get("n_edge_versions", 0),
        days=int(days), wall_s=round(wall, 1), compact_s=round(compact_s, 1),
        compactions=compactions, digest=digest, store_bytes=store_bytes)


def fidelity(dataset: str, result: BuildResult) -> tuple[bool, list[str]]:
    """Exact-match gate against SNAP's published stats (`DATASET_CARDS.md`
    / D-089) — the same discipline `build_snb_store.py::fidelity` uses:
    "exact or nothing", printed whether it passes or fails."""
    want = EXPECTED[dataset]
    lines: list[str] = []
    ok = True
    for rel_type, want_n in want["per_type"].items():
        got_n = result.per_type.get(rel_type, 0)
        ok &= got_n == want_n
        lines.append(f"edge {rel_type:<5} want {want_n:>10,}  got {got_n:>10,}  "
                     f"{'ok' if got_n == want_n else 'MISMATCH'}")
    ok &= result.total_events == want["events"]
    lines.append(f"TOTAL events      want {want['events']:>10,}  got {result.total_events:>10,}  "
                 f"{'ok' if result.total_events == want['events'] else 'MISMATCH'}")
    ok &= result.n_edge_versions == want["events"]
    lines.append(f"n_edge_versions   want {want['events']:>10,}  got {result.n_edge_versions:>10,}  "
                 f"{'ok' if result.n_edge_versions == want['events'] else 'MISMATCH'}")
    ok &= result.n_entities == want["nodes"]
    lines.append(f"n_entities        want {want['nodes']:>10,}  got {result.n_entities:>10,}  "
                 f"{'ok' if result.n_entities == want['nodes'] else 'MISMATCH'}")
    # days is a derived, rounding-sensitive figure (min/max vt over a
    # microsecond clock); DATASET_CARDS.md's own figure is itself a rounded
    # span, so this line is REPORTED, not gated — a one-day drift from
    # rounding is not a mapping defect, and folding it into `ok` would make
    # the gate flaky for a reason that has nothing to do with correctness.
    lines.append(f"extent days       want ~{want['days']:>6,}  got ~{result.days:>6,}  "
                 f"(reported, not gated)")
    return ok, lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, choices=sorted(EXPECTED),
                    help="which sx dataset to build")
    ap.add_argument("--raw-dir", default="data_raw",
                    help="directory holding <dataset>-{a2q,c2q,c2a}.txt.gz")
    ap.add_argument("--out", required=True)
    ap.add_argument("--backend", default="native", choices=("native", "duckdb"))
    ap.add_argument("--node-label", default="User")
    ap.add_argument("--compact-every", type=int, default=100_000,
                    help="fold segments after ingest (0 disables); see build()'s "
                         "docstring -- a no-op at sx scale either way")
    ap.add_argument("--digest", default="manifest", choices=("none", "manifest", "full"),
                    dest="digest_mode",
                    help="store identity for the receipt; 'full' is the "
                         "replay-equivalence digest (practical at this scale, "
                         "unlike SF1's)")
    args = ap.parse_args()

    raw_dir, out = Path(args.raw_dir), Path(args.out)
    sha = _sha()
    print(f"RUN_STARTED commit={sha} dataset={args.dataset} raw_dir={raw_dir} out={out} "
          f"backend={args.backend} digest={args.digest_mode} host={platform.node()}",
          flush=True)

    result = build(raw_dir, out, args.dataset, args.backend, args.node_label,
                   args.compact_every, args.digest_mode)
    ok, lines = fidelity(args.dataset, result)

    print("\n=== mapping-fidelity gate ===")
    for line in lines:
        print(line)
    print(f"\nGATE: {'PASS' if ok else 'FAIL'}")

    card = {
        "dataset": args.dataset,
        "note": ("SNAP Stack-Exchange interaction network, three typed edge "
                 "files (A2Q/C2Q/C2A) streamed in fixed order per "
                 "docs/eval/DATASET_CARDS.md and docs/DECISIONS.md D-089. "
                 "SNAP material; not an LDBC/other-benchmark result."),
        "commit": sha,
        "host": platform.node(),
        "platform": platform.platform(),
        "backend": args.backend,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wall_s": result.wall_s,
        "compact_s": result.compact_s,
        "compactions": result.compactions,
        "events": result.total_events,
        "n_entities": result.n_entities,
        "n_edge_versions": result.n_edge_versions,
        "per_type": result.per_type,
        "extent_days_approx": result.days,
        "store_identity": result.digest,
        "store_bytes": result.store_bytes,
        "expected": EXPECTED[args.dataset],
        "fidelity_gate": "PASS" if ok else "FAIL",
        "fidelity_table": lines,
    }
    (out / "dataset_card.json").write_text(json.dumps(card, indent=1, sort_keys=True, default=str))
    print(f"\ncard: {out / 'dataset_card.json'}")
    print(f"identity: {result.digest}  wall: {result.wall_s}s  bytes: {result.store_bytes:,}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
