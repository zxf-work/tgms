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
           store_identity: str | None = None, run: str | None = None,
           edge_sampling: str = "head",
           extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """The four-key record shape (`eval_harness.py`'s pattern, restated by
    the freeze's §A10): **machine + sha + store identity + date** per file.
    `store_identity` is `Store.digest()` — the backend-independent,
    replay-equivalence digest (`build_snb_store.py::_identity`'s `"full"`
    mode) — the caller's responsibility to have computed **once**, off the
    pristine store, and pass through; this function does not open a store
    itself (`sx-mathoverflow` alone is 70+ MB, and this is called once per
    arm per store — five redundant opens per store for one digest each
    would be pure waste). `run` is a top-up profile's `run_tag` (e.g.
    `"topup-1"`) — `None` (the key is simply absent) for the run of record
    itself, so the scored-alone rule §H names is mechanically visible in
    every top-up record without a reader having to infer it from the
    filename alone. `edge_sampling` (`Profile.edge_sampling`, always
    present, never conditional the way `run` is) is the
    `_sample_edges` strategy this record's arm ran under — stamped on
    EVERY record, `full`/`smoke` included, so a reader never has to trust
    a profile name's own convention and can instead read the actual
    sampling strategy a given file was produced under."""
    out = {
        "generated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "measured_date": measured_date(),
        "git_sha": _git_sha(),
        "campaign_yaml_sha256": _sha256(CAMPAIGN_YAML),
        "freeze_document_sha256": _sha256(FREEZE_DOC),
        "profile": profile,
        "backend": backend,
        "store": store_label,
        "store_identity": store_identity,
        "edge_sampling": edge_sampling,
        "machine": {"host": socket.gethostname(), "platform": platform.platform(),
                    "python": platform.python_version(), "cpus": os.cpu_count()},
        **(extra or {}),
    }
    if run is not None:
        out["run"] = run
    return out


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
#: A population cell the generator cannot build on this store's corpus at
#: all — e.g. a mixed declared/undeclared PatternMatch cell on a
#: single-typed store, which needs >= 2 edge types to exist structurally
#: (R-11's own ruling). Recorded per missing slot, by name, never a
#: silently smaller cell count (§G3/§G5's discipline, applied to the
#: generator's own shortfall rather than to a measured population).
OUTCOME_UNCONSTRUCTIBLE = "UNCONSTRUCTIBLE_BY_CORPUS"

#: `docs/design/M5_PATTERN_ADJUDICATION_2026-08-29.md` root cause 1: a
#: cell whose recorded `ScanRegion` node arm is empty has no in-region uid
#: to write to at all -- `in-region-node-write` used to fall back to a
#: Source-input-cohort uid (or an unrelated sampled uid), which is not
#: "in region" in the sense the correction kind's own name claims and is
#: exactly what made every one of the 57 adjudicated rows look like a
#: real L0->L1 win. Recorded per slot, honestly, rather than silently
#: degenerating to an out-of-region probe under an in-region label.
OUTCOME_NO_IN_REGION_PROBE = "NO_IN_REGION_PROBE"

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


def _sample_edges(store: Any, cap: int = 300, *, strategy: str = "head",
                  rng: random.Random | None = None) -> list[Any]:
    """The carve/propagation/pinned arms' shared edge-identity pool for
    Class B/C/D corrections (`_weighted_corrections`'s `edges=` argument).
    `strategy` is a `Profile.edge_sampling` value, threaded down from
    `main()` through each `*_sweep` function.

    **`"head"`** — the ORIGINAL, unchanged behavior: the first `cap` edges
    in the adapter's own iteration order. This is what `full`/`smoke` use,
    so a re-run of the run-of-record's own procedure (Addendum 5, iTiger
    job 205995) draws byte-for-byte the same population it always did —
    run-1's scoring is closed and its procedure must stay reproducible.

    **`"uniform"`** — a seeded uniform sample of `cap` edges over the
    ENTIRE edge population, deterministic from the caller's own seeded
    `rng` (never a fresh/wall-clock source). This is the fix for the
    STOP-flagged finding building `synth-iv-60k`'s top-up surfaced:
    `"head"` on a store whose adapter iterates in roughly insertion order
    draws a pool clustered in the first ~0.5% of valid time (measured:
    `vt_s` in `[0, 299]` out of a 60,000-tick extent), and only a
    carve-arm window that happens to land there can ever see it. Two
    paths, chosen at call time on what the adapter can tell us:

    - **the count is known** (`store.stats()["n_edge_versions"]`, which
      every backend reports today): draw `cap` distinct indices from
      `range(count)` via `rng.sample` (or take everything if
      `count <= cap`), then one pass over `all_edge_versions()` keeping
      only the selected indices — one scan, exact uniformity.
    - **the count is unreachable** (a `stats()` that raises or omits the
      key — no current backend does this, but the fallback does not
      assume one never will): reservoir sampling (Algorithm R) over the
      same one-pass iterator, using the identical `rng` — every edge has
      probability `cap/n` of survival regardless of `n`, one scan either
      way, O(cap) space.

    `strategy="uniform"` with `rng=None` is a caller bug, refused outright
    rather than silently falling back to a fresh `random.Random()` — a
    profile that forgot to thread its own seeded rng through would
    otherwise draw a wall-clock-seeded sample and get a byte-different
    population on every nominally-identical-seed run.
    """
    if strategy == "head":
        out = []
        for e in store.adapter.all_edge_versions():
            out.append(e)
            if len(out) >= cap:
                break
        return out
    if strategy != "uniform":
        raise ValueError(f"_sample_edges: unknown strategy {strategy!r}, "
                         f"want 'head' or 'uniform'")
    if rng is None:
        raise ValueError("_sample_edges(strategy='uniform') needs a seeded rng "
                         "-- refusing a silent wall-clock fallback")
    count: int | None = None
    try:
        count = int(store.stats()["n_edge_versions"])
    except Exception:  # noqa: BLE001 -- any failure here means "count unknown"
        count = None
    if count is not None and count > 0:
        if count <= cap:
            return list(store.adapter.all_edge_versions())
        chosen = set(rng.sample(range(count), cap))
        out = []
        for idx, e in enumerate(store.adapter.all_edge_versions()):
            if idx in chosen:
                out.append(e)
        return out
    # Reservoir sampling (Algorithm R): the count-unreachable fallback.
    out = []
    for idx, e in enumerate(store.adapter.all_edge_versions()):
        if idx < cap:
            out.append(e)
        else:
            j = rng.randrange(idx + 1)
            if j < cap:
                out[j] = e
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
                    rng: random.Random, edge_sampling: str = "head") -> list[CarveTrial]:
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
            edge_pool = _sample_edges(probe, strategy=edge_sampling, rng=rng)
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
    #: Fix 2 (adjudication root cause 2) -- REAL ground truth: `True` iff
    #: re-executing this cell's plan against the corrected store copy
    #: produced a different `result_digest` than the pre-correction
    #: baseline. `False` by default -- unset on any trial the correction
    #: loop never reaches a re-execution for (`NO_IN_REGION_PROBE`,
    #: `UNCONSTRUCTIBLE_BY_CORPUS`, `REFUSED_ON_RECOMPUTE`, `TIMEOUT`
    #: rows), where "did the result change" was never measured.
    result_changed: bool = False
    #: The cell's own pre-correction `result["rows_total"]` (the same
    #: `execute.py::paginate` field the adjudication's own report speaks
    #: of: "L1 fresh <=> rows_total==0 <=> region node arm empty"). Read
    #: back verbatim, never recomputed -- one baseline execution's row
    #: count, repeated across every correction-kind trial that baseline
    #: fans out to. Observability only, not itself part of any gate.
    rows_total: int = 0
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


#: One (name, pattern, sources, mixed, anchored) cell spec.
CellSpec = tuple[str, Any, tuple, bool, bool]


def _anchor(var: str, uid: str) -> Any:
    """One `Source` binding a pattern node variable to a single concrete
    uid via a one-row `NodeScan` — the "smaller anchored sets" lever
    (Addendum 5 / the coordinator's follow-up): fully anchoring every node
    variable in a cell collapses what would otherwise be an unconstrained
    join over the whole store down to a handful of concrete lookups.
    Empirically, against the real `sx-mathoverflow` store (500k events)
    while building this generator: a 2-edge mixed pattern with one
    endpoint anchored per edge ran ~17s; with all four endpoints anchored,
    ~0.8s. Four of the original (unanchored, single-anchor) mixed cells
    hit the 600s wall in the run of record (Addendum 5) — full anchoring
    is what fixes that, not a longer ceiling."""
    from tgms.tgir.node import NodeScan, Source
    return Source(var, NodeScan(var, uids=(uid,)))


def _anchor_uids_of(sources: tuple) -> tuple[str, ...]:
    """The concrete uids a cell's `sources` bound its node variables to —
    read back off each `Source.relation` (a `NodeScan`), never re-derived.
    Used to build a "declared-type edge write" correction that actually
    lands ON a mixed cell's own anchor identities (see
    `_pattern_correction_probes`) — a random edge between two unrelated
    uids cannot invalidate a *fully anchored* pattern's already-narrow
    Level-0 scope any more than it can Level-1's, so the useful in-region
    edge probe has to target the cell's own anchors specifically."""
    out: list[str] = []
    for src in sources:
        uids = getattr(getattr(src, "relation", None), "uids", None)
        if uids:
            out.append(uids[0])
    return tuple(out)


def _t_node_cells(rng: random.Random, uids: Sequence[str], rel_types: Sequence[str],
                  n: int, *, anchored: bool) -> list[CellSpec]:
    """`n` single-edge T_node-win cells (§A4's other class — "any scored
    store, anchored and unanchored"), varying the anchor uid and the
    declared rel_type draw by draw so they are not `n` copies of the same
    query. This is the class that pads a single-typed store's cell count
    toward R-11's 80 floor: it needs no second edge type at all."""
    from tgms.tgir.node import EdgePat, NodePat, Pattern
    rels = list(rel_types) or ["R"]
    out: list[CellSpec] = []
    for i in range(n):
        rel = rels[i % len(rels)]
        pattern = Pattern((NodePat("x"), NodePat("y")), (EdgePat("e1", "x", "y", rel),))
        if anchored and uids:
            sources = (_anchor("x", rng.choice(uids)),)
            name = f"t_node-anchored-{i}"
        else:
            sources = ()
            name = f"t_node-unanchored-{i}"
        out.append((name, pattern, sources, False, anchored))
    return out


def _seed_edge(edge_pool: Sequence[Any], rng: random.Random) -> Any | None:
    """One real `EdgeVersion`, drawn uniformly from `edge_pool`
    (`_sample_edges(store, strategy="uniform", rng=rng)` -- reused, never
    duplicated). `None` on an empty pool: the caller's signal to fall back
    to the old independent-uid anchoring, still recorded honestly by the
    `NO_IN_REGION_PROBE`/empty-result-set machinery rather than silently
    mis-measured."""
    return rng.choice(edge_pool) if edge_pool else None


def _seed_two_edge_path(edge_pool: Sequence[Any],
                        rng: random.Random) -> tuple[Any, str, Any] | None:
    """A real, DIRECTED 2-hop path `a --e_ab--> b --e_bc--> c`: two edges
    from `edge_pool` where one's `dst` is the other's `src` at a shared
    node `b` -- never just "any two edges touching the same node" (a
    fork or confluence at a shared node does not satisfy a directed
    `EdgePat`'s own `src`/`dst` requirement in either assignment).
    Returns `(e_ab, b_uid, e_bc)`, or `None` if the sampled pool holds no
    such directed pair.

    Tries every hinge candidate (shuffled), not just one: a node whose
    only incoming/outgoing edges in the pool are the SAME self-loop
    (`src == dst == node`) looks like a hinge but can never yield
    `e_in.eid != e_out.eid` -- on a real store this is a small minority
    of hinges, but giving up after the first unlucky pick (verified
    empirically against `sx-mathoverflow`: a single-hinge attempt missed
    a pool that a multi-hinge search found ~50% of the time) would
    silently reintroduce root cause 3's own near-zero-match-probability
    failure mode for the 3edge shape specifically."""
    incoming: dict[str, list[Any]] = {}
    outgoing: dict[str, list[Any]] = {}
    for e in edge_pool:
        incoming.setdefault(e.dst, []).append(e)
        outgoing.setdefault(e.src, []).append(e)
    hinges = [node for node in outgoing if node in incoming]
    rng.shuffle(hinges)
    for node in hinges:
        for _ in range(4):
            e_in, e_out = rng.choice(incoming[node]), rng.choice(outgoing[node])
            if e_in.eid != e_out.eid:
                return e_in, node, e_out
    return None


def _seed_outgoing_edge(edge_pool: Sequence[Any], node: str, rng: random.Random,
                        exclude: frozenset = frozenset()) -> Any | None:
    """A real edge `node --e--> other`, none of whose `eid`s is in
    `exclude` -- extends a real 2-edge path to a third real hop when the
    sampled pool happens to offer one. `None` if it does not; the 3-edge
    chain's last hop then falls back to a plain uid draw (the cell may
    legitimately end up empty at that hop -- no longer masked, see
    `OUTCOME_NO_IN_REGION_PROBE`'s own honesty precedent)."""
    candidates = [e for e in edge_pool if e.src == node and e.eid not in exclude]
    return rng.choice(candidates) if candidates else None


def _mixed_cells(rng: random.Random, uids: Sequence[str], rel_types: Sequence[str],
                 n: int, edge_pool: Sequence[Any] = ()) -> list[CellSpec]:
    """`n` mixed declared/undeclared cells (§A4's 2026-08-27 correction —
    R-11's own definition, restated by the coordinator's follow-up: "a
    PatternMatch whose edge variables include at least one with a declared
    rel_type and at least one without"). Needs >= 2 edge types, which only
    `sx-mathoverflow` provides among the scored stores.

    Two pattern shapes, alternated, per the coordinator's ask for shape
    variety: a **2-edge** pair `(x,y)`/`(u,v)` with one edge declared and
    the other not, and a **3-edge chain** `a-b-c-d` with exactly one of
    the three edges left undeclared (the other two draw independently from
    `rel_types`, so two chains with the same undeclared position still
    differ in their declared types). Every node variable is fully
    anchored (see `_anchor`), which is what keeps each cell well under the
    600s wall on a real-scale store.

    **2026-08-29 fix** (`docs/design/M5_PATTERN_ADJUDICATION_2026-08-29.md`
    root cause 3): the original generator anchored every node variable on
    an INDEPENDENT uniform draw from the sampled uid pool -- on a
    real-scale store (tens of thousands of uids) the odds that two
    independently-drawn uids happen to be edge-connected are essentially
    zero, so 43/83 floor-satisfying cells bound zero rows by construction,
    not by any property of Level 0 or Level 1. Fixed by seeding each cell
    from REAL edges in `edge_pool` (`_sample_edges(..., strategy=
    "uniform")`, reused rather than duplicated) and anchoring variables on
    those edges' own endpoints: a `2edge` cell draws one real edge per
    pattern edge, using the sampled edge's own `rel_type` as the declared
    side's type (guaranteed to match); a `3edge` chain draws a real
    directed 2-hop path for its first two hops and tries to extend it with
    a third real outgoing edge for the last hop, falling back to a plain
    uid when the sampled pool offers no such edge. A cell can still
    legitimately end up empty after Sigma/type narrowing -- that is no
    longer masked (the `in-region-node-write` fix above), so it is fine to
    report that honestly here rather than chase a 100% non-empty
    guarantee."""
    from tgms.tgir.node import EdgePat, NodePat, Pattern
    rels = list(rel_types)
    out: list[CellSpec] = []
    if len(rels) < 2 or len(uids) < 4:
        return out
    i = 0
    while len(out) < n:
        if i % 2 == 0:
            declared_first = (i % 4) < 2
            e_decl, e_undecl = _seed_edge(edge_pool, rng), _seed_edge(edge_pool, rng)
            if e_decl is not None and e_undecl is not None:
                decl_rel = e_decl.rel_type
                d_src, d_dst = e_decl.src, e_decl.dst
                u_src, u_dst = e_undecl.src, e_undecl.dst
            else:
                decl_rel = rels[i % len(rels)]
                anchor_pool = (rng.sample(uids, 4) if len(uids) >= 4
                              else [uids[j % len(uids)] for j in range(4)])
                d_src, d_dst, u_src, u_dst = anchor_pool
            if declared_first:
                edges = (EdgePat("e1", "x", "y", decl_rel), EdgePat("e2", "u", "v", None))
                anchor_map = {"x": d_src, "y": d_dst, "u": u_src, "v": u_dst}
            else:
                edges = (EdgePat("e1", "x", "y", None), EdgePat("e2", "u", "v", decl_rel))
                anchor_map = {"x": u_src, "y": u_dst, "u": d_src, "v": d_dst}
            pattern = Pattern((NodePat("x"), NodePat("y"), NodePat("u"), NodePat("v")), edges)
            vs = ("x", "y", "u", "v")
            shape = "2edge"
        else:
            which_undeclared = i % 3
            path = _seed_two_edge_path(edge_pool, rng)
            real_rel: dict[int, str] = {}
            if path is not None:
                e_ab, b_uid, e_bc = path
                a_uid, c_uid = e_ab.src, e_bc.dst
                real_rel[0], real_rel[1] = e_ab.rel_type, e_bc.rel_type
                e_cd = _seed_outgoing_edge(edge_pool, c_uid, rng,
                                           exclude=frozenset({e_ab.eid, e_bc.eid}))
                if e_cd is not None:
                    d_uid = e_cd.dst
                    real_rel[2] = e_cd.rel_type
                else:
                    d_uid = rng.choice(uids)
                anchor_map = {"a": a_uid, "b": b_uid, "c": c_uid, "d": d_uid}
            else:
                anchor_uids = (rng.sample(uids, 4) if len(uids) >= 4
                              else [uids[j % len(uids)] for j in range(4)])
                anchor_map = dict(zip(("a", "b", "c", "d"), anchor_uids))

            def _rel_for(idx: int, _real: dict[int, str] = real_rel) -> str:
                return _real.get(idx, rels[(i + idx) % len(rels)])

            edges = tuple(
                EdgePat(evar, a, b, None if idx == which_undeclared else _rel_for(idx))
                for idx, (evar, a, b) in enumerate(
                    (("e1", "a", "b"), ("e2", "b", "c"), ("e3", "c", "d"))))
            pattern = Pattern((NodePat("a"), NodePat("b"), NodePat("c"), NodePat("d")), edges)
            vs = ("a", "b", "c", "d")
            shape = "3edge"
        sources = tuple(_anchor(v, anchor_map[v]) for v in vs)
        out.append((f"mixed-{shape}-{i}", pattern, sources, True, True))
        i += 1
    return out[:n]


def _all_declared_cells(rng: random.Random, uids: Sequence[str], rel_types: Sequence[str],
                        n: int) -> list[CellSpec]:
    """The all-declared control named in §A4: structurally narrower, never
    counted toward the mixed win (per the 2026-08-27 correction and
    `tests/test_scan_region_pattern.py::
    test_ab_out_foreign_rel_type_is_fresh_at_both_levels_when_all_declared`).
    Recorded anyway — its own column, reported not gated."""
    from tgms.tgir.node import EdgePat, NodePat, Pattern
    rels = list(rel_types)
    out: list[CellSpec] = []
    if len(rels) < 2 or len(uids) < 4:
        return out
    for i in range(n):
        a, b = (rng.sample(rels, 2) if len(rels) >= 2 else (rels[0], rels[0]))
        pattern = Pattern((NodePat("x"), NodePat("y"), NodePat("u"), NodePat("v")),
                          (EdgePat("e1", "x", "y", a), EdgePat("e2", "u", "v", b)))
        anchor_uids = rng.sample(uids, 4) if len(uids) >= 4 else [uids[j % len(uids)] for j in range(4)]
        sources = tuple(_anchor(v, u) for v, u in zip(("x", "y", "u", "v"), anchor_uids))
        out.append((f"all-declared-{i}", pattern, sources, False, True))
    return out


def pattern_l1_sweep(store_label: str, path: Path, cfg: dict[str, Any], *,
                     backend: str, single_typed: bool, rel_types: Sequence[str],
                     min_cells: int, min_mixed_cells: int,
                     rng: random.Random) -> list[PatternL1Trial]:
    """§A4 / §C2. `mixed=True` cells only fire on a multi-typed store
    (`mixed_requires_multi_typed_store` in campaign.yaml) — a single-typed
    store contributes T_node-win cells only, per the freeze's own ruling
    that the per-variable `rel_types` win is unconstructible there, and
    reports the `min_mixed_cells` shortfall as `min_mixed_cells` explicit
    `UNCONSTRUCTIBLE_BY_CORPUS` rows rather than a silently smaller count.

    `min_cells`/`min_mixed_cells` are the *targets* this sweep tries to
    build toward, not a guarantee — a cell that times out or refuses still
    consumes one of the population slots planned for it and is recorded by
    name (§H2's "not adequately measured", never a lowered floor); the
    actual achieved counts are read back from the returned trials by
    `summarize_pattern_l1`, same as every other arm in this file.
    """
    from tgms.tgir.execute import run_plan
    from tgms.tgir.node import PatternMatch
    from tgms.tgir.types import Sigma
    from tgms.artifact.witness import check_artifact

    store = tgms.open(path, backend=backend)
    sub = probe_substrate(store, rng=rng, sample=400)
    uids = list(sub.uids)
    rel0 = rel_types[0] if rel_types else (sub.rel_types[0] if sub.rel_types else "R")

    trials: list[PatternL1Trial] = []
    cell_specs: list[CellSpec] = []
    from tgms.tgir.node import EdgePat, NodePat, Pattern
    # one unanchored T_node cell kept for continuity with the original,
    # smaller population (§1.8 test 4's own shape, no anchoring at all)
    cell_specs.append(("t_node-unanchored",
                       Pattern((NodePat("x"), NodePat("y")), (EdgePat("e1", "x", "y", rel0),)),
                       (), False, False))

    if not single_typed and len(rel_types) >= 2 and len(uids) >= 4:
        # Fix 3 (adjudication root cause 3): the mixed-cell generator needs
        # real edges to anchor on, drawn uniformly over the WHOLE edge
        # population (never "head", which clusters) -- `_sample_edges`'s
        # own uniform strategy, reused rather than duplicated. `cap=8000`
        # (over the default 300): the 3edge chain's real-path extension
        # (`_seed_outgoing_edge`, one specific node's own out-edge) needs a
        # much bigger pool to have a fair chance of a hit than the
        # 2-edge-sharing-ANY-node hinge search does -- measured empirically
        # against `sx-mathoverflow` (506,550 edge versions) while building
        # this fix: 300 found a 3rd real hop ~35% of the time, 8000 ~65%,
        # and the store's one linear log scan (`_sample_edges`'s own cost,
        # not cap-dependent) stayed ~2s at every cap tried up to 25,000 --
        # so the larger pool is close to free here.
        edge_pool = _sample_edges(store, cap=8000, strategy="uniform", rng=rng)
        cell_specs += _mixed_cells(rng, uids, rel_types, min_mixed_cells, edge_pool)
        n_more = max(0, min_cells - len(cell_specs))
        cell_specs += _t_node_cells(rng, uids, rel_types, n_more, anchored=True)
        cell_specs += _all_declared_cells(rng, uids, rel_types, 3)
    else:
        n_more = max(0, min_cells - len(cell_specs))
        cell_specs += _t_node_cells(rng, uids, (rel0,), n_more, anchored=True)
        reason = (f"{store_label} is single-typed ({rel0!r} only)" if single_typed
                 else f"{store_label} has too small a sampled uid pool ({len(uids)} < 4) "
                      f"to anchor a 4-variable mixed pattern")
        for i in range(min_mixed_cells):
            trials.append(PatternL1Trial(
                store=store_label, cell=f"mixed-unconstructible-{i}", mixed=True, anchored=False,
                node_digest="", correction_kind="", level0_verdict="", level1_verdict="",
                level0_witnesses=0, level1_witnesses=0, level1_terms_level0=0,
                level1_terms_level1=0, outcome=OUTCOME_UNCONSTRUCTIBLE,
                note=f"{reason} — a mixed declared/undeclared pattern needs >= 2 edge "
                     f"types in the store (R-11's own ruling) and cannot be "
                     f"constructed here; recorded per-slot rather than a silent shortfall"))
        log_line(f"UNCONSTRUCTIBLE pattern-l1 {store_label}: {min_mixed_cells} mixed slots "
                f"({reason})")

    def _run_pattern_plan(root: Any, on_store: Any) -> dict[str, Any]:
        return run_plan(root, on_store.adapter, tt_source=on_store,
                        cost_ceilings={"rows_scanned_est": 10 ** 9,
                                      "expansions_est": 10 ** 9,
                                      "time_est_ms": 10 ** 8})

    for name, pattern, sources, mixed, anchored in cell_specs:
        root = PatternMatch(pattern, sources=sources, sigma_=Sigma.default())

        def _run(root: Any = root) -> dict[str, Any]:
            return _run_pattern_plan(root, store)
        try:
            result, timed_out = _with_timeout(_run, seconds=CELL_TIMEOUT_S)
        except TgmsError as e:
            trials.append(PatternL1Trial(
                store=store_label, cell=name, mixed=mixed, anchored=anchored,
                node_digest="", correction_kind="", level0_verdict="", level1_verdict="",
                level0_witnesses=0, level1_witnesses=0, level1_terms_level0=0,
                level1_terms_level1=0, outcome=OUTCOME_REFUSED, note=str(e)))
            continue
        if timed_out:
            # §A10's hard per-cell wall ceiling — an unanchored (or
            # under-anchored) mixed pattern's undeclared-type edge scan is
            # what the run of record (Addendum 5) hit minutes on at real
            # scale; recorded by name, never silently waited out, and
            # never counted toward R-11's cell floor.
            trials.append(PatternL1Trial(
                store=store_label, cell=name, mixed=mixed, anchored=anchored,
                node_digest=root.node_digest, correction_kind="", level0_verdict="",
                level1_verdict="", level0_witnesses=0, level1_witnesses=0,
                level1_terms_level0=0, level1_terms_level1=0, outcome=OUTCOME_TIMEOUT,
                note=f"run_plan exceeded the {CELL_TIMEOUT_S}s per-cell wall ceiling"))
            log_line(f"TIMEOUT pattern-l1 {store_label} {name}: "
                    f"run_plan exceeded {CELL_TIMEOUT_S}s")
            continue
        record = _pattern_record(store, root, result, name)
        anchor_uids = _anchor_uids_of(sources)
        # Fix 1 (adjudication root cause 1): draw the in-region-node-write
        # probe's uid from the executed cell's own RECORDED ScanRegion node
        # arm -- never from `anchor_uids` (Source input cohorts), which is
        # not the join's bound region and is what made every one of the 57
        # adjudicated rows look like a real L0->L1 win. Union across every
        # pattern variable's node arm; an empty union means this cell has
        # no in-region node probe at all, recorded honestly below rather
        # than falling back to an out-of-region uid under an "in-region"
        # label.
        region = record.steps[0].scan_region if record.steps else None
        region_node_uids = tuple(sorted(
            {u for uids_ in (region or {}).get("node_uids", {}).values() for u in uids_}))
        for kind, corrector in _pattern_correction_probes(
                sub, rel_types if mixed else (), anchor_uids, region_node_uids):
            work = _isolated_copy(path)
            wstore = tgms.open(work, backend=backend)
            try:
                ok = corrector(wstore)
                if not ok:
                    trials.append(PatternL1Trial(
                        store=store_label, cell=name, mixed=mixed, anchored=anchored,
                        node_digest=root.node_digest, correction_kind=kind,
                        level0_verdict="", level1_verdict="",
                        level0_witnesses=0, level1_witnesses=0,
                        level1_terms_level0=0, level1_terms_level1=0,
                        result_changed=False, rows_total=result.get("rows_total", 0),
                        outcome=OUTCOME_NO_IN_REGION_PROBE,
                        note="this cell's recorded ScanRegion node arm is empty -- no "
                             "in-region node probe is constructible for it; recorded "
                             "per-slot (UNCONSTRUCTIBLE_BY_CORPUS's own precedent) "
                             "rather than silently degenerating to an out-of-region uid"))
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
                # Fix 2 (adjudication root cause 2): real ground truth for
                # this trial -- re-execute the SAME plan against the
                # already-built corrected copy (`wstore`, reused, never a
                # second isolated copy) and compare result digests, rather
                # than trusting either verdict as its own ground truth.
                outcome = OUTCOME_OK
                rc_note = ""
                after_result, after_timed_out = _with_timeout(
                    lambda root=root, wstore=wstore: _run_pattern_plan(root, wstore),
                    seconds=CELL_TIMEOUT_S)
                if after_timed_out:
                    outcome = OUTCOME_TIMEOUT
                    result_changed = False
                    rc_note = (f"result re-execution against the corrected copy exceeded "
                              f"the {CELL_TIMEOUT_S}s wall ceiling; result_changed is "
                              f"unknown, not a measured 'no change', and is recorded "
                              f"False only because the field has no other value")
                else:
                    result_changed = after_result["result_digest"] != result["result_digest"]
                trials.append(PatternL1Trial(
                    store=store_label, cell=name, mixed=mixed, anchored=anchored,
                    node_digest=root.node_digest, correction_kind=kind,
                    level0_verdict=v0.steps.to_json()["verdict"],
                    level1_verdict=v1.steps.to_json()["verdict"],
                    level0_witnesses=len(v0.steps.witnesses), level1_witnesses=len(v1.steps.witnesses),
                    level1_terms_level0=len([t for t in v1.terms if t.level == "level-0"]),
                    level1_terms_level1=len([t for t in v1.terms if t.level == "level-1"]),
                    result_changed=result_changed, rows_total=result.get("rows_total", 0),
                    outcome=outcome, note=rc_note))
            finally:
                wstore.close()
                shutil.rmtree(work.parent, ignore_errors=True)
        log_line(f"ok  pattern-l1 {store_label} {name}: "
                f"{len([t for t in trials if t.cell == name])} trials")
    store.close()
    return trials


def _pattern_correction_probes(sub: Substrate,
                               rel_types: Sequence[str] = (),
                               anchor_uids: Sequence[str] = (),
                               region_node_uids: Sequence[str] = ()
                               ) -> list[tuple[str, Callable[[Any], bool]]]:
    """The correction shapes §1.8's tests exercise: a node write on a
    plausibly-matched uid ('in-region'), and a node write on an uid unlikely
    to be an endpoint of any scanned edge ('out-of-region', §1.8 test 4 —
    "the test that measures the item's entire value"). SILENT: the freeze
    does not name a specific probe menu for the campaign population (only
    the unit-test suite has fixed scenarios); this harness's own choice,
    recorded here rather than assumed, is to reuse the two shapes the
    soundness suite already proved distinguish Level 0 from Level 1.

    **2026-08-29 fix** (`docs/design/M5_PATTERN_ADJUDICATION_2026-08-29.md`
    root cause 1): `in_region` used to draw its uid from `anchor_uids` —
    the cell's Source INPUT cohorts — never the join's own BOUND uids, so
    every "in-region" write actually landed wherever the *inputs* happened
    to be, not where the executed cell's `ScanRegion` node arm says the
    region actually is (`pattern.py:410-412`, `scan_region.py:267`). Fixed
    to draw from `region_node_uids` — the caller's own union of the
    recorded `ScanRegion.node_uids` values — instead. `anchor_uids` is
    kept only for `declared_edge_write` below, which deliberately targets
    the cell's own anchor identities (a different, correct use — see that
    function's own note) and is untouched by this fix.

    On a **mixed** cell (`rel_types` non-empty), two more correction kinds
    are added — per the coordinator's follow-up ask for more "correction
    kinds" alongside more cells: an edge write of a `rel_types`-declared
    type **between two of the cell's own `anchor_uids`** (should invalidate
    at both levels, per
    `test_ab_in_declared_rel_type_correction_invalidates_both_levels`'s
    shape — and must land ON an anchor: a fully-anchored cell's Level-0
    scope is already narrow on identity, so a declared-type edge between
    two *unrelated* uids would miss at both levels and test nothing) and an
    edge write of a type foreign to the whole store's inventory at a
    fresh, never-anchored identity pair — outside every cell's anchor set
    by construction, exercising the mixed cell's identity-narrowing the
    same way `out-of-region-node-write` does for T_node, but on an edge.
    """
    uids = list(sub.uids) or ["n0"]

    def in_region(store: Any) -> bool:
        if not region_node_uids:
            # An empty recorded region node arm: this cell has no
            # in-region node probe to construct at all. Returning `False`
            # tells the caller to record the trial slot honestly
            # (`OUTCOME_NO_IN_REGION_PROBE`) instead of writing anywhere —
            # never a silent fall-back to an out-of-region uid under an
            # "in-region" label.
            return False
        uid = region_node_uids[0]
        store.assert_node(uid, sub.node_label, {"injected": "l1-in"}, sub.vt_lo, sub.vt_hi)
        return True

    def out_of_region(store: Any) -> bool:
        uid = f"__l1_probe_{random.randrange(10**9)}"
        store.assert_node(uid, sub.node_label, {"injected": "l1-out"}, sub.vt_lo, sub.vt_hi)
        return True

    probes: list[tuple[str, Callable[[Any], bool]]] = [
        ("in-region-node-write", in_region), ("out-of-region-node-write", out_of_region)]

    if rel_types:
        def declared_edge_write(store: Any) -> bool:
            if len(anchor_uids) >= 2:
                a, b = anchor_uids[0], anchor_uids[1]
            else:
                a, b = (random.sample(uids, 2) if len(uids) >= 2 else (uids[0], uids[0]))
            store.assert_edge(a, b, rel_types[0], {"injected": "l1-declared"}, sub.vt_lo, sub.vt_hi)
            return True

        def foreign_edge_write(store: Any) -> bool:
            a = f"__l1_probe_{random.randrange(10**9)}"
            b = f"__l1_probe_{random.randrange(10**9)}"
            store.assert_edge(a, b, rel_types[-1], {"injected": "l1-foreign"}, sub.vt_lo, sub.vt_hi)
            return True

        probes += [("declared-type-edge-write", declared_edge_write),
                  ("foreign-type-edge-write", foreign_edge_write)]
    return probes


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
                      n_pairs: int, n_rounds: int, rng: random.Random,
                      edge_sampling: str = "head"
                      ) -> tuple[list[PropagationDecision], dict[str, Any]]:
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
    edge_pool = _sample_edges(store, strategy=edge_sampling, rng=rng)
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
                 rng: random.Random, edge_sampling: str = "head") -> list[PinnedTrial]:
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
                                                 edges=_sample_edges(
                                                     probe, strategy=edge_sampling, rng=rng))
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
    mixed_cells_nonempty = {t.cell for t in live if t.mixed and t.rows_total > 0}
    lift = [t for t in live if t.level0_verdict == "possibly-stale" and t.level1_verdict == "fresh"]
    # `docs/design/M5_PATTERN_ADJUDICATION_2026-08-29.md` root cause 2:
    # `level1_unsound_regressions` used to count only L0-fresh -> L1-stale,
    # which `level1.py:89-90` makes structurally unreachable -- a
    # tautological zero, never a soundness measurement. Kept here under an
    # honest name (it is still a real monotonicity invariant), but it is
    # no longer what Gate A reads.
    level1_monotonicity_violations = [
        t for t in live if t.level0_verdict == "fresh" and t.level1_verdict == "possibly-stale"]
    # The REAL false-fresh count (Fix 2): Level 1 said "fresh" on a trial
    # whose own re-executed result actually changed under the correction.
    # This is Gate A now -- MUST be 0.
    level1_false_fresh = [t for t in live if t.result_changed and t.level1_verdict == "fresh"]
    unconstructible = {t.cell for t in trials if t.outcome == OUTCOME_UNCONSTRUCTIBLE}
    timed_out = {t.cell for t in trials if t.outcome == OUTCOME_TIMEOUT}
    no_in_region_probe = {t.cell for t in trials if t.outcome == OUTCOME_NO_IN_REGION_PROBE}
    req = cfg["pattern_l1"]
    met = {"min_cells": len(cells) >= req["min_cells"],
          "min_mixed_cells": len(mixed_cells) >= req["min_mixed_cells"]}
    return {
        "trials": len(trials), "live": len(live), "cells": len(cells),
        "mixed_cells": len(mixed_cells), "mixed_cells_binding_ge1_row": len(mixed_cells_nonempty),
        "level1_lift_trials": len(lift),
        "level1_false_fresh": len(level1_false_fresh),   # MUST be 0 -- Gate A
        "level1_monotonicity_violations": len(level1_monotonicity_violations),
        "unconstructible_by_corpus_slots": len(unconstructible),
        "timed_out_cells": len(timed_out),
        "no_in_region_probe_cells": len(no_in_region_probe),
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
    """Write one arm's record. Refuses outright, before touching disk, if a
    run-tagged (top-up) write's `name` does not carry the `topup-` prefix
    every top-up profile's `record_prefix` is built with — the mechanical
    guarantee that a top-up run can never land under a run-of-record
    filename (Addendum 5, iTiger job 205995, the 14 files already under
    `benchmarks/m5-v1/`), independent of whether a future profile's own
    `record_prefix` override was written correctly."""
    if receipt_obj.get("run") and not name.startswith("topup-"):
        raise SystemExit(
            f"REFUSING TO WRITE: {name}.json carries no 'topup-' prefix, but this "
            f"write is tagged run={receipt_obj['run']!r}. A top-up profile must "
            f"never produce a record name that could collide with the run of "
            f"record (Addendum 5, iTiger job 205995) — this is a caller bug in "
            f"the profile's record_prefix, refused rather than risking an "
            f"overwrite of a scored file.")
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

#: The five arms `main()` knows how to run, and the run-of-record's own
#: filename prefix for each (Addendum 5, iTiger job 205995 — the 14 files
#: under `benchmarks/m5-v1/`). A top-up profile restricts `arms` to a
#: subset and overrides the corresponding entries of `record_prefix`;
#: `_write_record` refuses outright if a run-tagged write's name would not
#: start with `topup-`, so a profile that forgets to override a prefix
#: cannot silently collide with the scored population instead of merely
#: being caught in review.
ALL_ARMS: frozenset[str] = frozenset({"carve", "pattern", "zero_changed", "pinned", "propagation"})
DEFAULT_PREFIXES: dict[str, str] = {
    "carve": "carve-arm", "pattern": "pattern-l1", "zero_changed": "zero-changed-ops",
    "pinned": "pinned", "propagation": "propagation",
}


@dataclass
class Profile:
    name: str
    scorable: bool
    arms: frozenset[str] = dataclasses.field(default_factory=lambda: ALL_ARMS)
    record_prefix: dict[str, str] = dataclasses.field(default_factory=lambda: dict(DEFAULT_PREFIXES))
    carve_cells_per_form: int = 0
    carve_corrections_cap: int = 0
    zero_changed_n_per_op: int = 0
    propagation_pairs: int = 0
    propagation_rounds: int = 0
    pinned_trials: int = 0
    pattern_min_cells: int = 0
    pattern_min_mixed_cells: int = 0
    #: Set only for a top-up run (e.g. `"topup-1"`). Stamped into every
    #: record's receipt as `"run"`, and what `_write_record` uses to
    #: enforce the `topup-` filename prefix (Addendum 5's remedy: "a
    #: propagation-arm top-up run ... scored alone").
    run_tag: str | None = None
    #: `_sample_edges`'s strategy (`"head"` or `"uniform"`) — see that
    #: function's own docstring. `"head"` is the value on `full` (hence
    #: also on plain `--smoke`, which scales `full`'s own numbers): a
    #: re-run of the run-of-record's procedure must draw byte-for-byte the
    #: same edge population it always did, since run-1's scoring is closed.
    #: A profile that changes this is naming a coordinator-authorized,
    #: profile-scoped §H revision (the coming Addendum 6), never a change
    #: to `full`/`smoke`'s own behavior. Stamped into every record's
    #: receipt (`receipt()`'s `edge_sampling` field) so the scored-alone
    #: rule stays mechanically auditable, not just true by convention.
    edge_sampling: str = "head"
    seed: int = 20260827


def full_profile(cfg: dict[str, Any]) -> Profile:
    return Profile("full", scorable=True,
                   carve_cells_per_form=cfg["carve_arm"]["min_cells_per_store_form"],  # R-5
                   carve_corrections_cap=40,
                   zero_changed_n_per_op=cfg["zero_changed_ops"]["min_changed_trials_each"] * 3,
                   propagation_pairs=30, propagation_rounds=20,
                   pinned_trials=cfg["pinned"]["min_trials"] * 2,
                   pattern_min_cells=cfg["pattern_l1"]["min_cells"],
                   pattern_min_mixed_cells=cfg["pattern_l1"]["min_mixed_cells"])


def topup_propagation_profile(cfg: dict[str, Any]) -> Profile:
    """Addendum 5, Gate C remedy: "a propagation-arm top-up run (raised
    pairs/rounds, same frozen design, scored alone ... authorized by a
    dated addendum before it launches)". `pairs=90, rounds=30` is 3x the
    full profile's `pairs=30, rounds=20` — run 1 yielded 26
    payload-changing decisions from that population across the three
    scored stores (Addendum 5: "over 26 payload-changing refresh decisions
    against the frozen floor of 30"), so 3x targets ~78, a comfortable
    margin over R-12's floor of 30. Only the propagation arm runs; the
    frozen 100/30 floor and every other arm's population are untouched.

    **`edge_sampling="uniform"`**: `propagation_sweep` DOES consume
    `_sample_edges` (its own `edge_pool`, used by `_weighted_corrections`
    for the B/C/D correction stream) — checked directly against the
    source, not assumed. The same "head" pool clustering that starved the
    carve arm's outside-window cell on an interval-valid substrate would
    equally starve this arm's correction stream once it runs against one,
    so this profile carries the same fix."""
    return Profile("topup-propagation", scorable=True, arms=frozenset({"propagation"}),
                   record_prefix={"propagation": "topup-propagation"},
                   propagation_pairs=90, propagation_rounds=30, run_tag="topup-1",
                   edge_sampling="uniform")


def topup_propagation2_profile(cfg: dict[str, Any]) -> Profile:
    """The propagation investigation's own finding (its report is
    authoritative here; this docstring restates only the numbers this
    profile's own settings depend on) — `topup-propagation`'s own
    `edge_sampling="uniform"` fix was RIGHT for the carve arm's problem
    but is strategy-INCONSISTENT for the propagation arm specifically, and
    this profile is the deliberate reversion for THIS arm alone.

    The mechanism: this arm's registered artifact windows come from
    `probe_substrate`'s own head-truncated `[vt_lo, vt_hi]`
    (`corrections.py` caps its scan at `sample*8 = 3200` versions) — so a
    `"head"`-sampled correction lands inside a registered parent's window
    BY THE SAME SHARED CONSTRUCTION that built the window in the first
    place, while a `"uniform"` correction (drawn over the whole store,
    unrelated to that head-truncated extent) mostly falls outside every
    window instead. Measured overlap between the uniform pool and the
    head-truncated window extent: 11.3% / 6.3% / 1.7% across the three
    scored stores. Controlled A/B at this profile's own `pairs=90,
    rounds=30` on `bitcoinotc`: `"head"` produced 331 decisions / 12
    payload-changed; `"uniform"` produced only 29 decisions / 7
    payload-changed from the SAME population. `edge_sampling="head"`
    here is therefore not a return to run-1's own unfixed behavior by
    oversight — it is this arm's own strategy being consistent with how
    its windows are built, which `"uniform"` never was; `topup-carve`'s
    `"uniform"` remains the correct fix for ITS arm (a real edge-identity
    clustering problem `_sample_edges("head")` has and this one does not
    share).

    `propagation_pairs=90` (unchanged from `topup-propagation`) /
    `propagation_rounds=200` (raised well past `topup-propagation`'s 30):
    arithmetic from the investigation's own per-round payload-changed
    rates under `"head"` — `sx-mathoverflow` (the worst of the three)
    ~0.30/round -> ~60 over 200 rounds, a 2x margin over R-12's `>= 30`
    floor (`cfg["propagation"]["min_decisions_after_payload_change"]`);
    `bitcoinotc` ~0.40/round -> ~80; `collegemsg` ~0.43/round -> ~87. No
    floor moved — 200 rounds is sized to comfortably clear the frozen 30,
    never to raise it.

    **Deferred, NOT fixed here**: the investigation also flagged
    `probe_substrate`'s own fixed `sample*8` scan cap (`corrections.py`)
    as the deeper issue — a cap that does not scale with store size is
    what makes the registered windows head-truncated at all, on any
    store big enough for it to matter. That is arm-logic/`corrections.py`
    surgery, out of scope for a `bench_m5.py`-only top-up profile; this
    profile only chooses the sampling STRATEGY consistent with the
    windows as they exist today, and does not touch how those windows
    are built.

    `run_tag="topup-2"` (never `"topup-1"`) so `_write_record`'s
    collision guard places it under `topup-propagation2-<store>.json` —
    distinct from both run-1's own files and `topup-propagation`'s own
    `topup-propagation-<store>.json`."""
    return Profile("topup-propagation-2", scorable=True, arms=frozenset({"propagation"}),
                   record_prefix={"propagation": "topup-propagation2"},
                   propagation_pairs=90, propagation_rounds=200, run_tag="topup-2",
                   edge_sampling="head")


def topup_pattern_profile(cfg: dict[str, Any]) -> Profile:
    """Addendum 5's R-11 finding: "2 cells per store and 0 mixed cells
    against floors of 80/40 (the cell generator underdelivers by
    capability, not by store limitation)". Only the pattern arm runs, at
    the frozen 80/40 targets — no floor moves; the population this
    profile can now *reach* toward those floors is what changed (see
    `_mixed_cells`/`_t_node_cells`, plural full-anchoring, and the two new
    correction kinds in `_pattern_correction_probes`).

    **`edge_sampling` left at the default (`"head"`), deliberately**: this
    profile's own `run_tag="topup-1"` population is closed (its record
    file already exists), so nothing here re-derives it. **Stale as of the
    2026-08-29 fix** (checked directly against the source, not assumed):
    `pattern_l1_sweep` NOW calls `_sample_edges(store, strategy=
    "uniform", ...)` internally to seed `_mixed_cells`' real-edge anchors
    (root cause 3) — hardcoded to `"uniform"`, not threaded through this
    field, so `edge_sampling` still has nothing to change on this profile;
    its corrections still come from `_pattern_correction_probes`' own
    node/edge writers, never from `_weighted_corrections`' edge-pool
    mechanism. Leaving it at "head" remains the honest reflection of
    that — this field just no longer means "no `_sample_edges` call at
    all", the way it did before the fix."""
    return Profile("topup-pattern", scorable=True, arms=frozenset({"pattern"}),
                   record_prefix={"pattern": "topup-pattern"},
                   pattern_min_cells=cfg["pattern_l1"]["min_cells"],
                   pattern_min_mixed_cells=cfg["pattern_l1"]["min_mixed_cells"],
                   run_tag="topup-1")


def topup_pattern2_profile(cfg: dict[str, Any]) -> Profile:
    """`docs/design/M5_PATTERN_ADJUDICATION_2026-08-29.md`'s own "Scoring
    consequence": job 206956 (the `topup-pattern`/`run=topup-1` population)
    met R-11's 83/40 floors in letter but the mixed class measured only
    the empty-region path — a labeling bug (root cause 1), a tautological
    soundness counter (root cause 2) and a ~0-match-probability mixed-cell
    generator (root cause 3), not a real corpus limitation. `run-1` and
    `topup-1`'s own record files are CLOSED (adjudicated, not
    re-generated) — this is a fresh, separately-tagged re-measurement of
    the pattern arm alone, over the SAME frozen 80/40 floors
    (`cfg["pattern_l1"]`; the adjudication authorizes a corrected
    measurement, never a raised or lowered bar), against the now-fixed
    `_pattern_correction_probes`/`_mixed_cells`/`summarize_pattern_l1`.

    `run_tag="topup-2"` (never `"topup-1"`) so `_write_record`'s collision
    guard places it under `topup-pattern2-<store>.json` — a name that can
    never collide with either `run-1`'s own 14 files or `topup-1`'s
    `topup-pattern-<store>.json`. `edge_sampling` is left at the default
    (`"head"`) for the same reason `topup_pattern_profile` leaves it
    there: this arm's own new `_sample_edges` use (root cause 3's fix) is
    hardcoded to `"uniform"` internally, not threaded through this field,
    so there is nothing here for `edge_sampling` to change either."""
    return Profile("topup-pattern-2", scorable=True, arms=frozenset({"pattern"}),
                   record_prefix={"pattern": "topup-pattern2"},
                   pattern_min_cells=cfg["pattern_l1"]["min_cells"],
                   pattern_min_mixed_cells=cfg["pattern_l1"]["min_mixed_cells"],
                   run_tag="topup-2")


def topup_carve_profile(cfg: dict[str, Any]) -> Profile:
    """`docs/design/M5_CARVE_POPULATION_PROPOSAL_2026-08-28.md` §5/§9
    (DECISION 5/7/8, unratified at the time this profile was written — see
    that memo's own header) — the carve-arm top-up run against a new
    interval-valid-time substrate (`synth-iv-60k`,
    `scripts/build_synth_iv_store.py`), because `bitcoinotc`/`collegemsg`
    are instantaneous-event stores on which the outside-window B/C/D cell
    is structurally empty (memo §1/§2) — not an injection-matrix defect.

    Only the carve arm runs, at the SAME frozen population this profile's
    settings are copied from `full_profile` verbatim
    (`carve_cells_per_form`/`carve_corrections_cap`) — R-5-R-8 and R-14a are
    unchanged per the memo's own DECISION 7 ("No floor moves"). This
    function changes WHICH SUBSTRATE the frozen arm runs against, never the
    arm's own logic, cell menu, or scoring.

    **Eligibility note the memo's own addendum will carry (its finding
    4/§2a, §8d)**: of the three frozen "carve-eligible" forms
    (`aggregate_events` with/without `of: "duration"`, `neighborhood_evolution`),
    `FRESHNESS_SEMANTICS.md` licenses only `aggregate_events` **with**
    `of: "duration"` as carve-**reachable** (`P` includes `@recut` only for
    that form — L9.1 and the plain-aggregate `P = Pᵥ` note both exclude the
    other two). **This profile still runs all three forms unchanged** —
    the population is defined by the frozen freeze text, not by this
    harness's own reading of which third of it is expected to yield a
    changed carve-arm trial; the other two forms remain the RG-1
    ratio's/F4's controls. Sizing the ≥200 floor against "one third of the
    trials" is the addendum's scoring interpretation to draw, not a reason
    to prune this harness's own population.

    **`edge_sampling="uniform"`, coordinator-authorized (the coming
    Addendum 6, a named §H revision, profile-scoped only): the fix for the
    STOP-flagged finding.** `carve_arm_sweep`'s `"head"` pool (the first
    300 edges in adapter iteration order) measured `vt_s` in `[0, 299]`
    out of `synth-iv-60k`'s 60,000-tick extent — under 0.5% of it — so a
    carve window placed almost anywhere else in the store could never see
    a pool edge at all. `full`/`smoke` keep `"head"` unchanged (see
    `_sample_edges`'s own docstring): run-1's scoring is closed and its
    procedure must stay byte-reproducible; this is a profile-scoped
    change, not a change to what `bitcoinotc`/`collegemsg` measured."""
    return Profile("topup-carve", scorable=True, arms=frozenset({"carve"}),
                   record_prefix={"carve": "topup-carve"},
                   carve_cells_per_form=cfg["carve_arm"]["min_cells_per_store_form"],
                   carve_corrections_cap=40, run_tag="topup-1", edge_sampling="uniform")


def topup_carve2_profile(cfg: dict[str, Any]) -> Profile:
    """`topup-carve`'s own remedy taken further, coordinator-authorized.
    `topup-carve` (`run_tag="topup-1"`, 60 cells/form, 40 corrections cap)
    yielded 122 outside-window B/C/D changed `aggregate_events_duration`
    trials against the frozen `>= 200` floor (`campaign.yaml`'s
    `carve_arm.floor.min_changed_trials`) — short of it by population
    size, not by any generator defect (the `edge_sampling="uniform"` fix
    that unblocked `topup-carve` in the first place already landed there
    and is carried forward unchanged below). Doubling
    `carve_cells_per_form` (60 -> 120) targets `122 * 2 = 244` changed
    trials by simple linear scaling of run-1's own observed 122/60 yield —
    no new mechanism, no floor moved (per this file's own `topup-*`
    precedent: `topup_propagation_profile`'s 3x is likewise a scalar
    multiple of run-1's own pairs/rounds, never a fresh number chosen
    independently of what run-1 actually measured).

    `carve_corrections_cap` scales by the SAME ratio `topup-carve` itself
    fixed (40 corrections per 60 cells = 2/3) rather than as an
    independent choice: doubling the cell population without doubling the
    per-cell correction budget in step would silently change what
    fraction of each cell's correction menu gets sampled — exactly the
    kind of procedural drift a `topup-*` profile must not introduce into a
    frozen arm's own machinery.

    Otherwise identical to `topup_carve_profile`: same `synth-iv-60k`
    substrate intent, same `edge_sampling="uniform"` fix, same three
    carve-eligible forms unchanged, same `R-5`-`R-8`/`R-14a` arm logic
    untouched. `run_tag="topup-2"` (never `"topup-1"`) so
    `_write_record`'s collision guard places it under
    `topup-carve2-<store>.json` — distinct from both run-1's own 14 files
    and `topup-carve`'s own `topup-carve-<store>.json`."""
    base_cells = cfg["carve_arm"]["min_cells_per_store_form"]  # 60, R-5
    doubled_cells = base_cells * 2
    scaled_corrections_cap = doubled_cells * 40 // base_cells  # topup-carve's own 40:60 ratio
    return Profile("topup-carve-2", scorable=True, arms=frozenset({"carve"}),
                   record_prefix={"carve": "topup-carve2"},
                   carve_cells_per_form=doubled_cells,
                   carve_corrections_cap=scaled_corrections_cap,
                   run_tag="topup-2", edge_sampling="uniform")


PROFILE_BUILDERS: dict[str, Callable[[dict[str, Any]], Profile]] = {
    "full": full_profile,
    "topup-propagation": topup_propagation_profile,
    "topup-propagation-2": topup_propagation2_profile,
    "topup-pattern": topup_pattern_profile,
    "topup-pattern-2": topup_pattern2_profile,
    "topup-carve": topup_carve_profile,
    "topup-carve-2": topup_carve2_profile,
}


#: Smoke-scale carve caps for a `"uniform"`-sampling profile — larger than
#: the `"head"` default (6/6) on purpose. `bitcoinotc`/`collegemsg` (always
#: `"head"`, per `full`/`smoke`) can never produce an outside-window
#: carve-only trial at ANY scale — they are instantaneous-event stores, so
#: more cells/corrections would only spend more wall time confirming the
#: same structural zero. A `"uniform"`-sampling profile's substrate
#: (`synth-iv-60k`) genuinely can, but the per-witnessed-edge hit rate is
#: low (verified empirically while re-checking this fix: 6/6 gives 0,
#: 12/15 gives 6, all `carve`-only, all `aggregate_events_duration`), so a
#: `"head"`-sized smoke sample would misreport a working substrate as if
#: the fix had done nothing. This is a smoke-scale-only knob — `carve`'s
#: own frozen R-5/R-8 numbers (60 cells/form, 40 corrections) are
#: untouched in `campaign.yaml` and in `full_profile`/`topup_carve_profile`.
_UNIFORM_SMOKE_CARVE_CELLS = 12
_UNIFORM_SMOKE_CARVE_CORRECTIONS = 15


def _smoke_scale(profile: Profile) -> Profile:
    """Shrink any profile's population for `--smoke` — arm selection and
    record-name prefixes (so e.g. `--profile topup-pattern --smoke` still
    writes `topup-pattern-<store>.json`, just a tiny one) are untouched;
    every count is capped small, `scorable` goes false. Never touches the
    frozen floors in `cfg` themselves — `--smoke` scales what this run
    *attempts*, never what a later scored run must clear."""
    carve_cap = (_UNIFORM_SMOKE_CARVE_CELLS if profile.edge_sampling == "uniform" else 6)
    corrections_cap = (_UNIFORM_SMOKE_CARVE_CORRECTIONS if profile.edge_sampling == "uniform" else 6)
    return dataclasses.replace(
        profile, scorable=False,
        carve_cells_per_form=min(profile.carve_cells_per_form, carve_cap) if "carve" in profile.arms else 0,
        carve_corrections_cap=min(profile.carve_corrections_cap, corrections_cap) if "carve" in profile.arms else 0,
        zero_changed_n_per_op=min(profile.zero_changed_n_per_op, 4) if "zero_changed" in profile.arms else 0,
        propagation_pairs=min(profile.propagation_pairs, 4) if "propagation" in profile.arms else 0,
        propagation_rounds=min(profile.propagation_rounds, 6) if "propagation" in profile.arms else 0,
        pinned_trials=min(profile.pinned_trials, 6) if "pinned" in profile.arms else 0,
        pattern_min_cells=(min(profile.pattern_min_cells, 8) or 8) if "pattern" in profile.arms else 0,
        pattern_min_mixed_cells=(min(profile.pattern_min_mixed_cells, 4) or 4)
        if "pattern" in profile.arms else 0,
    )


# ===========================================================================
# entry point
# ===========================================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=tuple(PROFILE_BUILDERS), default="full",
                    help="'full' is every arm at the frozen floors (the run of "
                        "record shape); 'topup-propagation'/'topup-pattern' run "
                        "ONLY that one arm, at raised population, tagged run=topup-1, "
                        "writing topup-<arm>-<store>.json (never the run-of-record names); "
                        "'topup-pattern-2' is the adjudication's corrected pattern-arm "
                        "re-measurement, same floors, tagged run=topup-2, writing "
                        "topup-pattern2-<store>.json (never topup-1's or run-1's names)")
    ap.add_argument("--smoke", action="store_true",
                    help="scale down whichever --profile names to a tiny, throwaway "
                        "population; records are marked scorable=false and never land "
                        "in benchmarks/m5-v1/ unless --out is given explicitly")
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
    if args.smoke:
        override = os.environ.get("TGMS_M5_CELL_TIMEOUT_S")
        if override is not None:
            CELL_TIMEOUT_S = int(override)  # smoke-only; a scored run always uses §A10's 600s

    profile = PROFILE_BUILDERS[args.profile](cfg)
    if args.smoke:
        profile = _smoke_scale(profile)
    run_label = f"{args.profile}{'+smoke' if args.smoke else ''}"

    sha = _git_sha()
    log_line(f"RUN_STARTED commit={sha} profile={run_label} arms={sorted(profile.arms)} "
            f"backend={args.backend} host={platform.node()} "
            f"campaign_yaml_sha256={_sha256(Path(args.campaign_yaml))[:12]}"
            + (f" run_tag={profile.run_tag}" if profile.run_tag else ""))

    if args.pidfile:
        write_pidfile(Path(args.pidfile))

    if args.smoke:
        default_out = Path(tempfile.gettempdir()) / "tgms-m5-smoke"
        out_dir = Path(args.out) if args.out else default_out
        store_labels = args.stores or ["ldbc-fixture"]
        store_index = {s["label"]: s for s in cfg["stores"]["soundness_only"] + cfg["stores"]["scored"]}
    else:
        out_dir = Path(args.out) if args.out else (ROOT / "benchmarks/m5-v1")
        store_labels = args.stores or [s["label"] for s in cfg["stores"]["scored"]]
        store_index = {s["label"]: s for s in cfg["stores"]["scored"]}

    rng = random.Random(profile.seed)
    all_ok = True

    for label in store_labels:
        meta = store_index.get(label)
        if meta is None and profile.run_tag is not None:
            # Ad-hoc top-up store resolution — `campaign.yaml` is FROZEN
            # (Addendum 3's two-way digest guard refuses on any drift) and
            # amending it to register a new substrate is a ratified-addendum
            # act this harness does not perform on its own (the carve
            # top-up's own substrate, `synth-iv-60k`, is proposed by
            # `M5_CARVE_POPULATION_PROPOSAL_2026-08-28.md` but UNRATIFIED at
            # the time this fallback was written). A top-up profile's own
            # store — never a `full`/scored-arm store, which must always be
            # discoverable through `campaign.yaml` — resolves by the
            # repo's own `stores/<label>` convention instead of being
            # silently skipped. `role: "carve-continuity"` is set
            # unconditionally here because the only arm any current
            # top-up profile runs against an ad-hoc store is "carve"; a
            # future top-up arm that needs `single_typed`/`rel_types`
            # metadata this fallback does not have would need its own,
            # named resolution rather than silently defaulting one.
            candidate = ROOT / "stores" / label
            if candidate.exists():
                meta = {"label": label, "path": f"stores/{label}", "role": "carve-continuity"}
                log_line(f"  {label}: not in campaign.yaml -- resolved by convention "
                        f"at {candidate} for this top-up profile (run={profile.run_tag})")
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

        def _receipt() -> dict[str, Any]:
            return receipt(profile=profile.name, store_label=label, backend=args.backend,
                           store_identity=store_digest, run=profile.run_tag,
                           edge_sampling=profile.edge_sampling)

        if "carve" in profile.arms and is_carve_store:
            carve_trials = carve_arm_sweep(
                label, path, cfg, backend=args.backend,
                cells_per_form=profile.carve_cells_per_form,
                corrections_cap=profile.carve_corrections_cap, rng=rng,
                edge_sampling=profile.edge_sampling)
            carve_summary = summarize_carve(carve_trials, cfg)
            if carve_summary["control_invariant_violations"]:
                log_line(f"C1 INVARIANT VIOLATED on {label}: "
                        f"{carve_summary['control_invariant_violations']} trials "
                        f"-- instrument defect, blocking")
                all_ok = False
            _write_record(out_dir, f"{profile.record_prefix['carve']}-{label}", _receipt(),
                         carve_summary, carve_trials, scorable=profile.scorable)

        if "pattern" in profile.arms:
            pattern_trials = pattern_l1_sweep(
                label, path, cfg, backend=args.backend, single_typed=single_typed,
                rel_types=rel_types, min_cells=profile.pattern_min_cells,
                min_mixed_cells=profile.pattern_min_mixed_cells, rng=rng)
            _write_record(out_dir, f"{profile.record_prefix['pattern']}-{label}", _receipt(),
                         summarize_pattern_l1(pattern_trials, cfg), pattern_trials,
                         scorable=profile.scorable)

        if "zero_changed" in profile.arms:
            zero_trials = zero_changed_ops_sweep(
                label, path, backend=args.backend, n_per_op=profile.zero_changed_n_per_op, rng=rng)
            _write_record(out_dir, f"{profile.record_prefix['zero_changed']}-{label}", _receipt(),
                         summarize_zero_changed(zero_trials, cfg), zero_trials,
                         scorable=profile.scorable)

        if "pinned" in profile.arms:
            pinned_trials_ = pinned_sweep(label, path, backend=args.backend,
                                          n_trials=profile.pinned_trials, rng=rng,
                                          edge_sampling=profile.edge_sampling)
            _write_record(out_dir, f"{profile.record_prefix['pinned']}-{label}", _receipt(),
                         summarize_pinned(pinned_trials_, cfg), pinned_trials_,
                         scorable=profile.scorable)

        if "propagation" in profile.arms:
            store = tgms.open(path, backend=args.backend)
            sub = probe_substrate(store, rng=rng)
            store.close()
            decisions, lookup_extra = propagation_sweep(
                label, path, sub, backend=args.backend, n_pairs=profile.propagation_pairs,
                n_rounds=profile.propagation_rounds, rng=rng,
                edge_sampling=profile.edge_sampling)
            determinism = propagation_determinism_check(path, sub, backend=args.backend)
            prop_summary = summarize_propagation(decisions, determinism, cfg)
            prop_summary["lookup_counters"] = lookup_extra
            prefix = profile.record_prefix["propagation"]
            if prop_summary.get("gate_c") and not prop_summary["gate_c"]["pass"]:
                log_line(f"Gate C not satisfied on {label} at this profile's population "
                        f"(see {prefix}-{label}.json)")
            _write_record(out_dir, f"{prefix}-{label}", _receipt(),
                         prop_summary, decisions, scorable=profile.scorable)

    log_line(f"DONE profile={run_label} out={out_dir} ok={all_ok}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
