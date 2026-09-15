"""Build the **synth** family (`synth-1m-native`, `synth-10m-native`, and the
30M/100M records `docs/design/SCALE_BUILD_FORECAST_2026-09-15.md` (B7)
pre-registers), streamed and N-parametrised — the driver that section's §1a
says does not exist yet.

**Which family this is, and how it was identified.** The 200k/1M/10M synth
records (`benchmarks/results-v1/eval-10m-*.json`, `docs/site_facts.json`,
`docs/eval/DATASET_CARDS.md`'s "synth (200k / 1M / 10M events)" card) are all
built by `scripts/eval_harness.py::build_dataset` — deterministic from scale
alone, splitmix64 endpoints, constant average degree via `n_nodes`/`edge_life`
scaling together, 50-node communities at 70% intra-community share, one
10x burst band, and a second belief epoch (corrections + a retraction).
`scripts/build_synth_iv_store.py` builds a *different* family (`synth-iv-*`,
interval-valid-time, log-uniform lifetimes, epoch-1 only, for the M5 carve
campaign) — not this one; `docs/eval/DATASET_CARDS.md`'s own iv card is
explicit that it "reuses the synth community/degree structure" as a
*different* dataset, never an edit to `build_dataset`.

**What was wrong with reusing `build_dataset` directly at 30M/100M.** It
calls `store.ingest_events([_event(i, scale) for i in range(scale)])` — one
Python list of `scale` dicts, fully materialized before `ingest_events` ever
chunks it. That is tens of GB of dict objects at 100M before the store even
opens (SCALE_BUILD_FORECAST §1a). `Store.ingest_events` (`tgms/store.py`)
already re-chunks whatever iterable it is given into `INGEST_CHUNK=50_000`-row
write batches internally — the unstreamed *list comprehension* was the bug,
not the store's own chunking. This script fixes exactly that: every event is
generated from its own index, on demand, in batches this script builds
itself (`--batch`), and nothing upstream of a single batch is ever held in
memory as a full N-sized structure.

**Reused verbatim** from `eval_harness.py` (same community/degree/burst
shape, so a 50k build here is comparable in *structure* to the 200k/1M/10M
records): `n_nodes`, `edge_life`, `_vt_s`, `COMMUNITY`, `INTRA_PCT`, the
splitmix64 finalizer `_mix`, and the full four-phase op sequence of
`build_dataset` (bulk ingest -> belief-epoch node assertion -> epoch-2
corrections + one node correction -> partial mid-interval corrections ->
retractions).

**New: `--seed`.** `build_dataset` has no seed concept at all — it is
deterministic from `scale` alone (SCALE_BUILD_FORECAST §1b: "seed: none").
This script folds a seed into the two per-event splitmix64 draws by XORing
it into the mix input *before* the finalizer: `_mix((2*i + arm) ^ (seed <<
1))` for `arm` in `{0, 1}` (src, dst-selector). At `seed=0` this reduces
algebraically to exactly `_mix(2*i)` / `_mix(2*i+1)` — `build_dataset`'s own
formula, unchanged. `seed=0` is therefore the canonical value this family's
existing records (which predate `--seed`) are consistent with, and it is
what the equivalence test below (`tests/test_build_synth_store.py`) builds
against. Any other seed draws an independent, still fully-reproducible
graph — useful for A/B builds that must *not* share endpoints.

**Digest equivalence — what is provably true, and what is not.**
`Store.store_digest()` sorts every node/edge version by, among other things,
`tt_s` (and hashes `vid`, which is `sha256(identity:tt_s:vt_s)`), and `tt_s`
comes from `HybridLogicalClock.tick()` = `max(wall_clock_micros(), last_tt +
1)` (`tgms/core/clock.py`) — a **wall-clock** value. `docs/eval/
DATASET_CARDS.md`'s "Loading rule" and `scripts/check_digest_stability.py`'s
own docstring both already state this as settled fact (D-023): "independently
built stores of the same data legitimately differ in tt and every derived
id." That makes `store.digest()` equality between *any* two independent
ingests of identical logical content — old generator vs. this one, or even
this script run twice — structurally impossible, seed held equal or not.
This is not a bug this script introduces or could fix.

So the equivalence this script's test proves is over `content_digest()`
(below): the same sort keys `store_digest()` uses, minus `vid`/`tt_s`/`tt_e`
(all three wall-clock-derived), which is exactly the *logical* content a
replay-equivalence argument cares about. Proven at `--n-entities 50000
--seed 0 --batch 50000` against `eval_harness.build_dataset(50000)`: at that
`--batch`, this script's bulk phase is one `ingest_events` call of exactly
50,000 rows, identical to `build_dataset`'s own (`INGEST_CHUNK` is also
50,000, so `build_dataset`'s single big list was always chunked into exactly
one batch at this scale regardless). Matching that chunking removes the one
batch-size-sensitive edge case `ingest_events` has: an auto-created bare node
version's `vt_s` is the minimum `vt_s` among same-*chunk* occurrences before
its first believed version is asserted (`tgms/storage/base.py::_ingest_events`)
— cross-chunk, a node already known is never re-asserted, so this can only
ever matter for a handful of `--batch`-adjacent nodes, and only if
`--batch` differs from what produced the comparison. Every edge version's own
identity, endpoints, `rel_type`, `vt_s`/`vt_e` and props are batch-size
invariant (`disc` comes from a cumulative offset carried across chunks, never
reset per chunk), which `tests/test_build_synth_store.py`'s
`test_determinism_per_batch` checks directly by comparing edge-only content
digests across two different `--batch` values.

**Batching/cadence.** `--batch 250` and `--compact-every 1_000_000` are
`SCALE_BUILD_FORECAST_2026-09-15.md` §1b's frozen values for the real
30M/100M dispatch (bulk batch measured against `build_snb_store.py`'s
throughput floor; compact cadence scaled off `build_snb_store.py`'s own
`COMPACT_EVERY_OPS=100_000`). Both are plain flags here, not constants,
because a 50k laptop smoke build (this repo's own gate) wants a *different*
`--batch` for the equivalence proof above.

    uv run python scripts/build_synth_store.py --n-entities 50000 --seed 0 \\
        --batch 50000 --compact-every 1000000 --backend native \\
        --out /tmp/synth-50k-native

    # the real B7 dispatch shape (xzgpu only -- never this laptop):
    uv run python scripts/build_synth_store.py --n-entities 30000000 --seed 0 \\
        --batch 250 --compact-every 1000000 --backend native \\
        --out /mnt/project/xzhang/tgms/stores/synth-30m-native
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tgms  # noqa: E402
from tgms.core.model import EntityRef, digest  # noqa: E402

SCHEMA_VERSION = "1.0.0"

#: Frozen family shape, verbatim from `scripts/eval_harness.py` -- see the
#: module docstring's "Reused verbatim" paragraph. Never redefine these to
#: "fix" a scale-curve property; that would silently redefine every existing
#: synth-* record's comparability (mirrors `build_synth_iv_store.py`'s own
#: stated discipline for the same reason).
COMMUNITY = 50
INTRA_PCT = 70
_M64 = (1 << 64) - 1

#: SCALE_BUILD_FORECAST §1b (frozen for the real B7 dispatch); both are
#: ordinary flags below; these are only the argparse defaults.
DEFAULT_BATCH = 250
DEFAULT_COMPACT_EVERY = 1_000_000

#: "a progress line every 500k ops" (task spec) -- not exposed as a flag,
#: since nothing about it is dataset- or scale-specific.
PROGRESS_EVERY_OPS = 500_000

#: The ordered phases of one build, exactly `build_dataset`'s own sequence.
#: A resumed build starts at `state["phase"]`; every phase strictly before it
#: is already fully committed to the store and is not re-entered.
PHASES = ("bulk", "n1_assert", "epoch2_edges", "epoch2_node", "partial", "retract", "done")


# --------------------------------------------------------------------- #
# generator math -- verbatim from eval_harness.py except _row_mix's seed
# --------------------------------------------------------------------- #

def _mix(x: int) -> int:
    """splitmix64 finalizer -- verbatim from `eval_harness.py::_mix`."""
    x = (x * 0x9E3779B97F4A7C15) & _M64
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & _M64
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & _M64
    return x ^ (x >> 31)


def n_nodes(n_entities: int) -> int:
    """Verbatim from `eval_harness.py::n_nodes`."""
    return max(200, n_entities // 100)


def edge_life(n_entities: int) -> int:
    """Verbatim from `eval_harness.py::edge_life`."""
    return max(40, n_entities // 20)


def _vt_s(i: int, n_entities: int) -> int:
    """Verbatim from `eval_harness.py::_vt_s` -- one deliberate burst band."""
    b0 = n_entities // 2
    b1 = b0 + max(1, n_entities // 20)
    if b0 <= i < b1:
        return b0 + (i - b0) // 10
    return i


def _row_mix(i: int, arm: int, seed: int) -> int:
    """`_mix(2*i + arm)`, seed-salted. At `seed=0` this is bit-identical to
    `eval_harness.py::_event`'s `_mix(2*i)` / `_mix(2*i+1)` (see module
    docstring, "New: --seed"). Depends on nothing but `(i, arm, seed)`, so a
    row is reproducible from its own index alone -- no state crosses a batch
    boundary, which is a strictly stronger property than "per-batch
    reproducible"."""
    return _mix((2 * i + arm) ^ (seed << 1))


def _event(i: int, n_entities: int, seed: int) -> dict[str, Any]:
    """Event `i`: same construction as `eval_harness.py::_event`, with
    `_row_mix` (seeded) standing in for the bare `_mix(2*i)`/`_mix(2*i+1)`."""
    n, t = n_nodes(n_entities), _vt_s(i, n_entities)
    src = _row_mix(i, 0, seed) % n
    r = _row_mix(i, 1, seed)
    if r % 100 < INTRA_PCT:
        base = (src // COMMUNITY) * COMMUNITY
        dst = base + (r >> 8) % min(COMMUNITY, n - base)
    else:
        dst = (r >> 8) % n
    return {"src": f"n{src}", "dst": f"n{dst}",
            "rel_type": "R" if i % 3 else "S",
            "vt_s": t, "vt_e": t + edge_life(n_entities),
            # Explicit, never left to `ingest_events`'s own offset-based
            # default: `Store.ingest_events` (`tgms/store.py`) resets its
            # internal `offset` to 0 on *every top-level call*, accumulating
            # correctly only across the batches *within* one call. This
            # script commits the bulk phase as many separate calls (one per
            # `--batch`), so relying on the implicit default would silently
            # restart disc numbering at "#0" every batch, colliding discs
            # across batches and breaking every later `_edge_ref` lookup
            # (found by the equivalence smoke build below at --batch <
            # --n-entities). Stamping it here reproduces exactly what
            # `eval_harness.py::build_dataset`'s single, whole-list call gets
            # from the implicit default (`f"#{offset+i}"` with `offset`
            # accumulating over one call's own chunks), for any --batch.
            "disc": f"#{i}"}


def _edge_ref(i: int, n_entities: int, seed: int) -> EntityRef:
    """The identity `ingest_events` gave event `i` -- verbatim in spirit from
    `eval_harness.py::_edge_ref`, using `_event`'s own explicit `disc`."""
    e = _event(i, n_entities, seed)
    return EntityRef(kind="edge", src=e["src"], dst=e["dst"],
                     rel_type=e["rel_type"], disc=e["disc"])


# --------------------------------------------------------------------- #
# content digest -- tt-independent equivalence notion (see module docstring)
# --------------------------------------------------------------------- #

def _strip_tt(row: dict[str, Any]) -> dict[str, Any]:
    row = dict(row)
    row.pop("vid", None)
    row.pop("tt_s", None)
    row.pop("tt_e", None)
    return row


def content_digest(store: Any, *, kinds: tuple[str, ...] = ("nodes", "edges")) -> str:
    """A digest over logical content only: every field `store_digest()`
    hashes, minus `vid`/`tt_s`/`tt_e` (all three derived from the wall-clock
    transaction clock -- see module docstring, "Digest equivalence"). Two
    independent builds of the same logical events, corrections and
    retractions, in the same order, produce the same `content_digest()` even
    though their `store.digest()` never will.

    `kinds` restricts to `("edges",)` to compare only edge-version content,
    which is fully batch-size invariant (unlike an auto-created node
    version's `vt_s` -- see module docstring's discussion of `--batch`).
    """
    payload: dict[str, Any] = {}
    if "nodes" in kinds:
        payload["nodes"] = sorted(
            (_strip_tt(v.to_json()) for v in store.adapter.all_node_versions()),
            key=lambda r: (r["uid"], r["vt_s"], r["vt_e"], json.dumps(r["props"], sort_keys=True)))
    if "edges" in kinds:
        payload["edges"] = sorted(
            (_strip_tt(v.to_json()) for v in store.adapter.all_edge_versions()),
            key=lambda r: (r["eid"], r["vt_s"], r["vt_e"], json.dumps(r["props"], sort_keys=True)))
    return digest(payload)


# --------------------------------------------------------------------- #
# resource / bytes accounting
# --------------------------------------------------------------------- #

def _rss_kb() -> dict[str, int]:
    """Cross-platform peak-RSS snapshot (`ru_maxrss` alone is already the
    lifetime peak, kB on Linux / bytes on macOS) -- same shape as
    `scripts/eval_bitemporal.py::_rss_kb` / `tgms/eval/storm_dag.py::_rss_kb`."""
    out: dict[str, int] = {}
    status = Path("/proc/self/status")
    if status.exists():  # Linux
        for line in status.read_text().splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                k, v = line.split(":")
                out[k.strip().lower()] = int(v.split()[0])
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    out["maxrss_kb"] = ru // 1024 if sys.platform == "darwin" else ru
    return out


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _store_bytes(out: Path) -> dict[str, int]:
    native = out / "native"
    manifest_bytes = _dir_bytes(native / "manifests")
    segment_bytes = _dir_bytes(native / "seg")
    return {"manifest_bytes": manifest_bytes, "segment_bytes": segment_bytes,
            "total_bytes": _dir_bytes(out)}


def _git_commit() -> str:
    env = os.environ.get("TGMS_COMMIT")
    if env:
        return env
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                               capture_output=True, text=True, check=True).stdout.strip()
        return f"{sha}-dirty" if dirty else sha
    except Exception:                                  # noqa: BLE001
        return "0" * 40


def _ram_gb() -> float:
    try:
        return round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / (1024 ** 3), 2)
    except (ValueError, OSError, AttributeError):       # pragma: no cover
        return 1.0


def _engine_build_info() -> dict[str, Any] | None:
    try:
        from tgms.storage.native import NativeAdapter
    except ImportError:
        return None
    return NativeAdapter.build_info()


# --------------------------------------------------------------------- #
# resumable, streaming build
# --------------------------------------------------------------------- #

def _progress_path(out: Path) -> Path:
    return out / ".build_progress.json"


def _load_progress(out: Path) -> dict[str, Any] | None:
    p = _progress_path(out)
    return json.loads(p.read_text()) if p.exists() else None


def _save_progress(out: Path, state: dict[str, Any]) -> None:
    _progress_path(out).write_text(json.dumps(state, sort_keys=True))


class _StopBuild(Exception):
    """Raised internally to unwind out of the phase loop at --stop-at-ops."""


class Builder:
    """Drives one (possibly resumed) build through `PHASES` in order,
    committing every op through the normal `Store` write API, compacting on
    cadence, and printing progress every `PROGRESS_EVERY_OPS` ops."""

    def __init__(self, store: Any, out: Path, n_entities: int, seed: int,
                batch: int, compact_every: int, stop_at_ops: int | None,
                ops_done: int, since_compact: int = 0) -> None:
        self.store = store
        self.out = out
        self.n_entities = n_entities
        self.seed = seed
        self.batch = batch
        self.compact_every = compact_every
        self.stop_at_ops = stop_at_ops
        self.ops = ops_done
        # Persisted exactly (not recomputed as `ops_done % compact_every`):
        # a compaction resets this to 0 even when the triggering tick
        # overshot the threshold, so the true remainder can drift from a
        # clean multiple of compact_every after many compactions.
        self._since_compact = since_compact
        self._last_progress_mark = ops_done // PROGRESS_EVERY_OPS
        self.t0 = time.time()
        self.compactions = 0

    # -- bookkeeping shared by every phase ------------------------------ #

    def _resume_state(self, phase: str, next_i: int | None) -> dict[str, Any]:
        return {"phase": phase, "next_i": next_i, "ops_done": self.ops,
                "since_compact": self._since_compact,
                "n_entities": self.n_entities, "seed": self.seed}

    def _tick(self, n: int, phase: str, next_i: int | None) -> None:
        """`phase`/`next_i` describe where a build should resume if it stops
        right after this call -- for an indexed/bulk phase that is always
        *this* phase at the next index (the range self-corrects to a no-op
        if it turns out to be exhausted); for a one-shot phase (`n1_assert`,
        `epoch2_node`) the op above already fully completed it, so the
        caller passes the *next* phase name instead (see `run_single`) —
        otherwise a resume would re-run an already-committed op."""
        self.ops += n
        self._since_compact += n
        if self.compact_every and self._since_compact >= self.compact_every:
            self._compact()
        mark = self.ops // PROGRESS_EVERY_OPS
        if mark > self._last_progress_mark:
            self._last_progress_mark = mark
            self._print_progress()
            # Piggyback a checkpoint on the same cadence: an unclean exit
            # (OOM, kill -9, power loss) with no --stop-at-ops still leaves a
            # progress file no older than PROGRESS_EVERY_OPS ops behind,
            # instead of only ever checkpointing at phase boundaries.
            _save_progress(self.out, self._resume_state(phase, next_i))
        if self.stop_at_ops is not None and self.ops >= self.stop_at_ops:
            _save_progress(self.out, self._resume_state(phase, next_i))
            raise _StopBuild()

    def _compact(self) -> None:
        compact = getattr(self.store.adapter, "compact", None)
        gc = getattr(self.store.adapter, "gc", None)
        if compact is not None and gc is not None:
            compact()
            gc(keep_last=2)
            self.compactions += 1
        self._since_compact = 0

    def _print_progress(self) -> None:
        elapsed = time.time() - self.t0
        rate = self.ops / elapsed if elapsed > 0 else 0.0
        rss = _rss_kb()
        nbytes = _store_bytes(self.out)
        postings_stats = getattr(self.store.adapter, "postings_stats", None)
        postings = postings_stats() if callable(postings_stats) else None
        print(f"  {self.ops:>14,} ops  {rate:>10,.0f} ops/s  elapsed {elapsed:>8.1f}s  "
              f"maxrss {rss.get('maxrss_kb', 0) / 1024:.0f} MB  "
              f"manifest {nbytes['manifest_bytes']:,} B  segment {nbytes['segment_bytes']:,} B  "
              f"compactions {self.compactions}"
              + (f"  postings {postings}" if postings is not None else ""), flush=True)

    # -- phases ---------------------------------------------------------- #

    def run_bulk(self, next_i: int) -> None:
        n_entities, seed, batch = self.n_entities, self.seed, self.batch
        lo = next_i
        while lo < n_entities:
            hi = min(lo + batch, n_entities)
            rows = [_event(i, n_entities, seed) for i in range(lo, hi)]
            self.store.ingest_events(rows)
            lo = hi
            self._tick(len(rows), "bulk", lo)

    def run_single(self, next_phase: str, next_i: int | None,
                  fn: Callable[[], Any]) -> None:
        """Run a one-shot op (`n1_assert`, `epoch2_node`). `next_phase`/
        `next_i` is where a build resumes if it stops right after -- always
        the *next* phase, since the single op above is unconditionally done
        once `fn()` returns and re-running it would duplicate a commit."""
        fn()
        self._tick(1, next_phase, next_i)

    def run_indexed(self, phase: str, idx_range: range,
                    op_fn: Callable[[int], Any]) -> None:
        step = idx_range.step
        for i in idx_range:
            op_fn(i)
            self._tick(1, phase, i + step)


def _phase_ranges(n_entities: int) -> dict[str, range]:
    """Index domains for the range-based phases -- exactly `build_dataset`'s
    own loops (`scripts/eval_harness.py`)."""
    step = max(1, n_entities // 200)
    life, mid = edge_life(n_entities), n_entities // 2
    return {
        "epoch2_edges": range(0, n_entities, step),
        "partial": range(mid - life // 2, mid, max(1, life // 20)),
        "retract": range(0, n_entities, step * 4),
    }


def build(out: Path, n_entities: int, seed: int, batch: int, compact_every: int,
         backend: str, resume: bool, stop_at_ops: int | None) -> dict[str, Any]:
    """Run (or resume) one build. Returns a result dict; `result["complete"]`
    is False if `--stop-at-ops` cut the build short."""
    existing = (out / "eventlog.jsonl").exists()
    if existing and not resume:
        raise SystemExit(f"{out} already has a store -- pass --resume to continue it, "
                         f"or remove it deliberately and re-run.")
    state = _load_progress(out) if existing else None
    if resume and existing and state is None:
        raise SystemExit(f"{out} exists but has no {_progress_path(out).name} -- cannot "
                         f"safely resume (it may already be a finished build, or was not "
                         f"built by this script).")
    if state is not None:
        if state["n_entities"] != n_entities or state["seed"] != seed:
            raise SystemExit(f"resume parameter mismatch: in-progress build was "
                             f"--n-entities {state['n_entities']} --seed {state['seed']}, "
                             f"this invocation asked for --n-entities {n_entities} "
                             f"--seed {seed}")
        phase, next_i, ops_done = state["phase"], state.get("next_i"), state["ops_done"]
        since_compact = state.get("since_compact", 0)
    else:
        phase, next_i, ops_done, since_compact = "bulk", 0, 0, 0

    store = tgms.open(out, backend=backend)
    b = Builder(store, out, n_entities, seed, batch, compact_every, stop_at_ops, ops_done,
               since_compact=since_compact)
    ranges = _phase_ranges(n_entities)
    order = PHASES
    start_idx = order.index(phase)

    def resumed(idx_range: Any) -> Any:
        if next_i is None:
            return idx_range
        return range(max(idx_range.start, next_i), idx_range.stop, idx_range.step)

    try:
        if start_idx <= order.index("bulk"):
            b.run_bulk(next_i if phase == "bulk" else 0)
            _save_progress(out, b._resume_state("n1_assert", None))
        if start_idx <= order.index("n1_assert"):
            b.run_single("epoch2_edges", ranges["epoch2_edges"].start, lambda: store.assert_node(
                "n1", "Node", {"name": "alpha"}, vt_s=0, vt_e=n_entities))
            _save_progress(out, b._resume_state("epoch2_edges", ranges["epoch2_edges"].start))
        if start_idx <= order.index("epoch2_edges"):
            r = resumed(ranges["epoch2_edges"]) if phase == "epoch2_edges" else ranges["epoch2_edges"]
            b.run_indexed("epoch2_edges", r, lambda i: store.correct(
                _edge_ref(i, n_entities, seed), {"weight": 2},
                vt_s=_event(i, n_entities, seed)["vt_s"],
                vt_e=_event(i, n_entities, seed)["vt_e"]))
            _save_progress(out, b._resume_state("epoch2_node", None))
        if start_idx <= order.index("epoch2_node"):
            b.run_single("partial", ranges["partial"].start, lambda: store.correct(
                EntityRef(kind="node", uid="n1"), {"name": "alpha-corrected"},
                vt_s=0, vt_e=n_entities))
            _save_progress(out, b._resume_state("partial", ranges["partial"].start))
        if start_idx <= order.index("partial"):
            life = edge_life(n_entities)
            r = resumed(ranges["partial"]) if phase == "partial" else ranges["partial"]
            b.run_indexed("partial", r, lambda i: store.correct(
                _edge_ref(i, n_entities, seed), {"weight": 3},
                vt_s=_event(i, n_entities, seed)["vt_s"] + life // 2,
                vt_e=_event(i, n_entities, seed)["vt_e"]))
            _save_progress(out, b._resume_state("retract", ranges["retract"].start))
        if start_idx <= order.index("retract"):
            r = resumed(ranges["retract"]) if phase == "retract" else ranges["retract"]
            b.run_indexed("retract", r, lambda i: store.retract(
                _edge_ref(i, n_entities, seed),
                _event(i, n_entities, seed)["vt_s"] + edge_life(n_entities) // 2))
            _save_progress(out, b._resume_state("done", None))
    except _StopBuild:
        wall = time.time() - b.t0
        stats = store.stats()
        result = {"complete": False, "ops": b.ops, "wall_s": round(wall, 3),
                  "stats": stats, "compactions": b.compactions,
                  "rss": _rss_kb(), "bytes": _store_bytes(out)}
        store.close()
        return result

    # done: fold the tail, final measurements
    b._compact()
    wall = time.time() - b.t0
    stats = store.stats()
    store_digest = store.digest()
    cdigest = content_digest(store)
    rss = _rss_kb()
    nbytes = _store_bytes(out)
    store.close()
    _progress_path(out).unlink(missing_ok=True)
    result = {"complete": True, "ops": b.ops, "wall_s": round(wall, 3), "stats": stats,
             "compactions": b.compactions, "store_digest": store_digest,
             "content_digest": cdigest, "rss": rss, "bytes": nbytes}
    record = _make_record(out, n_entities, seed, batch, compact_every, backend, result)
    (out / "build-record.json").write_text(json.dumps(record, indent=1, sort_keys=True))
    result["record"] = record
    return result


# --------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------- #

def _safe_relative(p: Path) -> bool:
    try:
        p.relative_to(ROOT)
        return True
    except ValueError:
        return False


def _make_record(out: Path, n_entities: int, seed: int, batch: int, compact_every: int,
                 backend: str, result: dict[str, Any]) -> dict[str, Any]:
    """`benchmarks/schema/result_manifest.schema.json`-conforming sidecar for
    one completed build, plus a `build_info` block with everything this
    driver measured. Written by `build()` itself (not `main()`) so a direct
    caller -- the test suite included -- gets the same sidecar a CLI run
    would, without going through `argparse`."""
    sha = _git_commit()
    record_path = out / "build-record.json"
    return {
        "schema_version": SCHEMA_VERSION,
        "git_commit": sha,
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "machine": {"host": platform.node(), "platform": platform.platform(),
                   "cpus": os.cpu_count() or 1, "ram_gb": _ram_gb()},
        "config": {"n_entities": n_entities, "batch": batch,
                  "compact_every": compact_every, "backend": backend},
        "seed": {"value": seed},
        "dataset": {"name": "synth", "digest": result["store_digest"],
                   "digest_kind": "store_digest"},
        "result_digest": result["content_digest"],
        "protocol": {"warmups": 0, "reps": 1, "ceilings": {}},
        "record": str(record_path.relative_to(ROOT)) if _safe_relative(record_path) else str(record_path),
        "build_info": {
            "engine": _engine_build_info(),
            "n_entities": n_entities,
            "seed": seed,
            "batch": batch,
            "compact_every": compact_every,
            "compactions": result["compactions"],
            "ops": result["ops"],
            "wall_s": result["wall_s"],
            "peak_rss": result["rss"],
            "store_bytes": result["bytes"],
            "stats": result["stats"],
            "content_digest": result["content_digest"],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n-entities", type=int, required=True,
                    help="event count -- the old generator's --scale, same math "
                        "(n_nodes = max(200, N//100), edge_life = max(40, N//20))")
    ap.add_argument("--seed", type=int, default=0,
                    help="0 reproduces eval_harness.build_dataset exactly (no --seed "
                        "flag exists there); any other value draws an independent, "
                        "still fully reproducible graph")
    ap.add_argument("--batch", type=int, default=DEFAULT_BATCH,
                    help=f"bulk-ingest commit size (default {DEFAULT_BATCH}, "
                        f"SCALE_BUILD_FORECAST §1b)")
    ap.add_argument("--compact-every", type=int, default=DEFAULT_COMPACT_EVERY,
                    help=f"compact()+gc(keep_last=2) every this many ops, 0 disables "
                        f"(default {DEFAULT_COMPACT_EVERY}, SCALE_BUILD_FORECAST §1b)")
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--backend", default="native", choices=("native",),
                    help="only native is streaming-safe at this scale")
    ap.add_argument("--resume", action="store_true",
                    help="continue an in-progress build at --out (refused without "
                        "this flag if --out already has a store)")
    ap.add_argument("--stop-at-ops", type=int, default=None,
                    help="stop after this many total ops (A/B-style partial builds); "
                        "resumable with --resume")
    args = ap.parse_args()

    sha = _git_commit()
    print(f"RUN_STARTED commit={sha} dataset=synth n_entities={args.n_entities} "
          f"seed={args.seed} batch={args.batch} compact_every={args.compact_every} "
          f"out={args.out} backend={args.backend} resume={args.resume} "
          f"host={platform.node()}", flush=True)

    result = build(args.out, args.n_entities, args.seed, args.batch,
                   args.compact_every, args.backend, args.resume, args.stop_at_ops)

    if not result["complete"]:
        print(f"\nSTOPPED at --stop-at-ops: {result['ops']:,} ops, "
             f"{result['wall_s']}s wall. Resume with the same command plus --resume.")
        return 0

    record_path = args.out / "build-record.json"
    print(f"\nDONE ops={result['ops']:,} wall={result['wall_s']}s "
         f"compactions={result['compactions']} "
         f"maxrss={result['rss'].get('maxrss_kb', 0) / 1024:.0f}MB "
         f"manifest={result['bytes']['manifest_bytes']:,}B "
         f"segment={result['bytes']['segment_bytes']:,}B")
    print(f"store_digest={result['store_digest']}")
    print(f"content_digest={result['content_digest']}")
    print(f"record: {record_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
