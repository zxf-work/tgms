"""M5 — the campaign harness against the ratified freeze (P2.3).

NORMATIVE: `docs/design/M5_CAMPAIGN_FREEZE_2026-08-27.md` (FROZEN). This file
implements §A's population, §B-E's measurement axes and §G's scoring
discipline; it decides nothing §A-§J already decided. Every frozen number
lives in `benchmarks/m5-v1/campaign.yaml`, **read, never hard-coded** — see
`load_campaign()` below for the digest guard that keeps the two documents
from drifting apart silently.

**Precedent this is built on** (`scripts/bench_freshness.py`, M4's harness):
the isolation-per-trial copy, the five-field replay tuple, the FLOOR
discipline (report "not adequately measured", never a lowered floor), and
the per-file machine+sha+store+date receipt. Six arms this file adds beyond
that precedent, one function each, named after the freeze section that
specifies them:

| function | freeze § | what it measures |
|---|---|---|
| `carve_arm_sweep` | A3 / C5 | the carve-arm decision rule, by-arm accounted |
| `pattern_l1_sweep` | A4 / C2 | PatternMatch Level-1: T_node win + mixed rel_types win |
| `zero_changed_ops_sweep` | A5 | the four historically-zero-changed-trial operators |
| `propagation_sweep` | B2 / E / Gate C | the two-hop chain, false-safe decisions, determinism |
| `pinned_sweep` | A7 / C4 | pinned scopes, the twice-run exemption difference |
| `control_sweep` | C1 | the re-measured all-"*" control, folded into every arm |

**Native backend only (R-1).** `main()` refuses outright on any other
backend before opening a store — DuckDB is a confound, not a population
member (§A1), and this harness does not measure a confound by accident.

**Where the freeze is silent, this file records raw rows and decides
nothing** (§G's own words: "record, don't decide"). Every such spot is
named in its function's docstring with the word SILENT so a reader (and
the eventual `gen_m5_report.py`) can find every place this harness made a
call the freeze did not make for it.

    uv run python scripts/bench_m5.py --smoke
    uv run python scripts/bench_m5.py --profile full --out benchmarks/m5-v1

`TGMS_MEASURED_DATE=YYYY-MM-DD` pins the receipt date (tgir_measure.py's
own pattern) so a re-run stays comparable across a day boundary.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import platform
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

import tgms  # noqa: E402
from tgms.core.errors import TgmsError  # noqa: E402
from tgms.core.model import OPEN_END, canonical_json  # noqa: E402
from tgms.eval.corrections import (  # noqa: E402
    PLACEMENTS, Correction, Substrate, Target, generate, probe_substrate,
)
from tgms.temporal.algebra import call_operator, ensure_all_registered  # noqa: E402
from tgms.tgir.check import ChainCache, Verdict, check  # noqa: E402
from tgms.tgir.depscope import TOP_TERM, DependencyScope  # noqa: E402

ensure_all_registered()

CAMPAIGN_YAML = ROOT / "benchmarks/m5-v1/campaign.yaml"
FREEZE_DOC = ROOT / "docs/design/M5_CAMPAIGN_FREEZE_2026-08-27.md"

_DATE_RE = __import__("re").compile(r"^\d{4}-\d{2}-\d{2}$")


def measured_date() -> str:
    """`TGMS_MEASURED_DATE`, mirroring `scripts/tgir_measure.py::_measured_date` —
    a re-run of a frozen record must stay reproducible across a day boundary,
    which `date.today()` alone cannot give it."""
    raw = os.environ.get("TGMS_MEASURED_DATE")
    if raw is None:
        return date.today().isoformat()
    if not _DATE_RE.match(raw):
        raise ValueError(f"TGMS_MEASURED_DATE must be YYYY-MM-DD, got {raw!r}")
    date.fromisoformat(raw)
    return raw


# ---------------------------------------------------------------------------
# campaign.yaml — the single source of the frozen numbers, and its digest guard
# ---------------------------------------------------------------------------

#: The two-way binding file (freeze doc §Addendum 3). Not derived from
#: whichever `campaign.yaml` a caller points `--campaign-yaml` at — this is
#: the repo's own canonical anchor, so pointing the harness at a tampered
#: *copy* of the yaml elsewhere still gets checked against the one true
#: pinned digest, not against whatever the copy happens to say about itself.
FREEZE_BINDING = ROOT / "benchmarks/m5-v1/FREEZE_BINDING"


class CampaignDigestError(SystemExit):
    """Raised (as a `SystemExit`) when either half of the two-way digest
    binding disagrees with a fresh hash — the freeze document against
    `campaign.yaml`'s own `freeze_sha256` field, or `campaign.yaml` against
    `FREEZE_BINDING`'s `campaign_yaml_sha256` line. The harness refuses to
    run rather than silently measuring against either document having moved
    out from under the other — G1's "no scoring choice may move after any
    measurement" applies to the harness's own inputs, not only to what it
    writes."""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _parse_binding_line(text: str, key: str, *, where: Path) -> str:
    """A labelled `key: value` line in `FREEZE_BINDING` — deliberately not
    full YAML (the file is meant to be readable and diffable as five plain
    lines), so this is a small dedicated parser rather than `yaml.safe_load`.
    Refuses loudly if the line is missing, which is what "REFUSING TO RUN"
    on a corrupted binding file should look like."""
    prefix = f"{key}:"
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    raise CampaignDigestError(f"{where}: no {key!r} line found — the binding file is malformed")


def load_campaign(path: Path = CAMPAIGN_YAML) -> dict[str, Any]:
    """Load `campaign.yaml` and refuse unless **both** halves of the
    freeze-vs-companion digest binding hold:

    **(a) the freeze document's current hash matches `campaign.yaml`'s own
    `freeze_sha256` field** — the original check, catching the FREEZE
    DOCUMENT drifting or being tampered.

    **(b) `campaign.yaml`'s current hash matches `FREEZE_BINDING`'s
    `campaign_yaml_sha256` line** — added 2026-08-27 after coordinator
    re-verification found (a) alone was a one-directional guard: nothing
    pinned this file's OWN bytes, so an edit to a frozen parameter inside
    it (the reported case: R-13's bar, `sed`'d from `10.0` to `9.9` in a
    copied file) passed silently, with no store ever opened to catch it
    downstream. `FREEZE_BINDING` is checked against the REPO's own
    canonical copy (`FREEZE_BINDING` module constant above), not derived
    from whatever `path` points at — so a tampered copy of the yaml passed
    via `--campaign-yaml` is caught even though its own `freeze_sha256`
    field was left untouched.

    A direct third check — the freeze document embedding `campaign.yaml`'s
    hash directly, closing the loop symmetrically — is not implemented and
    is not implementable: see `docs/design/M5_CAMPAIGN_FREEZE_2026-08-27.md`
    Addendum 3 for why (a literal two-way embedding is a SHA-256
    self-referential fixpoint, i.e. as hard as a preimage search).
    `FREEZE_BINDING` is the documented, one-pass alternative Addendum 3
    authorizes: written last, depending on both documents, with neither
    document depending on it.
    """
    if not path.exists():
        raise CampaignDigestError(f"no campaign.yaml at {path}")
    cfg = yaml.safe_load(path.read_text())

    # (a) — the freeze document against campaign.yaml's own recorded value.
    freeze_rel = cfg.get("freeze_document")
    if not freeze_rel:
        raise CampaignDigestError(f"{path}: no freeze_document key")
    freeze_path = ROOT / freeze_rel
    if not freeze_path.exists():
        raise CampaignDigestError(f"{path}: freeze_document {freeze_rel} does not exist")
    actual_freeze = _sha256(freeze_path)
    recorded_freeze = cfg.get("freeze_sha256")
    if actual_freeze != recorded_freeze:
        raise CampaignDigestError(
            f"REFUSING TO RUN (check a): {path} records freeze_sha256="
            f"{recorded_freeze!r} for {freeze_rel}, but that document now "
            f"hashes to {actual_freeze!r}. The freeze and its "
            f"machine-readable companion have drifted apart. Per G1, a "
            f"changed freeze value needs a dated §I addendum AND a "
            f"matching campaign.yaml update together, never the harness "
            f"running ahead on a stale pin.")

    # (b) — campaign.yaml's OWN bytes against the canonical FREEZE_BINDING.
    if not FREEZE_BINDING.exists():
        raise CampaignDigestError(
            f"REFUSING TO RUN: no {FREEZE_BINDING} — the two-way digest "
            f"binding (freeze doc Addendum 3) has not been established; "
            f"campaign.yaml's own contents are unpinned.")
    binding_text = FREEZE_BINDING.read_text()
    recorded_yaml = _parse_binding_line(binding_text, "campaign_yaml_sha256", where=FREEZE_BINDING)
    actual_yaml = _sha256(path)
    if actual_yaml != recorded_yaml:
        raise CampaignDigestError(
            f"REFUSING TO RUN (check b): {FREEZE_BINDING} records "
            f"campaign_yaml_sha256={recorded_yaml!r}, but {path} now hashes "
            f"to {actual_yaml!r}. campaign.yaml itself has drifted from its "
            f"pinned bytes — a frozen parameter (a gate threshold, a "
            f"window fraction, a floor) may have changed without a "
            f"matching, dated freeze addendum. Never run ahead on a stale "
            f"pin; re-derive FREEZE_BINDING only as part of a ratified "
            f"addendum.")
    # Consistency check, not a separate trust boundary: FREEZE_BINDING's own
    # freeze_document_sha256 line should agree with check (a)'s value too,
    # since both are hashes of the same file. A disagreement here means
    # FREEZE_BINDING itself was written against a different freeze-document
    # state than campaign.yaml currently pins — caught by name rather than
    # silently trusting whichever of the two the run happened to check first.
    recorded_freeze_in_binding = _parse_binding_line(
        binding_text, "freeze_document_sha256", where=FREEZE_BINDING)
    if recorded_freeze_in_binding != recorded_freeze:
        raise CampaignDigestError(
            f"REFUSING TO RUN: {FREEZE_BINDING} records freeze_document_sha256="
            f"{recorded_freeze_in_binding!r}, but {path} records freeze_sha256="
            f"{recorded_freeze!r} for the same document. The binding and the "
            f"companion disagree about which freeze-document state they were "
            f"each pinned against.")
    return cfg


# ---------------------------------------------------------------------------
# receipt / host discipline (A10, [INHERITED])
# ---------------------------------------------------------------------------

def _git_sha() -> str:
    # Slurm compute nodes may lack git (or refuse /project ownership); the
    # submitter stamps TGMS_COMMIT from the login node so records never
    # carry "unknown".
    env = os.environ.get("TGMS_COMMIT")
    if env:
        return env
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                              capture_output=True, text=True,
                              check=True).stdout.strip()
    except Exception:  # pragma: no cover
        return "unknown"


def receipt(*, profile: str, store_label: str | None, backend: str,
           store_identity: str | None = None,
           extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The four-key record shape (`eval_harness.py`'s pattern, restated by
    the freeze's §A10): **machine + sha + store identity + date** per file.
    `store_identity` is `Store.digest()` — the backend-independent,
    replay-equivalence digest (`build_snb_store.py::_identity`'s `"full"`
    mode) — the caller's responsibility to have computed **once**, off the
    pristine store, and pass through; this function does not open a store
    itself (`sx-mathoverflow` alone is 70+ MB, and this is called once per
    arm per store — five redundant opens per store for one digest each
    would be pure waste)."""
    return {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "measured_date": measured_date(),
        "git_sha": _git_sha(),
        "campaign_yaml_sha256": _sha256(CAMPAIGN_YAML),
        "freeze_document_sha256": _sha256(FREEZE_DOC),
        "profile": profile,
        "backend": backend,
        "store": store_label,
        "store_identity": store_identity,
        "machine": {"host": socket.gethostname(), "platform": platform.platform(),
                    "python": platform.python_version(), "cpus": os.cpu_count()},
        **(extra or {}),
    }


def log_line(msg: str) -> None:
    """A poll-friendly progress line: timestamped, flushed immediately, safe
    under `nohup > log 2>&1 &`."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def write_pidfile(path: Path) -> None:
    path.write_text(str(os.getpid()))
    log_line(f"pidfile {path} pid={os.getpid()}")


# ---------------------------------------------------------------------------
# native-only refusal (R-1, §A1)
# ---------------------------------------------------------------------------

def refuse_unless_native(backend: str) -> None:
    if backend != "native":
        raise SystemExit(
            f"REFUSING TO RUN: backend={backend!r}. R-1/§A1: native only, "
            f"scored and characterization alike — DuckDB has no replay "
            f"cursor (tests/test_artifact_refresh.py:51-67), which both "
            f"widens the invalidation denominator and zeroes D-153's "
            f"pinned-exemption line by construction. Not a preference; a "
            f"population exclusion.")


# ---------------------------------------------------------------------------
# shared helpers (mirrors bench_freshness.py, unchanged in substance)
# ---------------------------------------------------------------------------

OUTCOME_OK = "OK"
OUTCOME_REFUSED = "REFUSED_ON_RECOMPUTE"
OUTCOME_ERRORED = "ERRORED"
OUTCOME_NOT_INJECTED = "NOT_INJECTED"
OUTCOME_TIMEOUT = "TIMEOUT"

#: §A10 [INHERITED, `PAPER_A_EVIDENCE_FREEZE.md` §C5]: "Hard per-cell wall
#: ceiling: 600 s ... recorded as TIMEOUT. No cell runs unbounded; no
#: timeout is reported as a completion." Overridable only for local,
#: non-scorable smoke runs, via `TGMS_M5_CELL_TIMEOUT_S` — never for a
#: scored run, which always uses the frozen 600s from `campaign.yaml`.
_DEFAULT_CELL_TIMEOUT_S = 600


class _CellTimeout(Exception):
    pass


def _alarm_handler(signum: int, frame: Any) -> None:  # noqa: ARG001
    raise _CellTimeout()


def _with_timeout(fn: Callable[[], Any], *, seconds: int) -> tuple[Any, bool]:
    """Run `fn()` under a hard wall-clock ceiling. Returns `(result, timed_out)`
    — `result` is `None` on timeout. SIGALRM-based (POSIX only, which is
    every host this campaign runs on — xzgpu and every dev machine); a
    caller on a platform without `SIGALRM` gets no ceiling rather than a
    crash, which is the same "widen rather than refuse" posture the rest
    of this file follows for anything that is not the property under test.
    Never nested — this harness never calls `_with_timeout` from inside
    another `_with_timeout`, which would corrupt the outer alarm."""
    if not hasattr(signal, "SIGALRM"):  # pragma: no cover - non-POSIX
        return fn(), False
    old_handler = signal.signal(signal.SIGALRM, _alarm_handler)
    signal.alarm(seconds)
    try:
        return fn(), False
    except _CellTimeout:
        return None, True
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)


def _payload_of(env: dict[str, Any]) -> dict[str, Any]:
    from tgms.temporal.algebra import ENVELOPE_META_FIELDS
    return {k: v for k, v in env.items()
            if k not in ENVELOPE_META_FIELDS and k != "result_digest"}


def _value_of(env: dict[str, Any]) -> str:
    """§G7 / D1.8: the value-identity-stripped payload — see
    `bench_freshness.py::_value_of` for the full rationale, restated
    verbatim here rather than imported (this file must stand alone against
    an M4 harness that may itself move)."""
    def strip(x: Any) -> Any:
        if isinstance(x, dict):
            return {k: strip(v) for k, v in sorted(x.items())
                    if k not in ("vid", "tt_s", "tt_e", "provenance_ref")}
        if isinstance(x, list):
            return [strip(v) for v in x]
        return x
    return canonical_json(strip(_payload_of(env)))


#: Set once by `main()` from `campaign.yaml`'s `wall_ceiling_s_per_cell`
#: (§A10). Module-level rather than threaded through every call because
#: the ceiling is a single frozen constant for the whole run, not a
#: per-cell choice — the same reasoning `FLOOR` in `bench_freshness.py`
#: lives at module scope for.
CELL_TIMEOUT_S = _DEFAULT_CELL_TIMEOUT_S


def _execute(store: Any, op: str, args: dict[str, Any], *,
            bypass: bool = True) -> tuple[dict[str, Any] | None, str, str | None]:
    def call() -> dict[str, Any]:
        return call_operator(store.adapter, op, dict(args),
                             skip_cost_check=bypass, tt_source=store)
    try:
        env, timed_out = _with_timeout(call, seconds=CELL_TIMEOUT_S)
        if timed_out:
            return None, OUTCOME_TIMEOUT, f"exceeded the {CELL_TIMEOUT_S}s per-cell wall ceiling"
        return env, OUTCOME_OK, None
    except TgmsError as e:
        code = getattr(e, "code", type(e).__name__)
        outcome = (OUTCOME_REFUSED if "COST" in str(code).upper()
                  or "BUDGET" in str(code).upper() else OUTCOME_ERRORED)
        return None, outcome, f"{code}: {e}"


def _top_scope(scope: DependencyScope) -> DependencyScope:
    """C1 — the re-measured all-`"*"` control (§C1, M4 §6 Control 2)."""
    return scope.with_terms([TOP_TERM])


def _row_touch_verdict(env: dict[str, Any], correction: Correction) -> str:
    """C1 — Control 1, the naive row-touch rule (D6.4, required, [INHERITED])."""
    touched = set(correction.identities)
    if not touched:
        return "fresh"
    blob = canonical_json(_payload_of(env))
    return "possibly-stale" if any(f'"{u}"' in blob for u in touched) else "fresh"


def _isolated_copy(pristine: Path) -> Path:
    work = Path(tempfile.mkdtemp()) / "store"
    shutil.copytree(pristine, work)
    return work


def _last_batch_id(store: Any) -> str:
    last = ""
    for batch in store.eventlog.batches():
        last = batch["batch_id"]
    return last


def _witness_arms(verdict: Verdict) -> tuple[str, ...]:
    return tuple(sorted({w.arm for w in verdict.witnesses}))


def _control_invariant_ok(real: Verdict, control: Verdict) -> bool:
    """§C1's invariant: `"*"` matches everything a narrower term matches and
    more, so the control can never be FRESH where the real derivation is
    POSSIBLY_STALE. A violation is an instrument defect and blocks the
    campaign (checked by the caller, not swallowed here)."""
    if real.state == "possibly-stale" and control.state == "fresh":
        return False
    return True


# ===========================================================================
# Arm 1 — §A3 / §C5: the carve-arm cell population
# ===========================================================================

CARVE_ELIG_FORMS = ("aggregate_events_plain", "aggregate_events_duration",
                    "neighborhood_evolution")

#: R-8's RG-1 pair — identical call, one carve-reachable via `of: "duration"`.
RG1_PAIR = ("aggregate_events_plain", "aggregate_events_duration")


@dataclass
class CarveTrial:
    """One `(Q, A)` cell, one injected correction (§A3, §C5). Replayable from
    `store_digest_before` / `scope_digest` / `injected_batch_id` / `verdict`
    / `changed` alone, per §A8's inherited five fields."""

    store: str
    form: str
    cell: str
    window_fraction: float
    window_width: int
    op: str
    args_form: dict[str, Any]
    correction_class: str
    correction_generator: str
    placement: str
    outside_window: bool
    store_digest_before: str = ""
    scope_digest: str = ""
    injected_batch_id: str = ""
    verdict: str = ""
    changed: bool = False
    value_changed: bool = False
    outcome: str = OUTCOME_OK
    note: str = ""
    arms: tuple[str, ...] = ()
    matched_on: tuple[str, ...] = ()
    top_verdict: str = ""            # C1 control, same trial
    rowtouch_verdict: str = ""       # C1 control 1, same trial
    control_invariant_ok: bool = True

    def to_json(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        for k in ("arms", "matched_on"):
            d[k] = list(d[k])
        return d


def _carve_windows(vt_lo: int, vt_hi: int, fractions: Sequence[float],
                   share_le_1pct: float, n: int,
                   rng: random.Random) -> list[tuple[float, int, int]]:
    """R-6: `n` `(fraction, t_a, t_b)` triples, windows placed at varying
    offsets across the store's own `vt` extent, with at least
    `share_le_1pct` of them at a fraction `<= 0.01`.

    Composition, fixed by construction (never a measured outcome the harness
    could fall short on by chance): a 2:2:1 cycle over
    `(0.1%, 1%, 5%)` gives 80% of cells a fraction `<= 1%`, comfortably
    above R-6's 40% floor for any `fractions` triple ordered smallest-first.
    """
    extent = max(1, vt_hi - vt_lo)
    cycle = [fractions[0], fractions[0], fractions[1], fractions[1], fractions[2]]
    out: list[tuple[float, int, int]] = []
    for i in range(n):
        frac = cycle[i % len(cycle)]
        width = max(2, int(extent * frac))
        start = vt_lo + rng.randrange(max(1, extent - width))
        out.append((frac, start, min(vt_hi, start + width)))
    le = sum(1 for f, _a, _b in out if f <= 0.01)
    assert le / len(out) >= share_le_1pct - 1e-9, "R-6 composition invariant"
    return out


def _carve_cells(sub: Substrate, rel_types: Sequence[str], form: str,
                 windows: list[tuple[float, int, int]],
                 rng: random.Random) -> list[dict[str, Any]]:
    """One `(Q, A)` cell per window (R-5: >= 60 per store x form). Two
    carve-eligible operators, three forms — RG-1's identical-call pair plus
    `neighborhood_evolution`."""
    out = []
    uids = list(sub.uids) or ["n0"]
    for frac, t_a, t_b in windows:
        uid = rng.choice(uids)
        if form in ("aggregate_events_plain", "aggregate_events_duration"):
            aggs = ([{"agg": "max", "of": "duration"}] if form == "aggregate_events_duration"
                    else [{"agg": "count"}])
            args = {"group_by": [{"dim": "endpoint", "role": "src"}],
                    "aggregates": aggs, "window": {"t_a": t_a, "t_b": t_b}}
            op = "aggregate_events"
        else:
            args = {"uid": uid, "t1": t_a, "t2": t_b,
                    "stride": max(1, (t_b - t_a) // 8)}
            op = "neighborhood_evolution"
        out.append({"op": op, "args": args, "window_fraction": frac,
                    "window_width": t_b - t_a, "window": (t_a, t_b), "uid": uid})
    return out


#: SILENT / flagged (see this file's own report, "where the freeze was
#: silent"): `tgms/eval/corrections.py`'s Class B/C/D generators
#: (`_b_overwrite`, `_correct`, `_retract`) build **node-ref ops only** —
#: `ref={"kind": "node", ...}` is hard-coded (`corrections.py:311,379,405`
#: etc.). The carve-eligible operators §A3 names (`aggregate_events` with
#: `group_by: endpoint`, `neighborhood_evolution`) scope on `targets.edges`
#: / `targets.incident` — an EDGE-shaped term — so a node-only correction
#: cannot intersect it AT ALL: `targets_match` routes on `fp.entity_kind`
#: before anything else, and a node footprint's `entity_kind` never reaches
#: the edge branch (verified empirically against `stores/ldbc-fixture`
#: while building this harness — every node-ref B/C/D trial against an
#: endpoint-scoped `aggregate_events` term reports `verdict=fresh` on
#: EVERY arm, value and carve alike, which is a MISS on `targets`, not a
#: sound absence). R-7/R-8 need EDGE-identity B/C/D corrections to reach
#: this population at all. `tgms/eval/corrections.py` itself is
#: `[INHERITED]` verbatim from M4 (§A8) and is not this task's file to
#: extend; the four generators below are this harness's OWN addition,
#: local to this file, mirroring `corrections.py`'s own class/placement
#: semantics exactly but against `store.adapter.all_edge_versions()`
#: instead of `believed_node_versions`. Recorded here rather than silently
#: assumed to already exist.
def _edge_interval(sub: Substrate, target: Target, placement: str,
                   rng: random.Random) -> tuple[int, int]:
    step = max(2, sub.span // 20)
    if target.window is None or placement.startswith("in-window") or placement == "new-identity":
        base = target.window[0] if target.window else sub.vt_lo
        top = target.window[1] if target.window else sub.vt_hi
        start = base + rng.randrange(max(1, (top - base) // 2)) if top > base else base
        return start, start + step
    start = target.window[1] + 1 + rng.randrange(step)
    return start, start + step


def _edge_version_for(edges: Sequence[Any], target: Target, placement: str,
                      vt_s: int, vt_e: int, rng: random.Random) -> Any | None:
    """Mirrors `corrections.py::_version_for`'s "outside window" branch, with
    one deliberate strengthening: prefer a version whose OWN `vt_s` falls
    inside the window (`window[0] <= v.vt_s < window[1]`) over one that
    merely lies somewhere after it. This is what makes an "outside window"
    Class B correction — `assert_edge` re-asserting the SAME identity with a
    new interval placed past `window[1]` — actually a true positive rather
    than a vacuous one: an overwrite SUPERSEDES the entire prior version, so
    a version that WAS witnessed inside the window has its in-window
    coverage retired even though the new op's own declared interval never
    touches the window. A version whose own occurrence lies entirely after
    the window was never part of the windowed read to begin with, and
    overwriting it changes nothing the query could have seen — a true
    negative by construction, not evidence about the carve arm. The M4
    generator this file otherwise reuses verbatim does not carry this
    preference (its own "reaching" filter, mirrored in the fallback below,
    only requires `v.vt_e > window_end`, which the RG-1 asymmetry can still
    exploit for `of:"duration"`'s far-endpoint dependence — kept as the
    fallback for exactly that case). SILENT: R-7/R-8 name the class/placement
    shares, not this selection preference — recorded here as this harness's
    own choice, made to raise the realized 'changed' yield toward R-8's
    floor rather than leave it to chance."""
    if not edges:
        return None
    if not (target.window is not None and placement.startswith("outside-window")):
        hits = [v for v in edges if v.vt_s < vt_e and vt_s < v.vt_e]
        return rng.choice(hits) if hits else rng.choice(list(edges))
    window_start, window_end = target.window
    witnessed = [v for v in edges if window_start <= v.vt_s < window_end]
    if witnessed:
        return rng.choice(witnessed)
    reaching = [v for v in edges if v.vt_e > window_end and v.vt_s < vt_e]
    return rng.choice(reaching) if reaching else None


def _edge_ref(v: Any) -> dict[str, Any]:
    return {"kind": "edge", "src": v.src, "dst": v.dst, "rel_type": v.rel_type, "disc": v.disc}


def _edge_b_overwrite(sub: Substrate, edges: Sequence[Any], target: Target,
                      placement: str, rng: random.Random) -> Correction | None:
    from tgms.storage.base import make_op
    vt_s, vt_e = _edge_interval(sub, target, placement, rng)
    if placement == "new-identity":
        src, dst = f"__einj{rng.randrange(10**9):09d}", f"__einj{rng.randrange(10**9):09d}"
        rel = rng.choice(sub.rel_types) if sub.rel_types else "R"
        return Correction("B", "edge_b_overwrite", placement,
            (make_op("assert_edge", src=src, dst=dst, rel_type=rel,
                     props={"injected": "eb"}, vt_s=vt_s, vt_e=vt_e,
                     source="inject", provenance_ref=None),),
            note="new-identity edge assert", identities=(src, dst))
    e = _edge_version_for(edges, target, placement, vt_s, vt_e, rng)
    if e is None:
        return None
    return Correction("B", "edge_b_overwrite", placement,
        (make_op("assert_edge", src=e.src, dst=e.dst, rel_type=e.rel_type, disc=e.disc,
                 props={"injected": "eb", "tier": "revised"}, vt_s=vt_s, vt_e=vt_e,
                 source="inject", provenance_ref=None),),
        note="overwriting edge assert; carves", identities=(e.src, e.dst))


def _edge_correct(sub: Substrate, edges: Sequence[Any], target: Target, placement: str,
                  rng: random.Random, *, whole: bool) -> Correction | None:
    from tgms.storage.base import make_op
    if placement == "new-identity" or not edges:
        return None
    want_s, want_e = _edge_interval(sub, target, placement, rng)
    v = _edge_version_for(edges, target, placement, want_s, want_e, rng)
    if v is None:
        return None
    if target.window is not None and placement.startswith("outside-window"):
        vt_s = max(want_s, v.vt_s + 1 if v.vt_s >= want_s else want_s)
        vt_e = max(vt_s + 1, want_e if whole else vt_s + max(2, (want_e - want_s) // 2))
        vt_e = min(vt_e, v.vt_e if v.vt_e < OPEN_END else vt_e)
        if vt_e <= vt_s:
            return None
    else:
        top = v.vt_e if v.vt_e < OPEN_END else v.vt_s + max(4, sub.span // 4)
        vt_s, vt_e = (v.vt_s, top) if whole else (v.vt_s, min(top, v.vt_s + max(2, sub.span // 8)))
        if vt_e <= vt_s:
            vt_e = vt_s + 1
    return Correction("C", "edge_c1_whole" if whole else "edge_c2_sub", placement,
        (make_op("correct", ref=_edge_ref(v), props={"injected": "ec", "revised": True},
                 vt_s=vt_s, vt_e=vt_e, source="inject", provenance_ref=None),),
        note="edge property correction; carves", identities=(v.src, v.dst))


def _edge_retract(sub: Substrate, edges: Sequence[Any], target: Target, placement: str,
                  rng: random.Random, *, truncate: bool) -> Correction | None:
    from tgms.storage.base import make_op
    if placement == "new-identity" or not edges:
        return None
    want_s, want_e = _edge_interval(sub, target, placement, rng)
    v = _edge_version_for(edges, target, placement, want_s, want_e, rng)
    if v is None:
        return None
    if target.window is not None and placement.startswith("outside-window"):
        if not truncate:
            return None
        t = max(want_s, v.vt_s + 1)
        if not (v.vt_s < t < v.vt_e):
            return None
    elif truncate:
        top = v.vt_e if v.vt_e < OPEN_END else v.vt_s + max(4, sub.span // 2)
        t = v.vt_s + max(1, (top - v.vt_s) // 2)
        if not (v.vt_s < t < top):
            return None
    else:
        t = v.vt_s
    return Correction("D", "edge_d1_truncate" if truncate else "edge_d2_full", placement,
        (make_op("retract", ref=_edge_ref(v), t=int(t), source="inject", provenance_ref=None),),
        note="edge retract; carves", identities=(v.src, v.dst))


_EDGE_BUILDERS: dict[str, Callable[..., Correction | None]] = {
    "edge_b_overwrite": _edge_b_overwrite,
    "edge_c1_whole": lambda sub, edges, target, placement, rng:
        _edge_correct(sub, edges, target, placement, rng, whole=True),
    "edge_c2_sub": lambda sub, edges, target, placement, rng:
        _edge_correct(sub, edges, target, placement, rng, whole=False),
    "edge_d1_truncate": lambda sub, edges, target, placement, rng:
        _edge_retract(sub, edges, target, placement, rng, truncate=True),
    "edge_d2_full": lambda sub, edges, target, placement, rng:
        _edge_retract(sub, edges, target, placement, rng, truncate=False),
}


def _sample_edges(store: Any, cap: int = 300) -> list[Any]:
    out = []
    for e in store.adapter.all_edge_versions():
        out.append(e)
        if len(out) >= cap:
            break
    return out


def _generate_edge_corrections(sub: Substrate, edges: Sequence[Any], target: Target,
                               rng: random.Random, *,
                               generators: Sequence[str] = tuple(_EDGE_BUILDERS),
                               placements: Sequence[str] = PLACEMENTS) -> list[Correction]:
    out: list[Correction] = []
    for gen in generators:
        for placement in placements:
            built = _EDGE_BUILDERS[gen](sub, edges, target, placement, rng)
            if built is not None:
                out.append(built)
    return out


def _weighted_corrections(store: Any, sub: Substrate, target: Target,
                          rng: random.Random, *, min_bcd_share: float,
                          min_outside_share_of_bcd: float,
                          edges: Sequence[Any] | None = None) -> list[Correction]:
    """R-7: >= 70% Class B/C/D, >= 60% of those outside every window.
    Composed by construction (three oversampled generator/placement
    buckets), never left to the balanced default matrix — the default 8x5
    cartesian in `corrections.generate()` does not clear either bar on its
    own, since Class A/E and in-window placements would otherwise dilute
    both shares below R-7's floor.

    `edges`, when given, blends in this file's own edge-ref B/C/D
    generators (see the block comment above `_edge_interval`) alongside
    `corrections.py`'s node-ref ones — needed for any carve-eligible cell
    whose scope is edge-shaped (`aggregate_events`, `neighborhood_evolution`).
    """
    bcd = ("b_overwrite", "c1_whole", "c2_sub", "d1_truncate", "d2_full")
    outside = ("outside-window-read", "outside-window-unread")
    inside_or_new = ("in-window-read", "in-window-unread", "new-identity")
    out: list[Correction] = []
    for _ in range(3):
        out += generate(store, sub, target, rng=rng, generators=bcd, placements=outside)
        if edges is not None:
            out += _generate_edge_corrections(sub, edges, target, rng, placements=outside)
    for _ in range(1):
        out += generate(store, sub, target, rng=rng, generators=bcd, placements=inside_or_new)
        if edges is not None:
            out += _generate_edge_corrections(sub, edges, target, rng, placements=inside_or_new)
    for _ in range(1):
        out += generate(store, sub, target, rng=rng,
                        generators=("a1_events", "a2_disjoint", "e_within_batch"))
    bcd_n = sum(1 for c in out if c.cls in ("B", "C", "D"))
    outside_of_bcd = sum(1 for c in out if c.cls in ("B", "C", "D")
                         and c.placement.startswith("outside-window"))
    return out, (bcd_n / len(out) if out else 0.0,
                (outside_of_bcd / bcd_n) if bcd_n else 0.0)


def _run_carve_trial(pristine: Path, cell: dict[str, Any], before_env: dict[str, Any],
                     scope: DependencyScope, correction: Correction, *,
                     store_label: str, form: str, backend: str) -> CarveTrial:
    t = CarveTrial(store=store_label, form=form,
                   cell=f"{cell['op']}/{cell['window_fraction']}/{cell['window']}",
                   window_fraction=cell["window_fraction"], window_width=cell["window_width"],
                   op=cell["op"], args_form=cell["args"],
                   correction_class=correction.cls, correction_generator=correction.generator,
                   placement=correction.placement,
                   outside_window=correction.placement.startswith("outside-window"))
    work = _isolated_copy(pristine)
    try:
        store = tgms.open(work, backend=backend)
    except Exception as e:  # pragma: no cover
        shutil.rmtree(work.parent, ignore_errors=True)
        t.outcome, t.note = OUTCOME_ERRORED, f"open failed: {e}"
        return t
    try:
        t.store_digest_before = store.digest()
        t.scope_digest = scope.digest()
        try:
            store._write(list(correction.ops))
        except TgmsError as e:
            t.outcome, t.note = OUTCOME_NOT_INJECTED, f"injection refused: {e}"
            return t
        t.injected_batch_id = _last_batch_id(store)
        after_env, outcome, err = _execute(store, cell["op"], cell["args"])
        t.outcome = outcome
        if after_env is None:
            t.note = err or ""
            return t
        t.changed = after_env["result_digest"] != before_env["result_digest"]
        t.value_changed = _value_of(after_env) != _value_of(before_env)
        verdict = check(scope, store.eventlog)
        t.verdict = verdict.state
        t.arms = _witness_arms(verdict)
        t.matched_on = tuple(sorted({c for w in verdict.witnesses for c in w.matched_on}))
        control = check(_top_scope(scope), store.eventlog)
        t.top_verdict = control.state
        t.control_invariant_ok = _control_invariant_ok(verdict, control)
        t.rowtouch_verdict = _row_touch_verdict(before_env, correction)
        return t
    finally:
        try:
            store.close()
        except Exception:  # pragma: no cover
            pass
        shutil.rmtree(work.parent, ignore_errors=True)


def carve_arm_sweep(store_label: str, path: Path, cfg: dict[str, Any], *,
                    backend: str, cells_per_form: int, corrections_cap: int,
                    rng: random.Random) -> list[CarveTrial]:
    """§A3 / §C5. Only the M4-continuity stores (`bitcoinotc`, `collegemsg`)
    carry R-5's population per the freeze's own table under R-5 ("over
    bitcoinotc + collegemsg"); a caller naming another store gets an empty
    list rather than a silent substitution."""
    store = tgms.open(path, backend=backend)
    sub = probe_substrate(store, rng=rng)
    rel_types = sub.rel_types
    windows_cfg = cfg["carve_arm"]["window_fractions"]
    share = cfg["carve_arm"]["min_fraction_share_le_1pct"]
    trials: list[CarveTrial] = []
    for form in cfg["carve_arm"]["forms"]:
        windows = _carve_windows(sub.vt_lo, sub.vt_hi, windows_cfg, share,
                                 cells_per_form, rng)
        cells = _carve_cells(sub, rel_types, form, windows, rng)
        for cell in cells:
            env, outcome, err = _execute(store, cell["op"], cell["args"])
            if env is None:
                log_line(f"  {store_label} {form} {cell['window']}: "
                        f"{outcome} at baseline ({err})")
                continue
            scope = DependencyScope.from_json(env["dependency"])
            target = Target(read_uids=(cell.get("uid"),) if cell.get("uid") else (),
                            window=cell["window"])
            probe = tgms.open(path, backend=backend)
            edge_pool = _sample_edges(probe)
            corrections, shares = _weighted_corrections(
                probe, sub, target, rng,
                min_bcd_share=cfg["carve_arm"]["injection"]["min_bcd_share"],
                min_outside_share_of_bcd=cfg["carve_arm"]["injection"][
                    "min_outside_window_share_of_bcd"],
                edges=edge_pool)
            probe.close()
            for correction in corrections[:corrections_cap]:
                trials.append(_run_carve_trial(
                    path, cell, env, scope, correction,
                    store_label=store_label, form=form, backend=backend))
        log_line(f"ok  carve {store_label} {form}: {len(cells)} cells, "
                f"{len([t for t in trials if t.form == form])} trials so far")
    store.close()
    return trials


# ===========================================================================
# Arm 2 — §A4 / §C2: the PatternMatch Level-1 population
# ===========================================================================

@dataclass
class PatternL1Trial:
    store: str
    cell: str
    mixed: bool
    anchored: bool
    node_digest: str
    correction_kind: str
    level0_verdict: str
    level1_verdict: str
    level0_witnesses: int
    level1_witnesses: int
    level1_terms_level0: int
    level1_terms_level1: int
    outcome: str = OUTCOME_OK
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _pattern_record(store: Any, root: Any, result: dict[str, Any], name: str) -> Any:
    """The same shape `tests/test_scan_region_pattern.py::_record` builds —
    an `ArtifactRecord` sourced from a real `run_plan` envelope, never
    hand-assembled, so the recorded `scan_region` is the real, paired
    artifact one execution produced."""
    from tgms.artifact.record import ArtifactRecord, StepDependency

    scope = DependencyScope.from_json(result["dependency"])
    region = result["tgir"].get("annotations", {}).get(root.node_digest, {}).get("scan_region")
    return ArtifactRecord(
        name=name, generation=0, kind="query_result", store=scope.store,
        plan={"plan_digest": "pd", "node_digest": root.node_digest, "plan_format": 1,
              "plan_ref": f"plans/{name}.json"},
        basis={"tt_q": scope.tt_q, "pinned": scope.pinned, "clamped": scope.clamped,
              "tt_q_verified": scope.tt_q_verified},
        state={"completeness": "complete", "exactness": "exact", "refusal": None},
        refresh={"kind": "tgir_plan", "ref": f"plans/{name}.json", "basis_policy": "open"},
        steps=[StepDependency("s1", scope, scan_region=region)],
    )


def _t_node_patterns(rel: str) -> list[tuple[str, Any]]:
    from tgms.tgir.node import EdgePat, NodePat, Pattern
    return [
        ("t_node-anchored", Pattern((NodePat("x"), NodePat("y")),
                                    (EdgePat("e1", "x", "y", rel),))),
        ("t_node-unanchored", Pattern((NodePat("x"), NodePat("y")),
                                      (EdgePat("e1", "x", "y"),))),
    ]


def _mixed_patterns(rel_types: Sequence[str]) -> list[tuple[str, Any]]:
    """The mixed declared/undeclared population (§A4's 2026-08-27
    correction) — needs >= 2 edge types in the store, which only
    `sx-mathoverflow` provides among the scored stores. One `EdgePat`
    declares a type, the other leaves `rel_type=None` (scans every type)."""
    from tgms.tgir.node import EdgePat, NodePat, Pattern
    out = []
    for declared in rel_types:
        out.append((f"mixed-{declared}", Pattern(
            (NodePat("x"), NodePat("y"), NodePat("u"), NodePat("v")),
            (EdgePat("e1", "x", "y", declared), EdgePat("e2", "u", "v", None)))))
    return out


def _all_declared_patterns(rel_types: Sequence[str]) -> list[tuple[str, Any]]:
    """The all-declared control named in §A4: structurally narrower, never
    counted toward the mixed win (per the 2026-08-27 correction and
    `tests/test_scan_region_pattern.py::
    test_ab_out_foreign_rel_type_is_fresh_at_both_levels_when_all_declared`).
    Recorded anyway — its own column, reported not gated."""
    from tgms.tgir.node import EdgePat, NodePat, Pattern
    if len(rel_types) < 2:
        return []
    a, b = rel_types[0], rel_types[1]
    return [("all-declared", Pattern(
        (NodePat("x"), NodePat("y"), NodePat("u"), NodePat("v")),
        (EdgePat("e1", "x", "y", a), EdgePat("e2", "u", "v", b))))]


def pattern_l1_sweep(store_label: str, path: Path, cfg: dict[str, Any], *,
                     backend: str, single_typed: bool, rel_types: Sequence[str],
                     n_corrections: int, rng: random.Random) -> list[PatternL1Trial]:
    """§A4 / §C2. `mixed=True` cells only fire on a multi-typed store
    (`mixed_requires_multi_typed_store` in campaign.yaml) — a single-typed
    store contributes T_node-win cells only, per the freeze's own ruling
    that the per-variable `rel_types` win is unconstructible there."""
    from tgms.tgir.execute import run_plan
    from tgms.tgir.node import PatternMatch
    from tgms.tgir.types import Sigma
    from tgms.artifact.witness import check_artifact

    store = tgms.open(path, backend=backend)
    sub = probe_substrate(store, rng=rng)
    rel = sub.rel_types[0] if sub.rel_types else "R"
    patterns: list[tuple[str, Any, bool]] = [
        (n, p, False) for n, p in _t_node_patterns(rel)]
    if not single_typed and len(rel_types) >= 2:
        patterns += [(n, p, True) for n, p in _mixed_patterns(rel_types)]
        patterns += [(n, p, False) for n, p in _all_declared_patterns(rel_types)]
    trials: list[PatternL1Trial] = []
    for name, pattern, mixed in patterns:
        root = PatternMatch(pattern, sigma_=Sigma.default())

        def _run() -> dict[str, Any]:
            return run_plan(root, store.adapter, tt_source=store,
                            cost_ceilings={"rows_scanned_est": 10 ** 9,
                                          "expansions_est": 10 ** 9,
                                          "time_est_ms": 10 ** 8})
        try:
            result, timed_out = _with_timeout(_run, seconds=CELL_TIMEOUT_S)
        except TgmsError as e:
            trials.append(PatternL1Trial(
                store=store_label, cell=name, mixed=mixed, anchored="anchored" in name,
                node_digest="", correction_kind="", level0_verdict="", level1_verdict="",
                level0_witnesses=0, level1_witnesses=0, level1_terms_level0=0,
                level1_terms_level1=0, outcome=OUTCOME_REFUSED, note=str(e)))
            continue
        if timed_out:
            # §A10's hard per-cell wall ceiling — a MIXED pattern's
            # undeclared-type edge scan is the one this harness observed
            # taking minutes on `sx-mathoverflow` (real scale) while
            # building this file; recorded by name, never silently waited
            # out, and never counted toward R-11's cell floor.
            trials.append(PatternL1Trial(
                store=store_label, cell=name, mixed=mixed, anchored="anchored" in name,
                node_digest=root.node_digest, correction_kind="", level0_verdict="",
                level1_verdict="", level0_witnesses=0, level1_witnesses=0,
                level1_terms_level0=0, level1_terms_level1=0, outcome=OUTCOME_TIMEOUT,
                note=f"run_plan exceeded the {CELL_TIMEOUT_S}s per-cell wall ceiling"))
            log_line(f"TIMEOUT pattern-l1 {store_label} {name}: "
                    f"run_plan exceeded {CELL_TIMEOUT_S}s")
            continue
        record = _pattern_record(store, root, result, name)
        for kind, corrector in _pattern_correction_probes(sub):
            work = _isolated_copy(path)
            wstore = tgms.open(work, backend=backend)
            try:
                ok = corrector(wstore)
                if not ok:
                    continue
                # One log walk, not two: level0/level1 read the identical
                # log state here, and `ChainCache` is keyed on
                # `(path, size, mtime)`, so sharing it across the pair is
                # sound (§3.9's own "off by default, on for a reason" —
                # this is that reason) and roughly halves this sweep's wall
                # time on a real-scale store's log.
                cache = ChainCache()
                v0 = check_artifact(record, wstore.eventlog, level1=False, chain_cache=cache)
                v1 = check_artifact(record, wstore.eventlog, level1=True, chain_cache=cache)
                trials.append(PatternL1Trial(
                    store=store_label, cell=name, mixed=mixed, anchored="anchored" in name,
                    node_digest=root.node_digest, correction_kind=kind,
                    level0_verdict=v0.steps.to_json()["verdict"],
                    level1_verdict=v1.steps.to_json()["verdict"],
                    level0_witnesses=len(v0.steps.witnesses), level1_witnesses=len(v1.steps.witnesses),
                    level1_terms_level0=len([t for t in v1.terms if t.level == "level-0"]),
                    level1_terms_level1=len([t for t in v1.terms if t.level == "level-1"])))
            finally:
                wstore.close()
                shutil.rmtree(work.parent, ignore_errors=True)
        log_line(f"ok  pattern-l1 {store_label} {name}: "
                f"{len([t for t in trials if t.cell == name])} trials")
    store.close()
    return trials


def _pattern_correction_probes(sub: Substrate) -> list[tuple[str, Callable[[Any], bool]]]:
    """The correction shapes §1.8's tests exercise: a node write on a
    plausibly-matched uid ('in-region'), and a node write on an uid unlikely
    to be an endpoint of any scanned edge ('out-of-region', §1.8 test 4 —
    "the test that measures the item's entire value"). SILENT: the freeze
    does not name a specific probe menu for the campaign population (only
    the unit-test suite has fixed scenarios); this harness's own choice,
    recorded here rather than assumed, is to reuse the two shapes the
    soundness suite already proved distinguish Level 0 from Level 1."""
    uids = list(sub.uids) or ["n0"]

    def in_region(store: Any) -> bool:
        uid = uids[0]
        store.assert_node(uid, sub.node_label, {"injected": "l1-in"}, sub.vt_lo, sub.vt_hi)
        return True

    def out_of_region(store: Any) -> bool:
        uid = f"__l1_probe_{random.randrange(10**9)}"
        store.assert_node(uid, sub.node_label, {"injected": "l1-out"}, sub.vt_lo, sub.vt_hi)
        return True

    return [("in-region-node-write", in_region), ("out-of-region-node-write", out_of_region)]


# ===========================================================================
# Arm 3 — §A5: the four zero-changed-trial operators
# ===========================================================================

ZERO_CHANGED_OPS = ("snapshot_subgraph", "temporal_paths", "co_active",
                    "find_temporal_motif_instances")


@dataclass
class ZeroChangedTrial:
    store: str
    op: str
    args_form: dict[str, Any]
    correction_class: str
    placement: str
    changed: bool
    verdict: str
    rows_returned_baseline: int
    outcome: str = OUTCOME_OK
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _row_returning_forms(store: Any, sub: Substrate, rel: str,
                         rng: random.Random, *, search_cap: int = 25) -> list[dict[str, Any]]:
    """R-10's argument forms: "an argument form that returns rows on these
    substrates". Frozen at the shape level here (before any injection, per
    §A5's own H5 discipline) — `seeds`/`hops` for `snapshot_subgraph`,
    `src`/`dst`/`k` for `temporal_paths`, a declared `rel_type` pair for
    `co_active`, a named motif for `find_temporal_motif_instances` — but the
    concrete identities are SEARCHED across up to `search_cap` candidate
    uid pairs per op, keeping the first that returns a non-empty baseline.
    A form that returns nothing after the whole search is reported by name
    (`rows_returned_baseline: 0`, noted), never silently swapped for a
    different shape — only the identity arguments within one fixed shape
    are chosen this way."""
    uids = list(sub.uids) or ["n0"]
    mid = sub.vt_lo + (sub.vt_hi - sub.vt_lo) // 2
    w = {"t_a": sub.vt_lo, "t_b": sub.vt_hi}
    out: list[dict[str, Any]] = []

    def _search(op: str, args_for: Callable[[str, str], dict[str, Any]]) -> dict[str, Any]:
        best = args_for(uids[0], uids[min(1, len(uids) - 1)])
        for _ in range(min(search_cap, len(uids) * len(uids))):
            a, b = rng.choice(uids), rng.choice(uids)
            args = args_for(a, b)
            env, outcome, _err = _execute(store, op, args)
            if env is not None and _rows_of(env) > 0:
                return args
        return best

    out.append({"op": "snapshot_subgraph", "t_valid": mid,
               "args": _search("snapshot_subgraph",
                               lambda a, b: {"seeds": [a, b], "t_valid": mid, "hops": 1})})
    out.append({"op": "temporal_paths",
               "args": _search("temporal_paths",
                               lambda a, b: {"src": a, "dst": b, "window": w, "k": 3})})
    out.append({"op": "co_active",
               "args": {"a_spec": {"rel_type": rel}, "b_spec": {"rel_type": rel},
                        "allen_relation": {"relation": "overlaps"}, "limit": 200}})
    out.append({"op": "find_temporal_motif_instances",
               "args": {"motif": "M_2node_pingpong", "window": w,
                        "delta": max(1, (sub.vt_hi - sub.vt_lo) // 8), "limit": 50}})
    return out


def _rows_of(env: dict[str, Any]) -> int:
    for key in ("rows", "instances", "paths", "pairs", "matches"):
        v = env.get(key)
        if isinstance(v, list):
            return len(v)
    return 0


def zero_changed_ops_sweep(store_label: str, path: Path, *, backend: str,
                           n_per_op: int, rng: random.Random) -> list[ZeroChangedTrial]:
    """§A5. `snapshot_subgraph` gets corrections placed AT its `t_valid`
    instant (R-10's own text), never over an interval — realized here by
    passing a one-tick `Target.window = (t_valid, t_valid + 1)` into the
    generator, so every placement's "in window" is exactly the instant."""
    store = tgms.open(path, backend=backend)
    sub = probe_substrate(store, rng=rng)
    rel = sub.rel_types[0] if sub.rel_types else "R"
    trials: list[ZeroChangedTrial] = []
    for form in _row_returning_forms(store, sub, rel, rng):
        env, outcome, err = _execute(store, form["op"], form["args"])
        if env is None:
            trials.append(ZeroChangedTrial(
                store=store_label, op=form["op"], args_form=form["args"],
                correction_class="", placement="", changed=False, verdict="",
                rows_returned_baseline=0, outcome=outcome, note=err or ""))
            continue
        n_rows = _rows_of(env)
        scope = DependencyScope.from_json(env["dependency"])
        if form["op"] == "snapshot_subgraph":
            t_valid = form["t_valid"]
            target = Target(read_uids=(), window=(t_valid, t_valid + 1))
        else:
            win = form["args"].get("window")
            target = Target(
                read_uids=tuple(str(form["args"][k]) for k in ("src", "dst")
                                if isinstance(form["args"].get(k), str)),
                window=(win["t_a"], win["t_b"]) if isinstance(win, dict) else None)
        probe = tgms.open(path, backend=backend)
        corrections = generate(probe, sub, target, rng=rng,
                               generators=("b_overwrite", "c1_whole", "c2_sub",
                                          "d1_truncate", "d2_full"))
        probe.close()
        for correction in corrections[:n_per_op]:
            work = _isolated_copy(path)
            wstore = tgms.open(work, backend=backend)
            try:
                try:
                    wstore._write(list(correction.ops))
                except TgmsError as e:
                    trials.append(ZeroChangedTrial(
                        store=store_label, op=form["op"], args_form=form["args"],
                        correction_class=correction.cls, placement=correction.placement,
                        changed=False, verdict="", rows_returned_baseline=n_rows,
                        outcome=OUTCOME_NOT_INJECTED, note=str(e)))
                    continue
                after_env, outcome2, err2 = _execute(wstore, form["op"], form["args"])
                if after_env is None:
                    trials.append(ZeroChangedTrial(
                        store=store_label, op=form["op"], args_form=form["args"],
                        correction_class=correction.cls, placement=correction.placement,
                        changed=False, verdict="", rows_returned_baseline=n_rows,
                        outcome=outcome2, note=err2 or ""))
                    continue
                changed = after_env["result_digest"] != env["result_digest"]
                verdict = check(scope, wstore.eventlog)
                trials.append(ZeroChangedTrial(
                    store=store_label, op=form["op"], args_form=form["args"],
                    correction_class=correction.cls, placement=correction.placement,
                    changed=changed, verdict=verdict.state, rows_returned_baseline=n_rows))
            finally:
                wstore.close()
                shutil.rmtree(work.parent, ignore_errors=True)
        log_line(f"ok  zero-changed {store_label} {form['op']}: "
                f"rows_baseline={n_rows} trials={len([t for t in trials if t.op == form['op']])}")
    store.close()
    return trials


# ===========================================================================
# Arm 4 — §B2 / §E / Gate C: propagation
# ===========================================================================

@dataclass
class PropagationDecision:
    parent_name: str
    child_name: str
    parent_gen_before: int
    parent_gen_after: int
    parent_payload_changed: bool
    child_verdict_before: str
    child_flagged_by_parent_recheck: bool
    child_recomputed: bool
    child_verdict_after_refresh: str
    false_safe: bool
    intersects_calls: int = 0
    candidate_survivors: int = 0

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _register_operator_artifact(store_dir: Path, store: Any, registry: Any, *,
                                name: str, op: str, args: dict[str, Any],
                                parents: tuple[Any, ...] = ()) -> Any:
    """The `"operator"`-kind registration idiom
    (`tests/test_artifact_refresh.py::test_operator_kind_refresh_end_to_end`):
    a `ToolRouter` call, a `{"op", "args"}` blob under `ops/`, and a
    registration built from that call's own envelope."""
    from tgms.tools.server import ToolRouter
    from tgms.tgir.depscope import DependencyScope as DS

    router = ToolRouter(store.adapter, tt_source=store)
    env = router.call(op, args)
    if "error" in env:
        raise TgmsError(env.get("message", "operator call refused"))
    meta = router.leaf_meta(op, env)
    ops_dir = store_dir / "ops"
    ops_dir.mkdir(exist_ok=True)
    ref = f"ops/{name}.json"
    (store_dir / ref).write_text(json.dumps({"op": op, "args": args}))
    dependency = DS.from_json(env["dependency"])
    return registry.register(
        name=name, kind="query_result",
        plan={"plan_digest": meta.get("plan_digest"), "node_digest": meta.get("node_digest"),
              "plan_format": None},
        basis={"tt_q": env["tt_q"], "pinned": env["pinned"], "clamped": env["clamped"],
              "tt_q_verified": dependency.tt_q_verified},
        state={"completeness": meta.get("completeness", "unknown"),
              "exactness": meta.get("exactness", "exact"), "refusal": None},
        refresh={"kind": "operator", "ref": ref, "basis_policy": "open"},
        dependency=dependency, parents=parents,
    ), env


def propagation_sweep(store_label: str, path: Path, sub: Substrate, *, backend: str,
                      n_pairs: int, n_rounds: int,
                      rng: random.Random) -> tuple[list[PropagationDecision], dict[str, Any]]:
    """§B2 / §E. `base -> A (temporal aggregate) -> B` (E1's chain shape),
    built as `"operator"`-kind registrations so no TGIR plan blob is needed.
    Each round injects a correction batch, refreshes every parent
    `check_artifact` finds stale, calls `parent_recheck` for each, and scores
    every flagged child against §B2's definition of false-safe: FRESH **and**
    a from-scratch recompute disagrees with the registered payload.

    Returns `(decisions, lookup_counters)` — the latter is D2's per-batch
    `intersects_calls` / `candidate_survivors` tally (R-18's own instrument).
    """
    from tgms.artifact.lookup import affected
    from tgms.artifact.propagate import parent_recheck
    from tgms.artifact.refresh import refresh
    from tgms.artifact.registry import Registry
    from tgms.artifact.witness import check_artifact

    work = _isolated_copy(path)
    store = tgms.open(work, backend=backend)
    registry = Registry(work)
    uids = list(sub.uids) or ["n0"]
    windows = []
    n = max(2, (sub.vt_hi - sub.vt_lo) // max(1, n_pairs))
    t = sub.vt_lo
    while len(windows) < n_pairs and t < sub.vt_hi:
        windows.append({"t_a": t, "t_b": min(sub.vt_hi, t + n)})
        t += n

    #: The last payload this harness itself observed for each artifact name
    #: — populated at registration, updated on every refresh. §B2's
    #: definition needs "the payload the superseded generation carried",
    #: which `ArtifactRecord.payload` does not reliably give here: that
    #: field is populated by `refresh._publish`'s own `ResultStore` write,
    #: never by a plain `registry.register()` call, so an artifact this
    #: harness registers directly (every one of them) starts with
    #: `payload=None`. Tracked here instead of read off the record.
    last_payload: dict[str, dict[str, Any]] = {}

    pairs: list[tuple[Any, Any, str, str]] = []
    for i, win in enumerate(windows):
        a_name, b_name = f"m5-parent-{i}", f"m5-child-{i}"
        try:
            gen_a, env_a = _register_operator_artifact(
                work, store, registry, name=a_name, op="aggregate_events",
                args={"group_by": [{"dim": "endpoint", "role": "src"}],
                     "aggregates": [{"agg": "count"}], "window": win})
            gen_b, env_b = _register_operator_artifact(
                work, store, registry, name=b_name, op="entity_history",
                args={"uid": uids[i % len(uids)]}, parents=(gen_a.id,))
        except TgmsError as e:
            log_line(f"  propagation pair {i}: registration refused ({e}); skipped")
            continue
        last_payload[a_name] = env_a
        last_payload[b_name] = env_b
        pairs.append((gen_a, gen_b, a_name, b_name))

    decisions: list[PropagationDecision] = []
    lookup_counters: list[dict[str, Any]] = []
    #: No window on the correction target (unlike the carve arm's
    #: outside-window design) — this arm wants corrections that land inside
    #: SOME registered artifact's window some of the time, which is what
    #: makes a parent go stale at all. `window=None` spreads placements
    #: uniformly over the store's own extent (`corrections._interval`'s own
    #: fallback), covering the many small per-pair windows above.
    target = Target(read_uids=tuple(uids), window=None)
    edge_pool = _sample_edges(store)
    for round_i in range(n_rounds):
        corrections, _shares = _weighted_corrections(
            store, sub, target, rng, min_bcd_share=0.7, min_outside_share_of_bcd=0.6,
            edges=edge_pool)
        if not corrections:
            continue
        correction = corrections[round_i % len(corrections)]
        try:
            store._write(list(correction.ops))
        except TgmsError:
            continue
        batch = list(store.eventlog.batches())[-1]
        lr = affected(batch, registry)
        lookup_counters.append({"round": round_i, "intersects_calls": lr.intersects_calls,
                                "candidate_survivors": lr.candidate_survivors,
                                "affected": [r.name for r in lr.affected]})

        for gen_a, gen_b, a_name, b_name in pairs:
            current_a = registry.current(a_name)
            if current_a is None:
                continue
            va = check_artifact(current_a, store.eventlog)
            if va.actionable_fresh or va.refresh is None:
                continue
            try:
                new_a = refresh(current_a, va.refresh, store, registry)
            except TgmsError as e:
                log_line(f"  propagation round {round_i} {a_name}: refresh refused ({e})")
                continue
            after_payload_env, _o2, _e2 = _execute(
                store, "aggregate_events",
                json.loads((work / new_a.refresh["ref"]).read_text())["args"])
            # §E2 item 1: `A`'s refreshed payload is byte-identical to a
            # from-scratch recompute at the same basis. `refresh()` IS that
            # recompute internally (`_run_operator` re-calls the same
            # `ToolRouter`), so this independent re-call — same args, the
            # same live state, a second kernel invocation — is the
            # structural check: its `result_digest` is compared to the
            # registration `refresh()` just published, below.
            prev = last_payload.get(a_name)
            payload_changed = (prev is not None and after_payload_env is not None
                              and _value_of(prev) != _value_of(after_payload_env))
            if after_payload_env is not None:
                last_payload[a_name] = after_payload_env

            propagation = parent_recheck(new_a.id, registry)
            flagged_names = {c.record.id for c in propagation.candidates}
            current_b = registry.current(b_name)
            if current_b is None:
                continue
            flagged = current_b.id in flagged_names
            vb_before = check_artifact(current_b, store.eventlog)
            child_recomputed = False
            child_verdict_after = vb_before.steps.to_json()["verdict"]
            false_safe = False
            if flagged and vb_before.refresh is not None:
                # §B2's definition: FRESH *and* a from-scratch recompute of
                # B disagrees with what this harness last observed for it.
                b_args = json.loads((work / current_b.refresh["ref"]).read_text())["args"]
                recompute_env, _o3, _e3 = _execute(store, "entity_history", b_args)
                prev_b = last_payload.get(b_name)
                if vb_before.actionable_fresh and recompute_env is not None and prev_b is not None:
                    false_safe = (_value_of(prev_b) != _value_of(recompute_env))
                if not vb_before.actionable_fresh:
                    new_b = refresh(current_b, vb_before.refresh, store, registry)
                    child_recomputed = True
                    child_verdict_after = check_artifact(
                        new_b, store.eventlog).steps.to_json()["verdict"]
                    b_recompute_env, _o4, _e4 = _execute(store, "entity_history", b_args)
                    if b_recompute_env is not None:
                        last_payload[b_name] = b_recompute_env
                elif recompute_env is not None:
                    last_payload[b_name] = recompute_env

            decisions.append(PropagationDecision(
                parent_name=a_name, child_name=b_name,
                parent_gen_before=current_a.generation, parent_gen_after=new_a.generation,
                parent_payload_changed=bool(payload_changed),
                child_verdict_before=vb_before.steps.to_json()["verdict"],
                child_flagged_by_parent_recheck=flagged, child_recomputed=child_recomputed,
                child_verdict_after_refresh=child_verdict_after, false_safe=false_safe,
                intersects_calls=lr.intersects_calls, candidate_survivors=lr.candidate_survivors))
    store.close()
    shutil.rmtree(work.parent, ignore_errors=True)
    return decisions, {"batches": lookup_counters}


def propagation_determinism_check(path: Path, sub: Substrate, *,
                                  backend: str) -> dict[str, Any]:
    """Gate C criterion 3: replaying the same log + corrections + refresh
    calls into a **fresh** directory yields byte-identical registry
    `record_digest` chains. Uses the clock-free `_apply` idiom
    (`tests/test_artifact_refresh.py`, `scripts/demo_propagation.py`) —
    **the only place in this harness that bypasses `Store`'s normal write
    API** — because `Store._write_locked` ticks a wall-clock-seeded HLC that
    would make two runs of this very check disagree for a reason that has
    nothing to do with the mechanism under test."""
    from tgms.storage.eventlog import EventLog, extend_chain
    from tgms.artifact.propagate import parent_recheck
    from tgms.artifact.refresh import refresh
    from tgms.artifact.registry import Registry
    from tgms.artifact.witness import check_artifact
    from tgms.storage.base import make_op

    def _apply(store: Any, log: Any, tt: int, *ops: dict[str, Any]) -> None:
        batch = list(ops)
        _bid, end_offset, record = log.append(tt, batch)
        note_cursor = getattr(store.adapter, "note_event_cursor", None)
        if note_cursor is not None:
            if store._chain is None:
                store._chain = log.chain_of_prefix(end_offset - len(record))
            store._chain = extend_chain(store._chain, record)
        store.adapter.begin()
        try:
            store.adapter.apply_ops(batch, tt)
        except Exception:
            store.adapter.rollback()
            raise
        if note_cursor is not None:
            note_cursor(end_offset, store._chain)
        store.adapter.commit()

    def _run(store_dir: Path) -> Any:
        store_dir.mkdir(parents=True)
        log = EventLog(store_dir / "eventlog.jsonl")
        uid = (sub.uids[0] if sub.uids else "n0")
        _apply(tgms.open(store_dir, backend=backend), log, 0,
              make_op("assert_node", uid=uid, label=sub.node_label, props={},
                      vt_s=0, vt_e=OPEN_END, source="ingest", provenance_ref=None))
        store = tgms.open(store_dir, backend=backend)
        registry = Registry(store_dir)
        gen_a, _ = _register_operator_artifact(
            store_dir, store, registry, name="det-a", op="entity_history", args={"uid": uid})
        gen_b, _ = _register_operator_artifact(
            store_dir, store, registry, name="det-b", op="entity_history",
            args={"uid": uid}, parents=(gen_a.id,))
        _apply(store, log, 10,
              make_op("correct", ref={"kind": "node", "uid": uid}, props={"w": 1},
                      vt_s=0, vt_e=OPEN_END, source="ingest", provenance_ref=None))
        reader = EventLog(store_dir / "eventlog.jsonl")
        va = check_artifact(gen_a, reader)
        new_a = refresh(gen_a, va.refresh, store, registry)
        parent_recheck(new_a.id, registry)
        vb = check_artifact(gen_b, reader)
        if not vb.actionable_fresh and vb.refresh is not None:
            refresh(gen_b, vb.refresh, store, registry)
        store.close()
        return registry

    d1 = Path(tempfile.mkdtemp()) / "det1"
    d2 = Path(tempfile.mkdtemp()) / "det2"
    try:
        reg1 = _run(d1)
        reg2 = _run(d2)
        log_bytes_equal = (d1 / "eventlog.jsonl").read_bytes() == (d2 / "eventlog.jsonl").read_bytes()
        art_bytes_equal = (d1 / "artifacts.jsonl").read_bytes() == (d2 / "artifacts.jsonl").read_bytes()
        checkpoints_equal = reg1.checkpoint() == reg2.checkpoint()
        digests_equal = all(
            r1.record_digest == r2.record_digest
            for name in reg1.names()
            for r1, r2 in zip(reg1.history(name), reg2.history(name)))
        return {"log_bytes_equal": log_bytes_equal, "artifacts_bytes_equal": art_bytes_equal,
               "checkpoints_equal": checkpoints_equal, "record_digests_equal": digests_equal,
               "deterministic": log_bytes_equal and art_bytes_equal and checkpoints_equal
               and digests_equal}
    finally:
        shutil.rmtree(d1.parent, ignore_errors=True)
        shutil.rmtree(d2.parent, ignore_errors=True)


# ===========================================================================
# Arm 5 — §A7 / §C4: the pinned-scope line
# ===========================================================================

@dataclass
class PinnedTrial:
    store: str
    op: str
    args_form: dict[str, Any]
    as_of_tt: int
    frontier_tt: int
    correction_tt: int
    pinned: bool
    exempted_verdict: str
    stripped_verdict: str
    avoided_by_exemption: bool
    exempt_receipt: dict[str, Any] | None
    scope_path: str = "leaf-basis"   # §A7: the plan-level `scope_of` path never emits `as_of_tt`

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


PINNABLE_OPS = ("entity_history", "version_history", "snapshot_subgraph", "diff_snapshots",
                "neighborhood_evolution", "aggregate_events", "graph_metric_timeseries",
                "burst_detection", "co_active", "count_temporal_motifs",
                "find_temporal_motif_instances", "temporal_reachability", "temporal_paths",
                "resolve_entities")


def pinned_sweep(store_label: str, path: Path, *, backend: str, n_trials: int,
                 rng: random.Random) -> list[PinnedTrial]:
    """§A7 / §C4. `as_of_tt` is passed as a leaf-level argument (14 of 15
    operators take it, per `algebra.as_of_tt_of`'s own docstring), which is
    the leaf/basis path §A7 says the pinned line is measured on — the
    plan-level `scope_of` path never emits it at all (§A7's own fail-safe
    note) and this harness does not attempt to route through it.

    Every trial runs `check()` twice on the identical injected batch: once
    against the scope `call_operator` actually produced (the exemption
    live), once with `as_of_tt` stripped (`dataclasses.replace`) — the
    twice-run difference §A7.2 defines as "invalidations avoided by the
    pinned exemption", never an estimate.
    """
    store = tgms.open(path, backend=backend)
    sub = probe_substrate(store, rng=rng)
    rel = sub.rel_types[0] if sub.rel_types else "R"
    frontier = store.frontier_tt()
    trials: list[PinnedTrial] = []
    ops = [o for o in PINNABLE_OPS if o != "resolve_entities"]
    i = 0
    while len(trials) < n_trials and i < n_trials * 4:
        i += 1
        op = ops[i % len(ops)]
        as_of_tt = sub.vt_lo + (frontier - sub.vt_lo) // 2 if frontier > sub.vt_lo else sub.vt_lo
        uid = sub.uids[i % len(sub.uids)] if sub.uids else "n0"
        args = _pinned_args(op, sub, uid, rel, as_of_tt)
        env, outcome, err = _execute(store, op, args)
        if env is None or not env.get("pinned", False):
            continue  # not realizable pinned on this op/store; try the next
        scope = DependencyScope.from_json(env["dependency"])
        target = Target(read_uids=(uid,) if uid else (), window=(sub.vt_lo, sub.vt_hi))
        probe = tgms.open(path, backend=backend)
        corrections, _s = _weighted_corrections(probe, sub, target, rng,
                                                 min_bcd_share=0.7, min_outside_share_of_bcd=0.6,
                                                 edges=_sample_edges(probe))
        probe.close()
        correction = next((c for c in corrections if c.placement.startswith("outside-window")),
                          corrections[0] if corrections else None)
        if correction is None:
            continue
        work = _isolated_copy(path)
        wstore = tgms.open(work, backend=backend)
        try:
            try:
                wstore._write(list(correction.ops))
            except TgmsError:
                continue
            batch_tt = int(list(wstore.eventlog.batches())[-1]["tt"])
            exempted = check(scope, wstore.eventlog)
            stripped_scope = dataclasses.replace(scope, as_of_tt=None)
            stripped = check(stripped_scope, wstore.eventlog)
            avoided = (exempted.state == "fresh" and stripped.state == "possibly-stale")
            trials.append(PinnedTrial(
                store=store_label, op=op, args_form=args, as_of_tt=as_of_tt,
                frontier_tt=frontier, correction_tt=batch_tt, pinned=True,
                exempted_verdict=exempted.state, stripped_verdict=stripped.state,
                avoided_by_exemption=avoided, exempt_receipt=exempted.exempt))
        finally:
            wstore.close()
            shutil.rmtree(work.parent, ignore_errors=True)
    store.close()
    return trials


def _pinned_args(op: str, sub: Substrate, uid: str, rel: str, as_of_tt: int) -> dict[str, Any]:
    w = {"t_a": sub.vt_lo, "t_b": sub.vt_hi}
    base: dict[str, Any] = {"as_of_tt": as_of_tt}
    if op in ("entity_history",):
        return {**base, "uid": uid}
    if op in ("version_history",):
        return {**base, "kind": "node", "window": w}
    if op == "snapshot_subgraph":
        mid = sub.vt_lo + (sub.vt_hi - sub.vt_lo) // 2
        return {**base, "seeds": [uid], "t_valid": mid, "hops": 1}
    if op == "diff_snapshots":
        mid = sub.vt_lo + (sub.vt_hi - sub.vt_lo) // 2
        return {**base, "t1": sub.vt_lo + 1, "t2": mid}
    if op == "neighborhood_evolution":
        return {**base, "uid": uid, "t1": sub.vt_lo, "t2": sub.vt_hi,
                "stride": max(1, (sub.vt_hi - sub.vt_lo) // 8)}
    if op == "aggregate_events":
        return {**base, "group_by": [{"dim": "endpoint", "role": "src"}],
                "aggregates": [{"agg": "count"}], "window": w}
    if op == "graph_metric_timeseries":
        return {**base, "metric": "node_count", "window": w,
                "stride": max(1, (sub.vt_hi - sub.vt_lo) // 8)}
    if op == "burst_detection":
        return {**base, "target": {"kind": "node_activity", "uid": uid}, "window": w,
                "stride": max(1, (sub.vt_hi - sub.vt_lo) // 16)}
    if op == "co_active":
        return {**base, "a_spec": {"rel_type": rel}, "b_spec": {"rel_type": rel},
                "allen_relation": {"relation": "overlaps"}, "limit": 200}
    if op == "count_temporal_motifs":
        return {**base, "motif": "M_2node_pingpong", "window": w,
                "delta": max(1, (sub.vt_hi - sub.vt_lo) // 8)}
    if op == "find_temporal_motif_instances":
        return {**base, "motif": "M_2node_pingpong", "window": w,
                "delta": max(1, (sub.vt_hi - sub.vt_lo) // 8), "limit": 50}
    if op == "temporal_reachability":
        return {**base, "src": uid, "window": w}
    if op == "temporal_paths":
        return {**base, "src": uid, "dst": uid, "window": w, "k": 2}
    return base


# ===========================================================================
# floor / summary accounting (§G, §B1 FLOOR, R-8/R-10/R-11/R-12/R-16)
# ===========================================================================

def summarize_carve(trials: Sequence[CarveTrial], cfg: dict[str, Any]) -> dict[str, Any]:
    live = [t for t in trials if t.outcome == OUTCOME_OK]
    changed = [t for t in live if t.changed]
    by_form: dict[str, list[CarveTrial]] = {}
    for t in changed:
        by_form.setdefault(t.form, []).append(t)
    rg1a, rg1b = RG1_PAIR
    changed_rg1a = len(by_form.get(rg1a, []))
    changed_rg1b = len(by_form.get(rg1b, []))
    outside_bcd_changed = [t for t in changed if t.outside_window and t.correction_class in "BCD"]

    def arm_bucket(ts: list[CarveTrial], want: str) -> list[CarveTrial]:
        out = []
        for t in ts:
            if want == "carve" and t.arms == ("carve",):
                out.append(t)
            elif want == "value" and t.arms == ("value",):
                out.append(t)
            elif want == "both" and set(t.arms) == {"value", "carve"}:
                out.append(t)
        return out

    carve_only = arm_bucket(outside_bcd_changed, "carve")
    value_only = arm_bucket(outside_bcd_changed, "value")
    both = arm_bucket(outside_bcd_changed, "both")
    denom = len(outside_bcd_changed)
    carve_only_invalidated = [t for t in carve_only if t.verdict == "possibly-stale"]
    dominance_pct = (100.0 * len(carve_only_invalidated) / denom) if denom else None

    r7_bcd = [t for t in live if t.correction_class in ("B", "C", "D")]
    r7_bcd_outside = [t for t in r7_bcd if t.outside_window]

    floor = cfg["carve_arm"]["floor"]
    met = {
        "min_changed_trials": len(outside_bcd_changed) >= floor["min_changed_trials"],
        "min_changed_trials_per_rg1_half": (
            changed_rg1a >= floor["min_changed_trials_per_rg1_half"] and
            changed_rg1b >= floor["min_changed_trials_per_rg1_half"]),
    }
    return {
        "trials": len(trials), "live": len(live), "changed": len(changed),
        "changed_rg1_plain": changed_rg1a, "changed_rg1_duration": changed_rg1b,
        "outside_window_bcd_changed": denom,
        "by_arm": {"value_only": len(value_only), "carve_only": len(carve_only), "both": len(both)},
        "carve_only_invalidated": len(carve_only_invalidated),
        "carve_dominance_pct": dominance_pct,
        "carve_dominates": (dominance_pct is not None and
                            dominance_pct >= cfg["gates"]["gate_b"]["carve_dominance_threshold_pct"]),
        "injection_bcd_share": (len(r7_bcd) / len(live)) if live else None,
        "injection_outside_share_of_bcd": (len(r7_bcd_outside) / len(r7_bcd)) if r7_bcd else None,
        "floor": {"required": floor, "achieved": {
            "min_changed_trials": denom,
            "min_changed_trials_per_rg1_half": min(changed_rg1a, changed_rg1b)},
            "met": met, "all_met": all(met.values())},
        "control_invariant_violations": len([t for t in live if not t.control_invariant_ok]),
    }


def summarize_pattern_l1(trials: Sequence[PatternL1Trial], cfg: dict[str, Any]) -> dict[str, Any]:
    live = [t for t in trials if t.outcome == OUTCOME_OK]
    cells = {t.cell for t in live}
    mixed_cells = {t.cell for t in live if t.mixed}
    lift = [t for t in live if t.level0_verdict == "possibly-stale" and t.level1_verdict == "fresh"]
    regression = [t for t in live if t.level0_verdict == "fresh" and t.level1_verdict == "possibly-stale"]
    req = cfg["pattern_l1"]
    met = {"min_cells": len(cells) >= req["min_cells"],
          "min_mixed_cells": len(mixed_cells) >= req["min_mixed_cells"]}
    return {
        "trials": len(trials), "live": len(live), "cells": len(cells),
        "mixed_cells": len(mixed_cells), "level1_lift_trials": len(lift),
        "level1_unsound_regressions": len(regression),   # MUST be 0 -- Gate A
        "floor": {"required": {"min_cells": req["min_cells"], "min_mixed_cells": req["min_mixed_cells"]},
                 "achieved": {"min_cells": len(cells), "min_mixed_cells": len(mixed_cells)},
                 "met": met, "all_met": all(met.values())},
    }


def summarize_zero_changed(trials: Sequence[ZeroChangedTrial], cfg: dict[str, Any]) -> dict[str, Any]:
    live = [t for t in trials if t.outcome == OUTCOME_OK]
    by_op: dict[str, list[ZeroChangedTrial]] = {}
    for t in live:
        by_op.setdefault(t.op, []).append(t)
    changed_per_op = {op: len([t for t in ts if t.changed]) for op, ts in by_op.items()}
    false_fresh = [t for t in live if t.changed and t.verdict == "fresh"]
    req = cfg["zero_changed_ops"]["min_changed_trials_each"]
    met = {op: n >= req for op, n in changed_per_op.items()}
    for op in cfg["zero_changed_ops"]["operators"]:
        met.setdefault(op, False)
    return {"trials": len(trials), "live": len(live), "changed_per_op": changed_per_op,
           "false_fresh": len(false_fresh), "required_per_op": req, "met": met,
           "all_met": all(met.values())}


def summarize_propagation(decisions: Sequence[PropagationDecision],
                          determinism: dict[str, Any] | None,
                          cfg: dict[str, Any]) -> dict[str, Any]:
    payload_changed = [d for d in decisions if d.parent_payload_changed]
    false_safe = [d for d in decisions if d.false_safe]
    req = cfg["propagation"]
    met = {"min_decisions": len(decisions) >= req["min_decisions"],
          "min_decisions_after_payload_change": len(payload_changed) >=
          req["min_decisions_after_payload_change"]}
    gate_c = None
    if determinism is not None:
        gate_c = {
            "criterion_1_byte_identical_recompute": "see per-decision payload_changed comparisons",
            "criterion_2_recheck_correct_vs_recompute": len(false_safe) == 0,
            "criterion_3_deterministic_replay": determinism["deterministic"],
            "criterion_4_zero_false_safe_at_floor": (len(false_safe) == 0 and met["min_decisions"]),
            "pass": (determinism["deterministic"] and len(false_safe) == 0 and met["min_decisions"]),
        }
    return {"decisions": len(decisions), "payload_changed": len(payload_changed),
           "false_safe": len(false_safe), "floor": {"required": req,
           "achieved": {"min_decisions": len(decisions),
                       "min_decisions_after_payload_change": len(payload_changed)},
           "met": met, "all_met": all(met.values())},
           "determinism": determinism, "gate_c": gate_c}


def summarize_pinned(trials: Sequence[PinnedTrial], cfg: dict[str, Any]) -> dict[str, Any]:
    req = cfg["pinned"]["min_trials"]
    avoided = [t for t in trials if t.avoided_by_exemption]
    receipts = [t.exempt_receipt for t in trials if t.exempt_receipt is not None]
    return {"trials": len(trials), "invalidations_avoided_by_exemption": len(avoided),
           "exemption_receipts_present": len(receipts),
           "exemption_receipts_present_but_empty": len(trials) - len(receipts),
           "floor": {"required": req, "achieved": len(trials), "met": len(trials) >= req,
                    "note": "reported, not passed (R-16)"}}


# ===========================================================================
# IO — one record file per arm, receipted (§A10 / M4's per-file shape)
# ===========================================================================

def _write_record(out_dir: Path, name: str, receipt_obj: dict[str, Any],
                  summary: dict[str, Any], rows: Sequence[Any], *, scorable: bool) -> Path:
    record = {**receipt_obj, "arm": name, "scorable": scorable, "summary": summary,
             "row_count": len(rows), "rows": [r.to_json() if hasattr(r, "to_json") else r
                                              for r in rows]}
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.json"
    path.write_text(json.dumps(record, indent=1, sort_keys=False))
    log_line(f"wrote {path} ({len(rows)} rows, scorable={scorable})")
    return path


# ===========================================================================
# profiles
# ===========================================================================

@dataclass
class Profile:
    name: str
    scorable: bool
    carve_cells_per_form: int
    carve_corrections_cap: int
    zero_changed_n_per_op: int
    propagation_pairs: int
    propagation_rounds: int
    pinned_trials: int
    seed: int = 20260827


def smoke_profile() -> Profile:
    return Profile("smoke", scorable=False, carve_cells_per_form=6,
                   carve_corrections_cap=6, zero_changed_n_per_op=4,
                   propagation_pairs=4, propagation_rounds=6, pinned_trials=6)


def full_profile(cfg: dict[str, Any]) -> Profile:
    return Profile("full", scorable=True,
                   carve_cells_per_form=cfg["carve_arm"]["min_cells_per_store_form"],  # R-5
                   carve_corrections_cap=40,
                   zero_changed_n_per_op=cfg["zero_changed_ops"]["min_changed_trials_each"] * 3,
                   propagation_pairs=30, propagation_rounds=20,
                   pinned_trials=cfg["pinned"]["min_trials"] * 2)


# ===========================================================================
# entry point
# ===========================================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=("smoke", "full"), default=None,
                    help="overridden to 'smoke' by --smoke")
    ap.add_argument("--smoke", action="store_true",
                    help="a tiny, throwaway population; records are marked "
                        "scorable=false and never land in benchmarks/m5-v1/ "
                        "unless --out is given explicitly")
    ap.add_argument("--backend", default="native")
    ap.add_argument("--out", default=None)
    ap.add_argument("--stores", nargs="*", default=None,
                    help="restrict to these store labels (smoke default: ldbc-fixture)")
    ap.add_argument("--pidfile", default=None)
    ap.add_argument("--campaign-yaml", default=str(CAMPAIGN_YAML))
    args = ap.parse_args()

    refuse_unless_native(args.backend)
    cfg = load_campaign(Path(args.campaign_yaml))

    global CELL_TIMEOUT_S
    CELL_TIMEOUT_S = int(cfg["wall_ceiling_s_per_cell"])

    profile_name = "smoke" if args.smoke else (args.profile or "full")
    if profile_name == "smoke":
        override = os.environ.get("TGMS_M5_CELL_TIMEOUT_S")
        if override is not None:
            CELL_TIMEOUT_S = int(override)  # smoke-only; a scored run always uses §A10's 600s
    sha = _git_sha()
    log_line(f"RUN_STARTED commit={sha} profile={profile_name} backend={args.backend} "
            f"host={platform.node()} campaign_yaml_sha256={_sha256(Path(args.campaign_yaml))[:12]}")

    if args.pidfile:
        write_pidfile(Path(args.pidfile))

    if profile_name == "smoke":
        profile = smoke_profile()
        default_out = Path(tempfile.gettempdir()) / "tgms-m5-smoke"
        out_dir = Path(args.out) if args.out else default_out
        store_labels = args.stores or ["ldbc-fixture"]
        store_index = {s["label"]: s for s in cfg["stores"]["soundness_only"] + cfg["stores"]["scored"]}
    else:
        profile = full_profile(cfg)
        out_dir = Path(args.out) if args.out else (ROOT / "benchmarks/m5-v1")
        store_labels = args.stores or [s["label"] for s in cfg["stores"]["scored"]]
        store_index = {s["label"]: s for s in cfg["stores"]["scored"]}

    rng = random.Random(profile.seed)
    all_ok = True

    for label in store_labels:
        meta = store_index.get(label)
        if meta is None:
            log_line(f"SKIP {label}: not in campaign.yaml's store inventory")
            continue
        path = ROOT / meta["path"]
        if not path.exists():
            log_line(f"SKIP {label}: no store at {path}")
            continue
        single_typed = meta.get("single_typed", True)
        rel_types = tuple(meta.get("rel_types", ()))

        log_line(f"=== {label} ===")
        _s = tgms.open(path, backend=args.backend)
        store_digest = _s.digest()
        _s.close()
        is_carve_store = meta.get("role") == "carve-continuity"

        if is_carve_store:
            carve_trials = carve_arm_sweep(
                label, path, cfg, backend=args.backend,
                cells_per_form=profile.carve_cells_per_form,
                corrections_cap=profile.carve_corrections_cap, rng=rng)
            carve_summary = summarize_carve(carve_trials, cfg)
            if carve_summary["control_invariant_violations"]:
                log_line(f"C1 INVARIANT VIOLATED on {label}: "
                        f"{carve_summary['control_invariant_violations']} trials "
                        f"-- instrument defect, blocking")
                all_ok = False
            _write_record(out_dir, f"carve-arm-{label}",
                         receipt(profile=profile.name, store_label=label, backend=args.backend, store_identity=store_digest),
                         carve_summary, carve_trials, scorable=profile.scorable)

        pattern_trials = pattern_l1_sweep(
            label, path, cfg, backend=args.backend, single_typed=single_typed,
            rel_types=rel_types, n_corrections=profile.carve_corrections_cap, rng=rng)
        _write_record(out_dir, f"pattern-l1-{label}",
                     receipt(profile=profile.name, store_label=label, backend=args.backend, store_identity=store_digest),
                     summarize_pattern_l1(pattern_trials, cfg), pattern_trials,
                     scorable=profile.scorable)

        zero_trials = zero_changed_ops_sweep(
            label, path, backend=args.backend, n_per_op=profile.zero_changed_n_per_op, rng=rng)
        _write_record(out_dir, f"zero-changed-ops-{label}",
                     receipt(profile=profile.name, store_label=label, backend=args.backend, store_identity=store_digest),
                     summarize_zero_changed(zero_trials, cfg), zero_trials,
                     scorable=profile.scorable)

        pinned_trials_ = pinned_sweep(label, path, backend=args.backend,
                                      n_trials=profile.pinned_trials, rng=rng)
        _write_record(out_dir, f"pinned-{label}",
                     receipt(profile=profile.name, store_label=label, backend=args.backend, store_identity=store_digest),
                     summarize_pinned(pinned_trials_, cfg), pinned_trials_,
                     scorable=profile.scorable)

        store = tgms.open(path, backend=args.backend)
        sub = probe_substrate(store, rng=rng)
        store.close()
        decisions, lookup_extra = propagation_sweep(
            label, path, sub, backend=args.backend, n_pairs=profile.propagation_pairs,
            n_rounds=profile.propagation_rounds, rng=rng)
        determinism = propagation_determinism_check(path, sub, backend=args.backend)
        prop_summary = summarize_propagation(decisions, determinism, cfg)
        prop_summary["lookup_counters"] = lookup_extra
        if prop_summary.get("gate_c") and not prop_summary["gate_c"]["pass"]:
            log_line(f"Gate C not satisfied on {label} at this profile's population "
                    f"(see propagation-{label}.json)")
        _write_record(out_dir, f"propagation-{label}",
                     receipt(profile=profile.name, store_label=label, backend=args.backend, store_identity=store_digest),
                     prop_summary, decisions, scorable=profile.scorable)

    log_line(f"DONE profile={profile_name} out={out_dir} ok={all_ok}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
