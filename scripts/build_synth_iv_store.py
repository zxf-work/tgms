"""Build `synth-iv-60k` — the deterministic interval-valid-time synth store
`docs/design/M5_CARVE_POPULATION_PROPOSAL_2026-08-28.md` §5/§9 line 5 proposes
(DECISION 5, primary option, unratified at authoring time — see that memo's
header). Its normative input is that memo; this script implements it exactly
where it froze a parameter and documents, here and in the final report, every
place it left one open.

**Why this exists.** §1-§2 of the memo: the run-of-record's carve arm
(`benchmarks/m5-v1/carve-arm-{bitcoinotc,collegemsg}.json`) measured **0**
outside-window B/C/D changed trials against R-8's floor of 200, on both
scored stores — not because the injection matrix underperformed (R-7's
per-half floor was met with 2.4x-4.7x margin), but because `bitcoinotc` and
`collegemsg` are **instantaneous-event stores**: every believed interval is
`[t, t+1)` (`DATASET_CARDS.md`), so no correction placed outside a query
window can ever touch a version the window's value arm read. §2b's own
worked example (`FRESHNESS_SEMANTICS.md`:1538-1541) needs a believed
interval that **starts inside the window and ends far outside it**. No
instantaneous store can supply that structurally, regardless of the
injection matrix.

**What this script builds, and what it deliberately is not.**

* **A new builder, a new label** (`synth-iv-60k`), never an edit to
  `scripts/eval_harness.py::build_dataset` — that function's card promises
  determinism from scale alone for `synth-1m-native`/`synth-10m-native`
  (`BASELINE_FREEZE_2026-08-27.md`:159), and editing it would silently
  redefine those numbers without changing their label (memo §5, "The one
  piece of new code").
* **The same community/degree structure as `build_dataset`**, copied
  verbatim below (`N_NODES`, `_mix`, `_vt_s`, `COMMUNITY`, `INTRA_PCT`, the
  src/dst construction in `_event_iv`) rather than imported — this script
  stays self-contained, mirroring `build_sx_store.py`/`build_wiki_talk_store.py`'s
  own stated preference for local reimplementation over a cross-script
  import. Nothing here is a new mapping decision for that half of the
  generator; only the interval-length rule below is new.
* **One new piece of code: the interval-length distribution.** `build_dataset`
  gives every edge a constant lifetime, `edge_life(scale) = max(40, scale //
  20)` — exactly 5% of the extent at every scale, the "one band" the memo
  calls "the marginal one, and the one thing worth changing" (§5). Replaced
  here by `edge_life_iv`, frozen and explained at its own definition below.
* **Epoch-1 only.** `build_dataset`'s epoch-2 corrections/retraction
  (`eval_harness.py`:568-591) are omitted outright (memo §5, "Second design
  call, and it is deliberate"): *"The injected matrix must be the only
  thing that carves, or a trial's outcome is confounded by whether the
  generator happened to pre-carve the version it targeted."* This mirrors
  `M4_MEASURED_REPORT.md`'s own discipline, *"verified zero superseded
  versions before the run."* The one `assert_node("n1", ...)` belief-epoch
  marker (`eval_harness.py`:565-566) is kept: it is a fresh node assertion,
  not a correction of anything the edge population already carries, and it
  never touches an edge version the carve arm's `aggregate_events` cells
  could read, so it cannot pre-carve the substrate this memo cares about.

**Parameters the memo froze, implemented exactly:**

* scale **60,000** — chosen (memo §5, "Scale, and therefore cost") to match
  `collegemsg`'s measured 59,835 edge versions, so the s/trial cost fit is
  interpolation onto a measured point (2.52s est. vs 2.50s measured) rather
  than extrapolation.
* epoch-1 only (no corrections baked into the generator itself).
* interval lengths span **~0.5%-50%** of the store's valid-time extent
  (memo §5: *"a deterministic length distribution spanning roughly
  0.5%-50% of the extent, drawn from the event index by the existing
  splitmix64 finalizer, so every R-6 band has intervals both longer and
  shorter than its window"*).

**Parameters the memo left open — chosen here, deterministically, and
flagged in the report per the task instruction (never silently):**

1. **The exact shape of the length distribution within [0.5%, 50%].** The
   memo names the band, not the density inside it. Chosen: **log-uniform**
   (draw `u` uniform in `[0,1]` from the event's own splitmix64 mix, take
   `exp(log(0.5%) + u * (log(50%) - log(0.5%)))`), not linear-uniform.
   Reason: R-6's three window fractions (0.1%/1%/5%) are themselves
   geometrically spaced, not arithmetically, and a linear-uniform draw over
   [0.5%, 50%] would put ~90% of its mass above 5% (the band nearest R-6's
   *widest* window) and leave the 0.1%/1% bands thin. Log-uniform gives
   comparable density to every decade the R-6 windows actually probe.
2. **Which `_mix` seed feeds the length draw.** `build_dataset` already
   spends `_mix(2i)` (src) and `_mix(2i+1)` (dst) per event `i`. Chosen:
   `_mix(3 * i)` (i.e. an independent third stream, never `_mix(2i)`/
   `_mix(2i+1)` reused) — reusing either endpoint's mix would correlate
   interval length with which node an edge lands on, which nothing in the
   memo asks for and which would make the community structure and the
   length distribution silently entangled.
3. **The node count function.** Reused `build_dataset`'s own
   `n_nodes(scale) = max(200, scale // 100)` unchanged (600 nodes at
   scale=60,000) — the memo does not ask for a different node count, only
   a different interval-length rule, so nothing else about the generator's
   shape was touched.
4. **The fitness-probe's own window and probe edge.** Not specified by the
   memo at all (it asks for the self-check's *existence*, not its exact
   parameters). See `fitness_probe()`'s docstring for the frozen,
   deterministic selection rule (first event index whose drawn interval
   spans >= 20% of the extent) and why 20% was chosen.

**SERVER-SIDE EXECUTION ONLY for the canonical (scale=60,000) build**, per
`BASELINE_FREEZE_2026-08-27.md`/Addendum 4's "never a laptop, ever" rule,
restated from `build_sx_store.py`/`build_wiki_talk_store.py`. This script
cannot enforce that mechanically; it stamps `platform.node()` into the
receipt. Unlike the SNAP builders, a **local run of the real 60,000-scale
build is not a rule violation for smoke purposes** in the same way — memo
§5 budgets it at "< 5 min... seconds of compute" precisely because it is
synthetic, in-process generation, not a multi-hundred-MB download+ingest —
but the *scored* copy that ever feeds a top-up campaign must still be an
iTiger-receipted build (memo §8c point 5), never this laptop's.

    uv run python scripts/build_synth_iv_store.py --out stores/synth-iv-60k
    uv run python scripts/build_synth_iv_store.py --scale 2000 --out /tmp/synth-iv-smoke   # non-canonical

Exit status is 0 only if BOTH gates pass: the exact-count self-verification
gate (§ below) and the fitness probe. A mismatch or a failed probe is
printed in full and blocks — the fitness probe in particular is never
retried against a different index or window at runtime; see its own
docstring for why that would be "tuning silently."
"""

from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: The memo's own chosen scale (§5, "Scale, and therefore cost") — matches
#: `collegemsg`'s measured 59,835 edge versions closely enough that the
#: s/trial cost fit is interpolation, not extrapolation. A build at any
#: other `--scale` is a smoke/dev build and its card is marked non-canonical
#: (mirrors `build_wiki_talk_store.py`'s `--expect-counts` override
#: semantics, adapted: here the "expected counts" are always deterministically
#: derivable from `--scale` itself via `expected_counts()`, so there is no
#: separate override flag to pass — `--scale` alone decides both the build
#: and its own gate).
CANONICAL_SCALE = 60_000

#: --------------------------------------------------------------------
#: Reused verbatim from `scripts/eval_harness.py::build_dataset` (community
#: structure, degree mixing, the burst band) — see the module docstring's
#: "What this script builds" for why this is a local copy, not an import.
#: --------------------------------------------------------------------

_M64 = (1 << 64) - 1


def _mix(x: int) -> int:
    """splitmix64 finalizer — verbatim from `eval_harness.py::_mix`."""
    x = (x * 0x9E3779B97F4A7C15) & _M64
    x ^= x >> 30
    x = (x * 0xBF58476D1CE4E5B9) & _M64
    x ^= x >> 27
    x = (x * 0x94D049BB133111EB) & _M64
    return x ^ (x >> 31)


def n_nodes(scale: int) -> int:
    """Verbatim from `eval_harness.py::n_nodes` — constant average degree."""
    return max(200, scale // 100)


def _vt_s(i: int, scale: int) -> int:
    """Verbatim from `eval_harness.py::_vt_s` — one deliberate burst band."""
    b0 = scale // 2
    b1 = b0 + max(1, scale // 20)
    if b0 <= i < b1:
        return b0 + (i - b0) // 10
    return i


#: Verbatim from `eval_harness.py` — community size and intra-community share.
COMMUNITY = 50
INTRA_PCT = 70

#: --------------------------------------------------------------------
#: New: the interval-length distribution (memo §5's "one piece of new
#: code"). See the module docstring's "left open" list, items 1-2, for the
#: shape and the seed stream chosen.
#: --------------------------------------------------------------------

#: The frozen band, memo §5 verbatim: "roughly 0.5%-50% of the extent."
MIN_LIFE_FRAC = 0.005
MAX_LIFE_FRAC = 0.50
_LOG_MIN, _LOG_MAX = math.log(MIN_LIFE_FRAC), math.log(MAX_LIFE_FRAC)


def life_frac(i: int) -> float:
    """Event `i`'s interval length as a fraction of the store's extent —
    log-uniform in `[MIN_LIFE_FRAC, MAX_LIFE_FRAC]`, drawn from an
    independent third `_mix` stream (`_mix(3*i)`, never `_mix(2*i)` or
    `_mix(2*i+1)`, which src/dst already consume). See the module
    docstring's "left open" items 1-2 for why log-uniform and why a third
    stream."""
    u = _mix(3 * i) / float(_M64)
    return math.exp(_LOG_MIN + u * (_LOG_MAX - _LOG_MIN))


def edge_life_iv(i: int, scale: int) -> int:
    return max(1, round(life_frac(i) * scale))


def _event_iv(i: int, scale: int) -> dict[str, Any]:
    """Event `i`: same src/dst/community/rel_type construction as
    `eval_harness.py::_event`, with `edge_life_iv` in place of the constant
    `edge_life`."""
    n, t = n_nodes(scale), _vt_s(i, scale)
    src = _mix(2 * i) % n
    r = _mix(2 * i + 1)
    if r % 100 < INTRA_PCT:
        base = (src // COMMUNITY) * COMMUNITY
        dst = base + (r >> 8) % min(COMMUNITY, n - base)
    else:
        dst = (r >> 8) % n
    life = edge_life_iv(i, scale)
    return {"src": f"n{src}", "dst": f"n{dst}",
            "rel_type": "R" if i % 3 else "S",
            "vt_s": t, "vt_e": t + life}


def _edge_ref(i: int, scale: int) -> Any:
    """The identity `ingest_events` gave event `i` — verbatim in spirit
    from `eval_harness.py::_edge_ref` (batch-offset discriminator)."""
    from tgms.core.model import EntityRef
    e = _event_iv(i, scale)
    return EntityRef(kind="edge", src=e["src"], dst=e["dst"],
                     rel_type=e["rel_type"], disc=f"#{i}")


# ---------------------------------------------------------------------------
# exact-count self-verification (the "honest gate" — build_wiki_talk_store.py's
# canonical/non-canonical split, adapted: expected counts are DERIVED from
# scale by a dry pass over the same deterministic generator, never an
# external published stat, since this store has none)
# ---------------------------------------------------------------------------

def expected_counts(scale: int) -> dict[str, int]:
    """A dry pass over `_event_iv` — no store opened, no I/O beyond CPU —
    computing exactly what a build at this `scale` must produce: `scale`
    edge versions (`ingest_events` gives each event its own disc, so none
    supersede one another — Class A throughout, matching `eval_harness.py`'s
    own "every occurrence is a distinct logical edge" note) and the exact
    distinct node-id count the events touch, plus `"n1"` for the belief-epoch
    marker `build()` asserts unconditionally (counted here even on the
    off chance `"n1"` never appears as an edge endpoint at a given scale)."""
    ids: set[str] = {"n1"}
    for i in range(scale):
        e = _event_iv(i, scale)
        ids.add(e["src"])
        ids.add(e["dst"])
    return {"events": scale, "nodes": len(ids)}


@dataclass
class BuildResult:
    total_events: int
    n_entities: int
    n_edge_versions: int
    wall_s: float
    digest: dict[str, Any]
    store_bytes: int


def _sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short=12", "HEAD"],
                              cwd=ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:                                  # noqa: BLE001
        return "unknown"


def _identity(store: Any, mode: str) -> dict[str, Any]:
    """Verbatim in spirit from `build_sx_store.py::_identity` /
    `build_wiki_talk_store.py::_identity`."""
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


def _events_iter(scale: int) -> Iterator[dict[str, Any]]:
    for i in range(scale):
        yield _event_iv(i, scale)


def build(out: Path, backend: str, scale: int, node_label: str = "Node",
         digest_mode: str = "manifest") -> BuildResult:
    """Epoch-1 only: `ingest_events` over `_event_iv`, then the one belief-
    epoch node marker. No corrections, no retractions — see the module
    docstring's "Epoch-1 only" section for why."""
    import tgms

    if out.exists():
        raise SystemExit(f"{out} exists — refusing to write into a live store. "
                         f"Remove it deliberately, then re-run.")
    t0 = time.time()
    store = tgms.open(out, backend=backend)
    store.ingest_events(_events_iter(scale), node_label=node_label)
    store.assert_node("n1", node_label, {"name": "alpha"}, vt_s=0, vt_e=scale)
    wall = time.time() - t0

    stats = store.stats()
    digest = _identity(store, digest_mode)
    store_bytes = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    store.close()

    return BuildResult(
        total_events=scale, n_entities=stats.get("n_entities", 0),
        n_edge_versions=stats.get("n_edge_versions", 0), wall_s=round(wall, 3),
        digest=digest, store_bytes=store_bytes)


def fidelity(result: BuildResult, expected: dict[str, int]) -> tuple[bool, list[str]]:
    """Exact-match gate against `expected_counts(scale)` — the same "exact
    or nothing" discipline `build_sx_store.py`/`build_wiki_talk_store.py`
    use, with the expected values derived from the generator itself rather
    than an external published stat (this store has none)."""
    lines: list[str] = []
    ok = True
    ok &= result.n_edge_versions == expected["events"]
    lines.append(f"n_edge_versions   want {expected['events']:>10,}  got "
                 f"{result.n_edge_versions:>10,}  "
                 f"{'ok' if result.n_edge_versions == expected['events'] else 'MISMATCH'}")
    ok &= result.n_entities == expected["nodes"]
    lines.append(f"n_entities        want {expected['nodes']:>10,}  got "
                 f"{result.n_entities:>10,}  "
                 f"{'ok' if result.n_entities == expected['nodes'] else 'MISMATCH'}")
    return ok, lines


# ---------------------------------------------------------------------------
# the fitness probe — a functional self-check, not a count check
# ---------------------------------------------------------------------------

def _select_probe_index(scale: int, *, min_frac: float = 0.20) -> int | None:
    """The frozen, deterministic probe-edge selection rule (memo left this
    open — item 4 in the module docstring's list): the **first** event
    index (in generation order) whose drawn interval spans `>= min_frac`
    of the extent. `20%` was chosen so a 1%-of-extent window sits
    comfortably inside the interval's start with ample room left over
    *after* the window for a correction to land outside it and still be
    well short of the interval's own end (`FRESHNESS_SEMANTICS.md`§9.7's
    worked example uses a 5x margin, `[0,100)` vs `[0,20)`; 20% against a
    1% window is a 20x margin, deliberately generous rather than marginal).
    Returns `None` if no such index exists in `range(scale)` — see
    `fitness_probe`'s docstring for what happens then (STOP, never widen
    the search silently)."""
    for i in range(scale):
        if life_frac(i) >= min_frac:
            return i
    return None


def fitness_probe(out: Path, backend: str, scale: int) -> dict[str, Any]:
    """Post-build functional self-check: does an outside-window B/C/D
    correction on THIS store actually change an `aggregate_events`
    duration answer? This is the store's fitness-for-purpose test, not a
    count check — R-8's whole population requirement (memo §2, DECISION 2)
    is "believed intervals with finite `vt_e` ... with mass in the
    `(vt_s in window, vt_e >> t_b)` region", and the only way to know the
    generator actually produced that shape is to run the exact mechanism
    the carve arm depends on, once, deterministically.

    **The probe, concretely** (mirrors `FRESHNESS_SEMANTICS.md`§9.7's own
    worked example, `[0,100)`/window `[0,20)`/correct `[50,60)` ->
    answer 100 -> 50): pick event index `i0 = _select_probe_index(scale)`
    (its own docstring has the selection rule); that event's original
    interval is `[vt_s0, vt_s0 + L)` with `L >= 0.20 * scale`. Window
    `w = [0, round(0.01*scale))` (a 1%-of-extent window, R-6's own middle
    band, starting at the store's own valid-time origin so `vt_s0` — which
    can be at or near 0 depending on `_vt_s`'s burst band — is virtually
    certain to fall inside it). Measure `aggregate_events` `max(duration)`
    grouped by source endpoint over `w`, on the pristine store. Inject ONE
    `correct` on event `i0`'s own edge ref, `vt_s = w[1] + margin` (safely
    OUTSIDE the window), `vt_e =` the original `vt_e` — per §9.7's shape,
    this leaves `[vt_s0, w[1]+margin)` as the surviving prior-belief
    fragment, whose OWN duration (`w[1]+margin - vt_s0`) is shorter than
    the original `L` and still overlaps the window, so the aggregate's
    answer must change if the mechanism works. Re-measure; compare
    `result_digest`.

    **If it does not change: STOP, do not retry.** No other index, window
    or margin is tried at runtime — picking a *different* deterministic
    rule after seeing this one fail would be exactly the "post-hoc
    revision, tuning silently" the task instruction forbids. A failure
    here means the memo's frozen distribution (or this script's own open
    choices 1-2) does not deliver DECISION 2's population requirement on
    this store, and that is reported as a blocking fact, not patched.
    """
    import tgms
    from tgms.temporal.algebra import call_operator, ensure_all_registered
    ensure_all_registered()

    i0 = _select_probe_index(scale)
    if i0 is None:
        return {"ok": False, "reason": "NO_QUALIFYING_PROBE_EDGE",
                "detail": f"no event index in range({scale}) drew an interval "
                          f">= 20% of the extent under this store's frozen "
                          f"length distribution; the fitness probe cannot even "
                          f"be attempted"}

    e0 = _event_iv(i0, scale)
    window_hi = max(2, round(0.01 * scale))
    margin = max(1, window_hi // 10)
    correction_vt_s = window_hi + margin
    if correction_vt_s >= e0["vt_e"]:
        return {"ok": False, "reason": "PROBE_EDGE_TOO_SHORT",
                "detail": f"event {i0}'s interval [{e0['vt_s']}, {e0['vt_e']}) does "
                          f"not reach past the correction point {correction_vt_s} "
                          f"(window_hi={window_hi}, margin={margin})"}

    args = {"group_by": [{"dim": "endpoint", "role": "src"}],
           "aggregates": [{"agg": "max", "of": "duration"}],
           "window": {"t_a": 0, "t_b": window_hi}}

    store = tgms.open(out, backend=backend)
    try:
        before = call_operator(store.adapter, "aggregate_events", dict(args),
                               skip_cost_check=True, tt_source=store)
        store.correct(_edge_ref(i0, scale), {"probe": "carve-fitness"},
                     vt_s=correction_vt_s, vt_e=e0["vt_e"])
        after = call_operator(store.adapter, "aggregate_events", dict(args),
                              skip_cost_check=True, tt_source=store)
    finally:
        store.close()

    changed = before["result_digest"] != after["result_digest"]
    return {
        "ok": changed,
        "reason": "OK" if changed else "DUPLICATE_ANSWER_AFTER_CORRECTION",
        "probe_event_index": i0,
        "probe_edge": {"src": e0["src"], "dst": e0["dst"], "rel_type": e0["rel_type"]},
        "original_interval": [e0["vt_s"], e0["vt_e"]],
        "window": [0, window_hi],
        "correction_interval": [correction_vt_s, e0["vt_e"]],
        "before_result_digest": before["result_digest"],
        "after_result_digest": after["result_digest"],
        "before_payload": {k: v for k, v in before.items()
                           if k in ("rows", "rows_total")},
        "after_payload": {k: v for k, v in after.items()
                          if k in ("rows", "rows_total")},
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--scale", type=int, default=CANONICAL_SCALE,
                    help=f"event count; the canonical, scored build is exactly "
                        f"{CANONICAL_SCALE} (memo §5's own choice, matching "
                        f"collegemsg's measured edge-version count). Any other "
                        f"value is a smoke/dev build and its card is stamped "
                        f"canonical=false.")
    ap.add_argument("--backend", default="native", choices=("native", "duckdb"))
    ap.add_argument("--node-label", default="Node")
    ap.add_argument("--digest", default="manifest", choices=("none", "manifest", "full"),
                    dest="digest_mode")
    ap.add_argument("--skip-fitness-probe", action="store_true",
                    help="for development only -- the default build always runs "
                        "the fitness probe and blocks on its failure")
    args = ap.parse_args()

    out = Path(args.out)
    sha = _sha()
    canonical_scale = args.scale == CANONICAL_SCALE
    print(f"RUN_STARTED commit={sha} dataset=synth-iv-{args.scale} out={out} "
          f"backend={args.backend} scale={args.scale} "
          f"canonical_scale={canonical_scale} digest={args.digest_mode} "
          f"host={platform.node()}", flush=True)

    expected = expected_counts(args.scale)
    result = build(out, args.backend, args.scale, args.node_label, args.digest_mode)
    ok, lines = fidelity(result, expected)

    print("\n=== exact-count self-verification gate ===")
    for line in lines:
        print(line)
    print(f"GATE: {'PASS' if ok else 'FAIL'}")

    probe: dict[str, Any] | None = None
    probe_ok = True
    if not args.skip_fitness_probe:
        probe = fitness_probe(out, args.backend, args.scale)
        probe_ok = bool(probe["ok"])
        print("\n=== fitness probe (outside-window B/C/D changes a duration answer) ===")
        print(json.dumps(probe, indent=1, default=str))
        print(f"FITNESS PROBE: {'PASS' if probe_ok else 'FAIL'}")
        if not probe_ok:
            print("\nSTOPPING, not retrying: the frozen length distribution (or "
                 "this script's own documented open choices) did not produce a "
                 "believed interval this store's data can carve across a 1% "
                 "window. This is reported as a blocking finding, per the task "
                 "instruction, not silently tuned around.")

    overall_ok = ok and probe_ok
    canonical = canonical_scale and overall_ok

    card = {
        "dataset": f"synth-iv-{args.scale}" if not canonical_scale else "synth-iv-60k",
        "note": ("Deterministic interval-valid-time synth store, proposed by "
                 "docs/design/M5_CARVE_POPULATION_PROPOSAL_2026-08-28.md §5/§9 "
                 "(DECISION 5) -- unratified at the time this builder was "
                 "written; see that memo for the population question this "
                 "store exists to answer. NOT an edit to "
                 "scripts/eval_harness.py::build_dataset; a new builder, a new "
                 "label. Epoch-1 only (no baked corrections)."),
        "commit": sha,
        "host": platform.node(),
        "platform": platform.platform(),
        "backend": args.backend,
        "scale": args.scale,
        "canonical_scale": canonical_scale,
        "interval_length_distribution": {
            "shape": "log-uniform", "min_frac_of_extent": MIN_LIFE_FRAC,
            "max_frac_of_extent": MAX_LIFE_FRAC, "seed_stream": "_mix(3*i)",
        },
        "epoch": "1 (no baked corrections/retractions)",
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "wall_s": result.wall_s,
        "events": result.total_events,
        "n_entities": result.n_entities,
        "n_edge_versions": result.n_edge_versions,
        "expected": expected,
        "store_identity": result.digest,
        "store_bytes": result.store_bytes,
        "count_gate": "PASS" if ok else "FAIL",
        "count_gate_table": lines,
        "fitness_probe": probe,
        "fitness_probe_gate": ("SKIPPED" if args.skip_fitness_probe
                               else ("PASS" if probe_ok else "FAIL")),
        # `canonical`: the scale-60,000 build claim, gated the same "both the
        # right scale AND the gates actually passed" way
        # build_wiki_talk_store.py's `canonical = no_override and ok` is —
        # never decided by "was --scale left at default" alone.
        "canonical": canonical,
        "overall_gate": "PASS" if overall_ok else "FAIL",
    }
    (out / "dataset_card.json").write_text(json.dumps(card, indent=1, sort_keys=True, default=str))
    print(f"\ncard: {out / 'dataset_card.json'}")
    print(f"identity: {result.digest}  wall: {result.wall_s}s  "
         f"bytes: {result.store_bytes:,}  canonical: {canonical}")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
