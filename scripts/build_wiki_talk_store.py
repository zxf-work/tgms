"""Build the SNAP `wiki-talk-temporal` store, then gate it against SNAP's
published stats (`docs/eval/DATASET_CARDS.md`, `docs/DECISIONS.md` D-089).

**Why a new script rather than an extension of `build_sx_store.py`.**
`build_sx_store.py`'s shape is built around *three typed edge files per
dataset* (`EDGE_TYPES`, `REL_TYPE_OF`, a per-`rel_type` fidelity table) —
wiki-talk is the opposite case D-089 names it for: one file, one edge type
(`TALK`), instantaneous events. Bending that structure to a single file
would mean gutting most of it. The other reason is scale: wiki-talk is
~15x sx-mathoverflow's event count (7,833,140 vs 506,550), which is where
`build_snb_store.py`'s manifest-growth lesson (manifest bytes are
O(batches^2) in the number of *published generations*, not in dataset
size — `COMPACT_EVERY_OPS`'s docstring there has the measurements) starts
to matter rather than being a rounding error the way it is at sx scale
(`build_sx_store.py`'s own `build()` docstring calls its `compact_every`
"a no-op cost-wise... either way" at 500k-1.4M events). Getting periodic
compaction *during* ingest — not just once at the end — needs a driving
loop that chunks the event stream itself, since `Store.ingest_events`
chunks and publishes a manifest per 50,000-event chunk
(`tgms/store.py::INGEST_CHUNK`) with no hook in between. `build()` below
therefore replicates that internal chunking loop verbatim (same op shape,
same offset bookkeeping — see `tgms/store.py` lines ~262-273) rather than
calling `store.ingest_events()` in one shot, purely so `maybe_compact()`
can run between chunks. This adds no mapping decision of its own; it is
the same chunking `ingest_events` already does, with a hook spliced in.

**SERVER-SIDE EXECUTION ONLY.** Per `docs/design/M5_CAMPAIGN_FREEZE_2026-08-27.md`
§A2/§A10 (restated from `build_sx_store.py`, which this script otherwise
mirrors in discipline): *"Every scored store is built server-side on xzgpu
with a receipt... New stores are built server-side, never on a laptop,
with a build receipt."* This script cannot enforce that mechanically — it
stamps `platform.node()` into the receipt for a human or CI check to gate
on. The dataset card's own text is blunter still: "Build the store on a
server; the raw file alone is 7.8M lines." A local smoke run against a
throwaway `--out` (and, per below, a *truncated* raw file) is the only
laptop-side use this script condones.

**The loading rule, verbatim from `DATASET_CARDS.md`'s "Loading rule (all
datasets, all systems)"**: one recorded event log per dataset; every
system loads *that* (TGMS backends by replay, baselines from the canonical
rows a native store produces). wiki-talk has exactly one raw file and one
edge type, so there is no file-order decision to make (unlike the sx
pair) — the log's determinism here comes entirely from streaming the one
file in its own on-disk order, never sorted or reshuffled.

**Node identity.** wiki-talk ships no separate node file — a "node" is
just an integer id that appears as a `SRC` or `DST` column value
somewhere in the one raw file. Nodes are therefore never explicitly
asserted; they ride `Store.ingest_events`'/`_ingest_events`'s implicit
dense-id registration (the same path `tgms/data/loaders.py::ingest_dataset`
already uses for CollegeMsg and bitcoinotc, and `build_sx_store.py` uses
for the sx pair), and the verification gate below counts them the same
way `Store.stats()['n_entities']` does.

**Raw file format** (verified against the actual local
`data_raw/wiki-talk-temporal.txt.gz` at authoring time, all 7,833,140
lines scanned): `SRC DST UNIX_SECONDS`, one edge per line, whitespace
separated — SNAP's own temporal-edge format, identical to
`tgms/data/loaders.py::snap_edge_stream`'s CollegeMsg/bitcoinotc format
and to `build_sx_store.py`'s per-file format. Unix seconds are converted
to microseconds (`* 1_000_000`), matching `snap_edge_stream`'s own
convention. **Card-vs-file check (flagging, not absorbing, per the task):**
the file has *no* `#`/`%` comment lines at all (a full scan confirms zero),
so the defensive skip below is dead code for this file specifically — it
is kept anyway for parity with every other SNAP loader in this repo
(`snap_edge_stream`, `build_sx_store.py::edge_events`), none of which
special-case their source file's actual comment-freedom either. Every
line has exactly 3 whitespace-separated fields (verified: `awk '{print
NF}' | sort -u` over the full file yields only `3`). A full streaming
tally against the file also reproduces DATASET_CARDS.md's numbers
exactly: 7,833,140 lines, 1,140,149 distinct ids, and a
(max_t - min_t)/86400 span of 2,320.42 days (`~2,320` in the card is that
figure rounded down, same "reported, not gated" treatment `build_sx_store.py`
gives its own `days` figure). Nothing here contradicts the card.

**Memory.** This script never materializes the file: `edge_events` is a
generator over one `gzip.open(..., "rt")` handle, `counted()` tallies
inline as records pass through, and `build()`'s own driving loop (see
above) holds at most one chunk (`--chunk`, default 50,000 event dicts) in
a Python list at a time — the same bound `Store.ingest_events` itself
uses internally. **Expected peak RSS held by *this script's Python code*:
one chunk of small dicts (`{src, dst, rel_type, vt_s}`, 4 short-string/int
fields each) plus O(1) counters — at the default 50,000-event chunk that
is on the order of 15-25 MB of Python objects, independent of the 7.8M-line
file size.** That bound does **not** cover the native store/adapter's own
resident memory (segment buffers, the manifest, mmap'd pages for ~1.14M
node ids and ~7.83M edge versions) — that memory is the engine's, not this
script's, this script does not control it beyond the `--chunk`/
`--compact-every` knobs below, and it was not measured here: this file's
verification ran only the truncated smoke build (see module-level `if
__name__` usage note in the repo root), not a real 7.8M-event build, so
there is no measured full-scale RSS to report and none is claimed.

    uv run python scripts/build_wiki_talk_store.py \\
        --raw data_raw/wiki-talk-temporal.txt.gz --out stores/wiki-talk

Exit status is 0 only if the fidelity gate passes against SNAP's published
counts (`DATASET_CARDS.md` / D-089). A mismatch is a mapping defect or a
data surprise; it is printed in full and it blocks.

**Local verification against a truncated input.** The default gate is
exact-match against the *real* SNAP counts and is not bypassable by
omission — pass `--expect-counts EVENTS,NODES` explicitly to override it
(e.g. against a scratch `.gz` holding only the file's first ~200k lines).
Supplying `--expect-counts` marks the resulting `dataset_card.json` as
`"canonical": false` with the override recorded alongside the real
published stats, so a truncated-input card can never be mistaken for (or
silently substituted as) a real build's receipt. Leaving `--expect-counts`
unset is the only way to run the default build, and that build always
gates against the real counts above.
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

#: The dataset's single edge type — wiki-talk ships no type column of its
#: own (it *is* one type, per SNAP's own split into per-type files for the
#: sx datasets vs. one file here).
REL_TYPE = "TALK"

#: SNAP's published stats, exactly as `DATASET_CARDS.md` and D-089 record
#: them for wiki-talk-temporal — the fidelity gate's other half, restated
#: here so a mismatch is caught before the build is ever cited elsewhere.
#: (Independently reproduced by a full streaming scan of the local raw
#: file at authoring time — see the module docstring's "Card-vs-file
#: check".)
EXPECTED: dict[str, int] = {"events": 7_833_140, "nodes": 1_140_149, "days": 2_320}

DAY_US = 86_400 * 1_000_000

#: Matches `Store.ingest_events`'s own internal chunk size
#: (`tgms/store.py::INGEST_CHUNK`) so this script's manual driving loop
#: (see module docstring) publishes exactly the manifests `ingest_events`
#: would have, just with a compaction hook between them.
DEFAULT_CHUNK = 50_000

#: `build_snb_store.py::COMPACT_EVERY_OPS`'s constant, reused verbatim: at
#: any batch size, manifest bytes are O(published generations^2), and
#: wiki-talk's ~157 chunks at the default `--chunk` (7,833,140 / 50,000)
#: is enough generations for that to be worth folding periodically rather
#: than once at the end, unlike sx scale where `build_sx_store.py` found
#: post-hoc-only compaction to be a no-op cost-wise.
DEFAULT_COMPACT_EVERY = 100_000


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


def edge_events(raw: Path) -> Iterator[dict[str, Any]]:
    """Stream the one raw file. Each line is `SRC DST UNIX_SECONDS`;
    `rel_type` is always `TALK` (the file carries no type column — see the
    module docstring's format note). Functionally identical to
    `tgms/data/loaders.py::snap_edge_stream`, reimplemented locally rather
    than imported so this builder stays self-contained, mirroring
    `build_sx_store.py::edge_events`'s own choice."""
    opener = gzip.open if raw.suffix == ".gz" else open
    with opener(raw, "rt", encoding="utf-8", newline="") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(("#", "%")):
                continue
            parts = line.split()
            if len(parts) < 3:
                raise ValueError(
                    f"{raw}: malformed row (want 'SRC DST UNIX_SECONDS'): {line!r}")
            s, d, t = parts[:3]
            yield {"src": f"n{s}", "dst": f"n{d}", "rel_type": REL_TYPE,
                  "vt_s": int(t) * 1_000_000}


def counted(stream: Iterator[dict[str, Any]], count_box: list[int],
           t0_box: list[int | None]) -> Iterator[dict[str, Any]]:
    """Tallies the total event count and the min `vt_s` seen, as records
    are emitted — the same "count while streaming, never materialize"
    discipline `build_sx_store.py::counted` / `build_snb_store.py::tally`
    use."""
    for rec in stream:
        count_box[0] += 1
        if t0_box[0] is None or rec["vt_s"] < t0_box[0]:
            t0_box[0] = rec["vt_s"]
        yield rec


def _chunks(it: Iterator[dict[str, Any]], n: int) -> Iterator[list[dict[str, Any]]]:
    buf: list[dict[str, Any]] = []
    for rec in it:
        buf.append(rec)
        if len(buf) >= n:
            yield buf
            buf = []
    if buf:
        yield buf


def _identity(store: Any, mode: str) -> dict[str, Any]:
    """Verbatim in spirit from `build_sx_store.py::_identity` /
    `build_snb_store.py::_identity` — `manifest` (default) is the
    generation counter plus the manifest sha the engine already stamps its
    own verify walks with; `full` is `Store.digest()`, the
    replay-equivalence digest. At wiki-talk's 7.83M edge versions that is
    closer to SF1's impractical regime than to sx's practical one
    (`build_snb_store.py::_identity`'s docstring measured SF1's 20.4M
    versions at 15.6 GB resident and unfinished after 50 minutes), so
    `full` is offered for parity but is **not** recommended at full scale
    here — it is opt-in, never default, and this script does not attempt
    to bound its cost the way it bounds the ingest loop's."""
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
    total_events: int
    n_entities: int
    n_edge_versions: int
    days: int
    wall_s: float
    compact_s: float
    compactions: int
    digest: dict[str, Any]
    store_bytes: int


def build(raw: Path, out: Path, backend: str, node_label: str = "User",
         chunk: int = DEFAULT_CHUNK, compact_every: int = DEFAULT_COMPACT_EVERY,
         digest_mode: str = "manifest") -> BuildResult:
    import tgms
    from tgms.storage.base import make_op

    if out.exists():
        raise SystemExit(f"{out} exists — refusing to write into a live store. "
                         f"Remove it deliberately, then re-run.")
    store = tgms.open(out, backend=backend)
    count_box = [0]
    t0_box: list[int | None] = [None]
    t0 = time.time()

    compact = getattr(store.adapter, "compact", None)
    gc = getattr(store.adapter, "gc", None)
    compact_s = [0.0]
    compactions = [0]
    since_compaction = [0]

    def maybe_compact(force: bool = False) -> None:
        """Fold the small per-chunk segments together and drop the
        manifests that referenced them — `build_snb_store.py::maybe_compact`,
        reused here at 15x sx-mathoverflow's scale where it stops being a
        rounding error. Skipped silently on a backend without either entry
        point, so this stays a native-store optimisation, not a
        precondition."""
        if not compact_every or compact is None or gc is None:
            return
        if not force and since_compaction[0] < compact_every:
            return
        if since_compaction[0] == 0:
            return
        t = time.time()
        compact()
        gc(keep_last=2)
        compact_s[0] += time.time() - t
        compactions[0] += 1
        since_compaction[0] = 0

    # Manual chunked write loop — see the module docstring's "Why a new
    # script" section for why this replicates `Store.ingest_events`'s own
    # internal chunking (`tgms/store.py` lines ~262-273) instead of calling
    # it in one shot: only this way does `maybe_compact()` get to run
    # between chunks. The op shape and `offset` bookkeeping below are
    # exactly what `ingest_events` builds itself; nothing here is a new
    # mapping decision.
    stream = counted(edge_events(raw), count_box, t0_box)
    offset = 0
    for rec_chunk in _chunks(stream, chunk):
        store._write([make_op(  # noqa: SLF001 — mirrors Store.ingest_events
            "ingest_events", events=rec_chunk, offset=offset, node_label=node_label,
            source="ingest", provenance_ref=None)])
        offset += len(rec_chunk)
        since_compaction[0] += len(rec_chunk)
        maybe_compact()
        if offset % 1_000_000 < chunk:
            rate = offset / max(time.time() - t0, 1e-9)
            print(f"  {offset:>12,} events  {rate:>9,.0f} ev/s  "
                  f"({compactions[0]} compactions, {compact_s[0]:,.0f}s)", flush=True)
    maybe_compact(force=True)                          # leave the store folded

    wall = time.time() - t0
    stats = store.stats()
    digest = _identity(store, digest_mode)
    store_bytes = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    store.close()

    total_events = count_box[0]
    days = ((stats.get("vt_max", 0) - t0_box[0]) // DAY_US) if t0_box[0] is not None else 0
    return BuildResult(
        total_events=total_events, n_entities=stats.get("n_entities", 0),
        n_edge_versions=stats.get("n_edge_versions", 0), days=int(days),
        wall_s=round(wall, 1), compact_s=round(compact_s[0], 1),
        compactions=compactions[0], digest=digest, store_bytes=store_bytes)


def fidelity(result: BuildResult, expected: dict[str, int],
            canonical: bool) -> tuple[bool, list[str]]:
    """Exact-match gate — the same "exact or nothing" discipline
    `build_sx_store.py::fidelity` / `build_snb_store.py::fidelity` use —
    against either SNAP's published stats (`canonical=True`, the default)
    or an explicit `--expect-counts` override for a truncated smoke run
    (`canonical=False`). `days` is reported, never gated, same reasoning
    as `build_sx_store.py::fidelity`: it is a rounding-sensitive derived
    figure and the card's own days figure is itself a rounded span."""
    lines: list[str] = []
    ok = True
    ok &= result.total_events == expected["events"]
    lines.append(f"TOTAL events      want {expected['events']:>10,}  got "
                 f"{result.total_events:>10,}  "
                 f"{'ok' if result.total_events == expected['events'] else 'MISMATCH'}")
    ok &= result.n_edge_versions == expected["events"]
    lines.append(f"n_edge_versions   want {expected['events']:>10,}  got "
                 f"{result.n_edge_versions:>10,}  "
                 f"{'ok' if result.n_edge_versions == expected['events'] else 'MISMATCH'}")
    ok &= result.n_entities == expected["nodes"]
    lines.append(f"n_entities        want {expected['nodes']:>10,}  got "
                 f"{result.n_entities:>10,}  "
                 f"{'ok' if result.n_entities == expected['nodes'] else 'MISMATCH'}")
    lines.append(f"extent days       want ~{expected['days']:>6,}  got ~{result.days:>6,}  "
                 f"(reported, not gated)")
    if not canonical:
        lines.append("NON-CANONICAL: gated against --expect-counts, not SNAP's "
                     "published stats — this card is a smoke-test receipt only.")
    return ok, lines


def _parse_expect_counts(raw: str) -> dict[str, int]:
    parts = raw.split(",")
    if len(parts) not in (2, 3):
        raise SystemExit(
            f"--expect-counts wants 'EVENTS,NODES[,DAYS]', got {raw!r}")
    try:
        events, nodes = int(parts[0]), int(parts[1])
        days = int(parts[2]) if len(parts) == 3 else 0
    except ValueError:
        raise SystemExit(f"--expect-counts wants integers, got {raw!r}")
    return {"events": events, "nodes": nodes, "days": days}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="data_raw/wiki-talk-temporal.txt.gz",
                    help="path to the raw SNAP file (SRC DST UNIX_SECONDS, "
                         "gzipped or not)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--backend", default="native", choices=("native", "duckdb"))
    ap.add_argument("--node-label", default="User")
    ap.add_argument("--chunk", type=int, default=DEFAULT_CHUNK,
                    help="events per write batch / manifest generation; see "
                         "DEFAULT_CHUNK -- matches Store.ingest_events's own "
                         "internal chunk size")
    ap.add_argument("--compact-every", type=int, default=DEFAULT_COMPACT_EVERY,
                    help="fold segments and drop superseded manifests every N "
                         "events (0 disables); see DEFAULT_COMPACT_EVERY -- "
                         "manifest bytes grow O(generations^2) without it, and "
                         "at wiki-talk's ~157 chunks that stops being free")
    ap.add_argument("--digest", default="manifest", choices=("none", "manifest", "full"),
                    dest="digest_mode",
                    help="store identity for the receipt; 'full' is the "
                         "replay-equivalence digest and is not recommended at "
                         "full wiki-talk scale -- see _identity's docstring")
    ap.add_argument("--expect-counts", default=None, metavar="EVENTS,NODES[,DAYS]",
                    help="override the fidelity gate for a non-canonical smoke "
                         "run (e.g. against a truncated raw file) -- the "
                         "resulting dataset_card.json is stamped canonical=false. "
                         "Omit for the real build, which always gates against "
                         "SNAP's published wiki-talk-temporal counts and refuses "
                         "on mismatch.")
    args = ap.parse_args()

    raw, out = Path(args.raw), Path(args.out)
    sha = _sha()
    #: Whether the gate is being run against the real published stats at
    #: all (True) vs. an explicit `--expect-counts` override (False). This
    #: is a *pre-gate* fact — it says nothing yet about whether the build
    #: actually matched. The card's own `canonical` field below is the
    #: *post-gate* claim and additionally requires `ok`; conflating the two
    #: was a defect a coordinator re-verification caught (a truncated,
    #: gate-failing default-counts run was writing `"canonical": true`
    #: because this flag alone was being reused as that field) — see the
    #: card comment below for the fix.
    no_override = args.expect_counts is None
    expected = EXPECTED if no_override else _parse_expect_counts(args.expect_counts)
    print(f"RUN_STARTED commit={sha} dataset=wiki-talk raw={raw} out={out} "
          f"backend={args.backend} digest={args.digest_mode} "
          f"override={not no_override} host={platform.node()}", flush=True)

    result = build(raw, out, args.backend, args.node_label, args.chunk,
                   args.compact_every, args.digest_mode)
    ok, lines = fidelity(result, expected, no_override)

    print("\n=== mapping-fidelity gate ===")
    for line in lines:
        print(line)
    print(f"\nGATE: {'PASS' if ok else 'FAIL'}")

    card = {
        "dataset": "wiki-talk",
        "note": ("SNAP wiki-talk-temporal, one raw file, single edge type "
                 "TALK, instantaneous events, per docs/eval/DATASET_CARDS.md "
                 "and docs/DECISIONS.md D-089. SNAP material; not an "
                 "LDBC/other-benchmark result."),
        "commit": sha,
        "host": platform.node(),
        "platform": platform.platform(),
        "backend": args.backend,
        "chunk": args.chunk,
        "compact_every": args.compact_every,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wall_s": result.wall_s,
        "compact_s": result.compact_s,
        "compactions": result.compactions,
        "events": result.total_events,
        "n_entities": result.n_entities,
        "n_edge_versions": result.n_edge_versions,
        "extent_days_approx": result.days,
        "store_identity": result.digest,
        "store_bytes": result.store_bytes,
        "expected": expected,
        "published_stats": EXPECTED,
        # `canonical` is the claim "this card can be trusted as a real,
        # gate-verified wiki-talk build" — that requires BOTH no
        # `--expect-counts` override AND the gate having actually passed.
        # Writing `no_override` alone here (dropping the `and ok`) was the
        # defect: a truncated default-counts run that correctly printed
        # `GATE: FAIL` and exited 1 was still landing a card claiming
        # `"canonical": true`, because canonicity was being decided purely
        # by "was an override given" and never re-checked against the gate
        # outcome that was computed two lines above. The card is written
        # either way (matching `build_sx_store.py`'s convention: always
        # write the card, always record the true gate result, never delete
        # the store on failure — see `fidelity_gate` below, which was
        # already unconditionally correct and is the field a reader should
        # check first regardless of `canonical`).
        "canonical": no_override and ok,
        "fidelity_gate": "PASS" if ok else "FAIL",
        "fidelity_table": lines,
    }
    (out / "dataset_card.json").write_text(json.dumps(card, indent=1, sort_keys=True, default=str))
    print(f"\ncard: {out / 'dataset_card.json'}")
    print(f"identity: {result.digest}  wall: {result.wall_s}s  bytes: {result.store_bytes:,}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
