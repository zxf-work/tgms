"""M4.6 — turn the harness records into `docs/design/M4_MEASURED_REPORT.md`.

**Appends only.** Sections 1 and 2 of the report are the forecast and the
denominator floor, both committed *before* the first headline trial ran. This
script re-reads them, never rewrites them, and adds the measurement below the
frozen marker. Every number it prints is derived from
`benchmarks/freshness-v1/trials-*.json` — there is no path by which a number in
the report was typed by hand.

    uv run python scripts/gen_freshness_report.py
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bench_freshness import (  # noqa: E402
    EXCLUDED_OPS, FLOOR, MEASURED_OPS, PRECISION_STORES,
)

REPORT = ROOT / "docs/design/M4_MEASURED_REPORT.md"
RECORDS = ROOT / "benchmarks/freshness-v1"

#: The marker section 2 ends with. Everything after it is regenerated; nothing
#: at or above it is ever touched.
FROZEN_MARKER = "---\n"

#: §8.2's table, transcribed from the **frozen** section 2 of the report. The
#: ranges are predictions and are scored, not adjusted.
FORECAST: tuple[tuple[str, str, tuple[str, ...], float | None, float | None, str], ...] = (
    ("F1", "narrow, carve-immune", ("neighborhood_evolution",), 0.7, None,
     "> 0.7 — highest of the fifteen"),
    ("F2", "narrow, carve-moot", ("entity_history", "co_active"), 0.5, 0.8,
     "0.5–0.8, unaffected by the carve arm"),
    ("F3", "event-keyed medium",
     ("aggregate_events", "burst_detection", "count_temporal_motifs",
      "find_temporal_motif_instances", "graph_metric_timeseries"), 0.3, 0.6,
     "0.3–0.6, window-placement-sensitive"),
    ("F5", "broad, carve-immune", ("temporal_reachability", "diff_snapshots"),
     None, None, "low-moderate; better than `temporal_paths`"),
    ("F6", "broad, carve-reachable",
     ("version_history", "snapshot_subgraph", "temporal_paths"), None, None,
     "worst; near the base rate"),
    ("F7", "∅", ("compute",), None, None, "FRESH always; 0 invalidations"),
)

#: F3's `aggregate_events` cells that are NOT the `duration` form. F4 is the
#: A/B between them and is scored separately.
RG1_PLAIN = "aggregate_events/count-endpoint"
RG1_DURATION = "aggregate_events/max-duration"


def load(name: str) -> dict[str, Any] | None:
    path = RECORDS / f"trials-{name}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def live(trials: Sequence[dict]) -> list[dict]:
    return [t for t in trials if t["outcome"] == "OK"]


def precision_pool(trials: Iterable[dict]) -> list[dict]:
    """The `POSSIBLY_STALE` row, excluding the refused/errored cell and
    excluding every substrate that cannot carry an honest ratio (§4.2)."""
    return [t for t in trials
            if t["outcome"] == "OK" and t["verdict"] != "fresh"
            and t["store"] in PRECISION_STORES]


def precision(trials: Iterable[dict]) -> tuple[float | None, int, int]:
    pool = list(trials)
    if not pool:
        return None, 0, 0
    hits = [t for t in pool if t["changed"]]
    return len(hits) / len(pool), len(hits), len(pool)


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x:.3f}"


def _carve_cell(trials: Iterable[dict]) -> list[dict]:
    """Class B/C/D × outside-window — the cell §8.2 predicts, and the placement
    where a value-arm-only mechanism returns `FRESH` and is wrong."""
    return [t for t in trials if t["cls"] in ("B", "C", "D")
            and t["placement"].startswith("outside-window")]


# ---------------------------------------------------------------------------
# the sections
# ---------------------------------------------------------------------------

def headline(full: dict, fixture: dict | None) -> str:
    s = full["summary"]
    all_trials = full["trials"] + ((fixture or {}).get("trials") or [])
    sound = [t for t in live(all_trials) if t["changed"]]
    ff = [t for t in sound if t["verdict"] == "fresh"]
    ff_value = [t for t in ff if t["value_changed"]]
    floor = s["floor"]
    verdict = ("**MET**" if s["false_fresh"] == 0 and floor["all_met"]
               else "**NOT MET — reported as not adequately measured**"
               if s["false_fresh"] == 0 else "**FAILED — the mechanism is unsound**")
    rows = "\n".join(
        f"| {k} | {v} | {floor['achieved'][k]} | "
        f"{'yes' if floor['met'][k] else '**no**'} |"
        for k, v in FLOOR.items())
    return f"""
## 3. Headline

> **false-fresh = {len(ff)}** over a `changed` column of **{len(sound)}**
> across every substrate, of which **{len([t for t in sound if t['value_changed']])}**
> are value-changed and **{len([t for t in sound if t['digest_only_changed']])}**
> are digest-only.
>
> On the value-changed half alone: **false-fresh = {len(ff_value)}**.

A false-fresh event is a **soundness violation**, not a finding: it means the
mechanism said `FRESH` about an answer that a recomputation showed had changed,
which is exactly what D1.13 forbids. The count is judged on the **full**
digest-changed set, and reported split because a `changed` column dominated by
`vid`-only changes would make a zero weak evidence (§8.7, D-M4g).

### The denominator floor, scored

Committed in section 1 before the run; achieved below. Exit criterion:
{verdict}.

| requirement | floor | achieved | met |
|---|---|---|---|
{rows}

### The D6.2 cross-tabulation

Over the {len(live(all_trials))} trials that injected and recomputed
(`{s['not_injected']}` cells generated an op the write path refused and are
therefore **not trials**; `{s['refused_or_errored']}` refused or errored on
recompute and are reported here, never folded into either metric):

| | recompute changed | recompute unchanged |
|---|---|---|
| `V = FRESH` | **{len(ff)}** — must be 0 | {len([t for t in live(all_trials) if t['verdict'] == 'fresh' and not t['changed']])} |
| `V = POSSIBLY_STALE` | {len([t for t in live(all_trials) if t['verdict'] == 'possibly-stale' and t['changed']])} | {len([t for t in live(all_trials) if t['verdict'] == 'possibly-stale' and not t['changed']])} |
| `V = UNDECIDABLE` | {len([t for t in live(all_trials) if t['verdict'] == 'undecidable' and t['changed']])} | {len([t for t in live(all_trials) if t['verdict'] == 'undecidable' and not t['changed']])} |

`UNDECIDABLE` is folded into `POSSIBLY_STALE` for the metrics and reported as
its own row (D13.25, D6.2): it is not a third contract, only a diagnosis that
would otherwise be lost inside a conservative verdict.
"""


def precision_section(full: dict) -> str:
    trials = live(full["trials"])
    pool = precision_pool(trials)
    overall, hits, total = precision(pool)

    by_op = []
    for op in MEASURED_OPS:
        p, h, n = precision([t for t in pool if t["op"] == op])
        if n:
            by_op.append(f"| `{op}` | {_pct(p)} | {h} / {n} |")

    # (1) by matched_on component
    conjuncts = ["kinds", "targets.nodes", "targets.edges", "targets.incident",
                 "rel_types", "vt", "props"]
    by_conj = []
    for c in conjuncts:
        p, h, n = precision([t for t in pool if c in t["matched_on"]])
        if n:
            by_conj.append(f"| `{c}` | {_pct(p)} | {h} / {n} |")
    vacuous = [t for t in pool if not t["matched_on"] and t["verdict"] != "undecidable"]
    pv, hv, nv = precision(vacuous)
    if nv:
        by_conj.append(f"| *(nothing narrowed — every conjunct vacuous)* | "
                       f"{_pct(pv)} | {hv} / {nv} |")

    # (2) by footprint arm — THE decision number
    value_only = [t for t in pool if t["arms"] == ["value"]]
    carve_only = [t for t in pool if t["arms"] == ["carve"]]
    both = [t for t in pool if sorted(t["arms"]) == ["carve", "value"]]
    pvo, hvo, nvo = precision(value_only)
    pco, hco, nco = precision(carve_only)
    pb, hb, nb = precision(both)
    carve_share = nco / total if total else 0.0

    # (3) by op kind
    by_kind = []
    for kind in ("ingest_events", "assert_node", "assert_edge", "correct",
                 "retract"):
        p, h, n = precision([t for t in pool if kind in t["kinds"]])
        if n:
            by_kind.append(f"| `{kind}` | {_pct(p)} | {h} / {n} |")

    # (4) the @version-only line
    digest_only = [t for t in pool if t["digest_only_changed"]]

    return f"""
---

## 4. Invalidation precision

**Reported, not passed** — v1 has no precision target, and D1.13 permits every
false invalidation. The denominator is the `POSSIBLY_STALE` row excluding the
refused/errored cell, over the two substrates that can carry an honest ratio.

> **overall precision = {_pct(overall)}** ({hits} true stale / {total}
> `POSSIBLY_STALE`), on `bitcoinotc` and `collegemsg`.

`stores/ldbc-fixture` is excluded from this and every ratio below: on a
22-entity fixture "every correction hits something the query read" is nearly
always true, so precision would read ≈ 1.0 and would mean nothing (§4.2). It
remains in the soundness column, where store size does not matter.
`resolve_entities` is excluded by §13.8.1's ruling. `compute` is a control.

### By operator

| operator | precision | true stale / stale |
|---|---|---|
{chr(10).join(by_op)}

### (1) By `matched_on` — which scope component fired

`matched_on` names only conjuncts that were **concrete on both sides**
(erratum E-3): a conjunct that passed because either side was `"*"` is not
attribution, it is the absence of a narrowing.

| conjunct | precision | true stale / stale |
|---|---|---|
{chr(10).join(by_conj)}

### (2) By footprint arm — **the decision number**

D13.27: *"the carve-arm line measures FF-1's precision cost directly."*
§13.10's decision rule: **if this line dominates, Level-1 carve-extent logging
is M5's highest-value dependency-research item; if it does not, the widening
was cheap and M5 should spend its effort elsewhere.**

| arm(s) that fired | precision | true stale / stale | share of all invalidations |
|---|---|---|---|
| `value` only | {_pct(pvo)} | {hvo} / {nvo} | {nvo / total:.1%} |
| **`carve` only** | {_pct(pco)} | {hco} / {nco} | **{carve_share:.1%}** |
| both | {_pct(pb)} | {hb} / {nb} | {nb / total:.1%} |

### (3) By op `kind` — append versus supersede

The axis `class` cannot carry (CO-3): A-vs-B is decided by
`believed_node_versions`, which is store state a log record does not hold. So
the disaggregation is by `kind` and `arm`, which *are* log-derivable.

| kind | precision | true stale / stale |
|---|---|---|
{chr(10).join(by_kind)}

### (4) The `@version`-only line (§13.8.2)

**{len(digest_only)} of {total}** `POSSIBLY_STALE` trials are
`digest-only-changed`: the recomputed answer's every value is identical and
only version identity moved. Under D1.8 that *is* a change, so these count as
true positives above — but §13.8.2 requires them called out, because **that is
a schema artifact and must not be read as a domain-design failure**. On the
legacy operators `vid` is frozen in the output (D8.5), so a Class B/C/D
correction changes the digest of a result nobody would call different.
"""


def forecast_section(full: dict) -> str:
    trials = live(full["trials"])
    pool = precision_pool(trials)
    cell = _carve_cell(pool)

    rows = []
    for fid, label, members, lo, hi, spelled in FORECAST:
        p, h, n = precision([t for t in cell if t["op"] in members])
        if n == 0:
            rows.append(f"| {fid} | {label} | {spelled} | *no trials in cell* | — |")
            continue
        if lo is None and hi is None:
            verdict = "*qualitative — see below*"
        elif hi is None:
            verdict = "**held**" if p >= lo else f"**MISSED** (< {lo})"
        else:
            verdict = "**held**" if lo <= p <= hi else (
                f"**MISSED** (outside {lo}–{hi})")
        rows.append(f"| {fid} | {label} | {spelled} | **{_pct(p)}** "
                    f"({h}/{n}) | {verdict} |")

    # F4 — the RG-1 pair, the cleanest A/B in the matrix
    plain, hp, npl = precision([t for t in cell if t["cell"] == RG1_PLAIN])
    dur, hd, nd = precision([t for t in cell if t["cell"] == RG1_DURATION])
    if plain and dur and dur > 0:
        ratio = plain / dur
        f4 = (f"**{_pct(plain)}** ({hp}/{npl}) without `duration` versus "
              f"**{_pct(dur)}** ({hd}/{nd}) with it — a factor of "
              f"**{ratio:.2f}×**. Predicted: ≥ 2× worse with `duration`. "
              f"**{'held' if ratio >= 2 else 'MISSED'}**.")
    else:
        f4 = (f"without `duration`: {_pct(plain)} ({hp}/{npl}); with: "
              f"{_pct(dur)} ({hd}/{nd}). Not scoreable as a ratio.")

    # F5's falsifiable half
    tr, _h1, n1 = precision([t for t in cell if t["op"] == "temporal_reachability"])
    tp, _h2, n2 = precision([t for t in cell if t["op"] == "temporal_paths"])
    f5 = (f"`temporal_reachability` {_pct(tr)} ({n1} trials) versus "
          f"`temporal_paths` {_pct(tp)} ({n2}). Predicted: the first is better. "
          + ("**held**." if (tr is not None and tp is not None and tr > tp)
             else "**MISSED**." if (tr is not None and tp is not None)
             else "not scoreable."))

    return f"""
---

## 5. The pre-registered forecast, scored per cell

Section 2's table, against the **Class B/C/D × outside-window** cell it
predicts — {len(cell)} trials on the two precision substrates.

| # | class | predicted | measured | verdict |
|---|---|---|---|---|
{chr(10).join(rows)}

**F4 — the RG-1 pair.** Identical operator, identical window; one of them is
carve-reachable and loses `V`. {f4}

**F5's falsifiable half.** {f5}
"""


def controls_section(full: dict, fixture: dict | None) -> str:
    all_live = live(full["trials"] + ((fixture or {}).get("trials") or []))
    changed = [t for t in all_live if t["changed"]]
    rt_ff = [t for t in changed if t["rowtouch_verdict"] == "fresh"]
    rt_new = [t for t in changed if t["placement"] == "new-identity"]
    rt_new_ff = [t for t in rt_new if t["rowtouch_verdict"] == "fresh"]
    ours_ff = [t for t in changed if t["verdict"] == "fresh"]

    pool = precision_pool(live(full["trials"]))
    rt_pool = [t for t in live(full["trials"])
               if t["rowtouch_verdict"] == "possibly-stale"
               and t["store"] in PRECISION_STORES]
    rt_p, rt_h, rt_n = precision(rt_pool)
    top_pool = [t for t in live(full["trials"]) if t["top_verdict"] != "fresh"
                and t["store"] in PRECISION_STORES]
    top_p, top_h, top_n = precision(top_pool)
    ours_p, _oh, _on = precision(pool)
    regressions = [t for t in pool if t["top_verdict"] == "fresh"]

    rate = len(rt_ff) / len(changed) if changed else 0.0
    return f"""
---

## 6. The two required controls

### Control 1 — the naive row-touch rule (D6.4, **required**)

*"Did the correction touch a row that appears in the stored result?"* — the
same workload, scored that way. §3's six counterexamples predict a non-zero
false-fresh rate; §13.6 predicts it fails on a two-op batch over a five-node
store. **Publishing this number is what turns memo §15 from an assertion into
a measurement.**

> **row-touch false-fresh = {len(rt_ff)} of {len(changed)} changed trials
> ({rate:.1%}).** The dependency-scope mechanism's own count on the same
> trials is **{len(ours_ff)}**.

Of the {len(rt_new)} changed trials in the **new-identity** placement — a
correction on an identity the stored result has no row for — the row-touch rule
called **{len(rt_new_ff)}** fresh. That is CE-1/CE-2/CE-3 as a measurement:
there are no rows to touch, so a positive-evidence rule sees nothing at all.

Row-touch precision, where it does fire: **{_pct(rt_p)}** ({rt_h}/{rt_n}). It
buys its precision with unsoundness, which is why the two numbers must be read
together and never separately.

### Control 2 — the all-`"*"` scope

Every operator forced to `TOP_TERM`. Sound by construction; its precision is
the floor, and the distance between it and the real derivations is what the
three Level-0 derivations bought.

> **all-`"*"` precision = {_pct(top_p)}** ({top_h}/{top_n}) versus the real
> derivations' **{_pct(ours_p)}**.

{len(regressions)} trials had the all-`"*"` control return `FRESH` where a real
derivation did not — it must be **0**, since `"*"` matches everything a
narrower term matches and more.
"""


def overhead_section(full: dict) -> str:
    trials = live(full["trials"])
    by_op: dict[str, list[int]] = {}
    for t in trials:
        by_op.setdefault(t["op"], []).append(t["scope_bytes"])
    rows = []
    for op in sorted(by_op):
        vals = sorted(by_op[op])
        payloads = [t["payload_bytes"] for t in trials if t["op"] == op]
        terms = {t["terms"] for t in trials if t["op"] == op}
        rows.append(
            f"| `{op}` | {sorted(terms)} | {vals[len(vals) // 2]} | "
            f"{vals[int(len(vals) * 0.95)]} | {vals[-1]} | "
            f"{statistics.median(payloads):.0f} |")

    lat = []
    for label, o in (full.get("overhead") or {}).items():
        if not o:
            continue
        lat.append(f"| `{label}` | {o['log_batches']} | {o['log_bytes']:,} | "
                   f"{o['median_ms_uncached']} | {o['median_ms_cached']} | "
                   f"{o['speedup']}× |")

    return f"""
---

## 7. Overhead, three numbers

### Storage — scope bytes per result

`len(scope.canonical())`, against the payload it rides beside.

| operator | terms | p50 | p95 | max | median payload |
|---|---|---|---|---|---|
{chr(10).join(rows)}

### Latency — and **D13.26's cost claim, corrected**

Erratum **E-2** restates the frozen claim, which was false as specified:

> A freshness check costs `O(prefix)` to verify the checkpoints plus
> `O(suffix)` to scan the corrections. Only the second term is proportional to
> *corrections since the read*; the first is proportional to the log up to the
> read and is the price of the tamper-evidence D13.18 buys.

`chain_of_prefix(offset)` folds the rolling hash from **byte 0**, and D13.24
step 6 requires it for every checkpoint. The chain cache makes repeated checks
against one log `O(suffix)` after the first — and it is an implementation
convenience, so both numbers are given rather than the flattering one.

| store | log batches | log bytes | median ms, **no cache** | median ms, **cached** | speedup |
|---|---|---|---|---|---|
{chr(10).join(lat)}

Step 5's cursor invariant is verified **inside** step 6's walk, so it costs
nothing extra and does not appear as a separate line (E-2's own instruction).

### Write path — **zero**

M4 adds nothing to the write path. Not "negligible": zero. Nothing is written
when an answer is produced, nothing is written when one is checked, and no
index is maintained. That is the honest contrast with Level 3, where the write
path starts paying, and with §13.10's Level-1 carve-extent recovery, which is a
write-path *format* change costing amplification proportional to superseded
versions.
"""


def population_section(full: dict, fixture: dict | None) -> str:
    trials = full["trials"] + ((fixture or {}).get("trials") or [])
    by_store = Counter(t["store"] for t in trials)
    by_class = Counter(t["cls"] for t in live(trials))
    by_placement = Counter(t["placement"] for t in live(trials))
    by_gen = Counter(t["generator"] for t in live(trials))
    stores = "\n".join(
        f"| `{s['label']}` | {s['backend']} | {s['cells']} | "
        f"{'**precision + soundness**' if s['precision_tier'] else 'soundness only'} |"
        for s in full["stores"] + ((fixture or {}).get("stores") or [])
        if s["label"] not in {x["label"] for x in full["stores"]}
        or s in full["stores"])
    return f"""
---

## 8. The population, and what was excluded

| substrate | backend | `(Q, A)` cells | tier |
|---|---|---|---|
{stores}

| | |
|---|---|
| trials, all substrates | {len(trials)} |
| by substrate | {dict(by_store)} |
| effect classes injected | {dict(by_class)} |
| placements | {dict(by_placement)} |
| generators | {dict(by_gen)} |

**Named exclusions**, per `EVIDENCE_MODEL.md` §7 — a filtered denominator is
named with its reason in every table it is absent from, never silently:

- **`resolve_entities`** — {EXCLUDED_OPS['resolve_entities']}
- **`compute`** — the ∅ control (D5.3), carried to show that ∅ ⇒ `FRESH`
  forever, and in no precision denominator. Measured operators:
  {len(MEASURED_OPS)} of the fifteen.
- **`stores/ldbc-fixture`** — soundness only. A 22-entity, 57-edge fixture
  establishes that a plan compiles, loads, admits and executes; it establishes
  **nothing about precision**, because on it "every correction hits something
  the query read" is nearly always true.

**Isolation.** Every trial injects into a *copy* of the substrate. §8.6 is why:
if trial *n*'s correction survived into *n+1*, the recorded `tt_q` and the log
would no longer correspond and every classification after it would be suspect —
in both directions. The cheaper undo-after-measure alternative is rejected
outright, because an undo is itself a Class B/C/D op and the next trial's
suffix would contain it.

**Replayability.** Every trial carries
`(store_digest_before, scope_digest, injected_batch_id, verdict, changed)` and
is reconstructible from those five fields; the artifact embeds the git SHA,
profile, seed, machine and counts.
"""


def main() -> int:
    full = load("full")
    fixture = load("fixture")
    if full is None:
        print("no benchmarks/freshness-v1/trials-full.json — run the sweep first")
        return 1

    text = REPORT.read_text()
    head, _sep, _rest = text.partition("\n---\n\n## 3.")
    if not _sep:
        head = text.rstrip()
    body = "".join([
        head.rstrip(), "\n\n---\n",
        headline(full, fixture),
        precision_section(full),
        forecast_section(full),
        controls_section(full, fixture),
        overhead_section(full),
        population_section(full, fixture),
        f"""
---

## 9. Provenance

| | |
|---|---|
| forecast frozen | 2026-08-22, before the first headline trial |
| harness | `scripts/bench_freshness.py` |
| generators | `tgms/eval/corrections.py` |
| records | `benchmarks/freshness-v1/trials-full.json`, `trials-fixture.json` |
| git SHA at run | `{full.get('git_sha', '')[:12]}` |
| seed | {full.get('seed')} |
| wall | {full.get('wall_s')} s, {full.get('trial_count')} trials |
| machine | {full.get('machine', {}).get('platform', '')} |
| this section generated by | `scripts/gen_freshness_report.py` |

Every number above is derived from the records named here. Sections 1 and 2
predate them.
""",
    ])
    REPORT.write_text(body)
    s = full["summary"]
    print(f"wrote {REPORT.relative_to(ROOT)}")
    print(f"false-fresh {s['false_fresh']}; changed {s['changed']} "
          f"(value {s['value_changed']}); precision "
          f"{_pct(s['precision'])}; floor met {s['floor']['all_met']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
