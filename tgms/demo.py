"""`tgms demo` — the sixty-second argument for a bi-temporal store.

A stranger who has just run `pip install tgms` types `tgms demo` and watches
one story play out: a small payments graph is built, a **correction** arrives
(a retroactive fix to what was believed, not a change in the world), and the
same question is then asked twice — once of today's beliefs and once of the
belief state that existed before the correction landed. The two answers
differ, which is the entire point of keeping transaction time.

Deliberate constraints:

- **No simulation.** Every line of output comes from the public code paths —
  `tgms.open`, `Store.assert_node` / `ingest_events` / `correct`, `ToolRouter`
  (what `tgms call` and the MCP server dispatch through), and the plan
  `Executor` with its content-addressed `ResultStore` (what `tgms ask` runs).
  A demo that faked its output would prove nothing.
- **No dependencies at run time.** No GPU, no model, no API key, no network,
  no dataset download; plain ASCII so it survives any terminal.
- **Small enough to be instant.** Four entities and eight events: the whole
  run is dominated by process start-up, not by TGMS.

The narration is a fixed five-beat arc — build, correct, ask now, ask then,
show the evidence — and not a tour of the operator set. Two operators appear,
because two are what the story needs.
"""

from __future__ import annotations

import datetime as _dt
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, TextIO

import tgms
from tgms.core.model import OPEN_END, EntityRef

#: Valid-time origin of the toy world: 2020-01-01T00:00:00Z, in microseconds.
T0 = 1_577_836_800_000_000
DAY = 86_400_000_000

#: The subject of the story. One vendor, three accounts paying it.
SUBJECT = "vendor-orion"
ACCOUNTS = ("acct-lyra", "acct-mira", "acct-vela")

#: Day offsets of the payment events (day, payer).
PAYMENTS: tuple[tuple[int, str], ...] = (
    (0, "acct-lyra"), (0, "acct-mira"), (1, "acct-lyra"), (2, "acct-vela"),
    (3, "acct-mira"), (4, "acct-lyra"), (5, "acct-vela"), (6, "acct-mira"),
)

#: The world fact the audit later revises, and the valid-time instant from
#: which the revision applies. The sanction was in force from day 3; nobody
#: knew until the audit.
SANCTION_DAY = 3

#: Props compared side by side in beat 4. Two keys keep the table narrow.
SHOWN_PROPS = ("status", "risk")

WIDTH = 78


# --------------------------------------------------------------------------- #
# formatting                                                                   #
# --------------------------------------------------------------------------- #

def _vt(us: int) -> str:
    """Valid time as a plain date; the open end is named, not printed as 2**62."""
    if us >= OPEN_END:
        return "open"
    return _dt.datetime.fromtimestamp(us / 1e6, _dt.timezone.utc).strftime("%Y-%m-%d")


def _tt(us: int) -> str:
    """Transaction time to the microsecond — belief states are addressed by it."""
    if us >= OPEN_END:
        return "now"
    return _dt.datetime.fromtimestamp(us / 1e6, _dt.timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def _props(row: dict[str, Any] | None) -> str:
    if row is None:
        return "(no version believed)"
    p = row.get("props") or {}
    return ", ".join(f"{k}={p.get(k, '-')}" for k in SHOWN_PROPS)


class _Narrator:
    """The five-beat script. Every beat says what is asked, what is called,
    and what came back — in that order, so the output reads as a story."""

    def __init__(self, out: TextIO, total: int = 5) -> None:
        self.out = out
        self.total = total
        self.beat = 0

    def head(self, title: str) -> None:
        self.beat += 1
        self.line("")
        self.line(f"[{self.beat}/{self.total}] {title}")

    def say(self, text: str) -> None:
        self.line(f"      {text}")

    def ask(self, text: str) -> None:
        self.line(f"  ask   {text}")

    def call(self, text: str) -> None:
        self.line(f"  call  {text}")

    def answer(self, text: str) -> None:
        self.line(f"  ->    {text}")

    def cont(self, text: str) -> None:
        self.line(f"        {text}")

    def line(self, text: str = "") -> None:
        print(text, file=self.out)

    def rule(self) -> None:
        self.line("-" * WIDTH)


# --------------------------------------------------------------------------- #
# the demo                                                                     #
# --------------------------------------------------------------------------- #

def run_demo(store_path: str | Path | None = None,
             out: TextIO | None = None) -> dict[str, Any]:
    """Run the scripted arc; return the receipts a test (or a caller) can check.

    `store_path` is optional by contract: with nothing given the demo makes its
    own directory under the system temp and leaves it in place, because the
    rendered trace from the last beat is a file the reader is invited to open.
    """
    out = out or sys.stdout
    n = _Narrator(out)
    t_start = time.perf_counter()

    if store_path is None:
        root = Path(tempfile.mkdtemp(prefix="tgms-demo-"))
        owned = True
    else:
        root = Path(store_path)
        owned = False
        if (root / "eventlog.jsonl").exists():
            print(f"tgms demo: {root} already holds a store, and the demo must "
                  f"build its own from nothing to keep the story exact.\n"
                  f"Pass a fresh --store path, or remove that directory.",
                  file=out)
            raise SystemExit(2)

    n.rule()
    n.line("TGMS demo -- what did we believe, and when did we believe it?")
    n.rule()
    n.say(f"store   $STORE = {root}")
    n.say(f"engine  native (tgms {tgms.__version__}) -- no network, no model, "
          f"no GPU, no download")

    store = tgms.open(root)
    try:
        return _arc(store, root, n, t_start, owned)
    finally:
        store.close()


def _arc(store: Any, root: Path, n: _Narrator, t_start: float,
         owned: bool) -> dict[str, Any]:
    from tgms.tools.server import ToolRouter

    # -- beat 1: a tiny bi-temporal graph ---------------------------------- #

    n.head("A tiny bi-temporal graph")
    n.say("Every fact TGMS stores carries two clocks: VALID time (when it was")
    n.say("true in the world) and TRANSACTION time (when TGMS came to believe")
    n.say("it). Keeping both is what makes the rest of this demo possible.")
    n.call("store.assert_node(...) x4  +  store.ingest_events(8 events)")

    store.assert_node(SUBJECT, "Vendor",
                      {"status": "cleared", "risk": "low"}, vt_s=T0)
    for uid in ACCOUNTS:
        store.assert_node(uid, "Account", {"kind": "operating"}, vt_s=T0)
    store.ingest_events([
        {"src": payer, "dst": SUBJECT, "rel_type": "PAYMENT",
         "vt_s": T0 + day * DAY, "props": {"amount": 1000 + 100 * i}}
        for i, (day, payer) in enumerate(PAYMENTS)])

    stats = store.stats()
    n.answer(f"{stats['n_entities']} entities, {stats['n_edge_versions']} PAYMENT "
             f"events, valid {_vt(stats['vt_min'])} .. {_vt(stats['vt_max'])}")
    n.cont(f"{SUBJECT} was recorded as status=cleared, risk=low from "
           f"{_vt(T0)} onward.")

    # -- beat 2: a correction ---------------------------------------------- #

    #: The belief state a reader of this store had one moment ago -- before
    #: the audit's finding was written. Addressing it later is the whole
    #: trick, and it costs one integer.
    tt_before = store.clock.last_tt

    n.head("A correction arrives")
    n.say("An audit finds that the vendor was under sanction from "
          f"{_vt(T0 + SANCTION_DAY * DAY)} --")
    n.say("so the graph was not made wrong by later events, it was wrong all")
    n.say("along. That is a CORRECTION, not an update: TGMS closes the old")
    n.say("belief in transaction time instead of overwriting it.")
    n.call(f"store.correct(node {SUBJECT}, "
           f"{{status: sanctioned, risk: high}}, "
           f"vt=[{_vt(T0 + SANCTION_DAY * DAY)}, open))")

    tt_after = store.correct(
        EntityRef(kind="node", uid=SUBJECT),
        {"status": "sanctioned", "risk": "high", "source": "audit-2020-01-09"},
        vt_s=T0 + SANCTION_DAY * DAY, vt_e=OPEN_END)

    n.answer(f"committed at transaction time {_tt(tt_after)}")
    n.cont(f"the belief state just before it stays addressable as tt={tt_before}")
    n.cont("not one payment event was rewritten; only the belief about the "
           "vendor was.")

    # -- beat 3: today's beliefs ------------------------------------------- #

    router = ToolRouter(store.adapter)

    n.head("Ask the graph what it believes NOW")
    n.ask(f"What is the status history of {SUBJECT}?")
    n.call(f"tgms call --store $STORE entity_history "
           f"'{{\"uid\": \"{SUBJECT}\"}}'")
    now = router.call("entity_history", {"uid": SUBJECT})
    _fail_fast(now)
    for row in now["rows"]:
        n.answer(f"valid [{_vt(row['vt_s'])} .. {_vt(row['vt_e'])})  {_props(row)}")
    n.cont(f"result_digest {now['result_digest'][:16]}")

    # -- beat 4: the belief state before the correction --------------------- #

    n.head("Ask the same question of the PAST belief state")
    n.ask(f"What did we believe about {SUBJECT} before the audit landed?")
    n.call(f"tgms call --store $STORE entity_history "
           f"'{{\"uid\": \"{SUBJECT}\", \"as_of_tt\": {tt_before}}}'")
    then = router.call("entity_history", {"uid": SUBJECT, "as_of_tt": tt_before})
    _fail_fast(then)
    for row in then["rows"]:
        n.answer(f"valid [{_vt(row['vt_s'])} .. {_vt(row['vt_e'])})  {_props(row)}")
    n.cont(f"result_digest {then['result_digest'][:16]}")

    n.line("")
    n.say("The same question, the same valid-time periods, two belief states:")
    n.line("")
    differing = _side_by_side(n, then["rows"], now["rows"])
    n.line("")
    n.say("The payments approved in that window were approved under the left")
    n.say("column. TGMS can still show it -- an ordinary store would now")
    n.say("report the right column as though it had always been true.")

    # -- beat 5: the evidence ----------------------------------------------- #

    n.head("Show the evidence")
    n.say("Both answers, plus a count of how many beliefs have been corrected,")
    n.say("as one deterministic plan through the executor `tgms ask` uses. Each")
    n.say("step's full result is stored on disk under its own content hash.")

    n.call("Executor(ToolRouter(store.adapter), ResultStore(...)).run(plan)")
    record, trace, corrections = _run_plan(store, root, tt_before)
    n.line("")
    n.cont(f"{'step':5} {'operator':17} {'rows':>5} {'wall':>9}  result_digest")
    for step in trace["steps"]:
        n.cont(f"{step['step_id']:5} {step['op']:17} "
               f"{step.get('rows_returned', '-'):>5} "
               f"{step.get('wall_ms', 0):>7.3f}ms  "
               f"{str(step.get('result_digest', ''))[:16]}")
    n.line("")
    n.answer(f"plan {record['plan']['plan_id']}  ok={trace['ok']}")
    n.cont(f"answer: {corrections} corrected belief in this store "
           f"(s3 counts superseded rows)")
    n.cont("s1 and s2 carry exactly the digests printed in steps 3 and 4:")
    n.cont("the answers are content-addressed, so an auditor re-derives them.")

    record_path = root / "demo-record.json"
    html_path = root / "demo-trace.html"
    record_path.write_text(json.dumps(record, indent=1))
    from tgms.tools.trace_viewer import render_trace_html
    html_path.write_text(render_trace_html(record))

    n.line("")
    n.say(f"record  {record_path}")
    n.say(f"trace   {html_path}   (open it in a browser)")
    n.say(f"results {root / 'demo-results'}/<digest>.json  -- one file per step")
    n.cont("re-render any saved record with: "
           "tgms trace render $STORE/demo-record.json -o trace.html")

    elapsed = time.perf_counter() - t_start
    n.line("")
    n.rule()
    n.line(f"Done in {elapsed:.2f}s. What you just saw: a correction to history "
           f"that did not")
    n.line("destroy the history it corrected, and a query language that can "
           "address both.")
    if owned:
        n.line(f"The demo store is disposable: rm -rf {root}")
        n.line("Use `tgms demo --store PATH` to build it somewhere you keep.")
    n.rule()

    return {
        "store": str(root),
        "tt_before": tt_before,
        "tt_after": tt_after,
        "rows_now": now["rows"],
        "rows_then": then["rows"],
        "digest_now": now["result_digest"],
        "digest_then": then["result_digest"],
        "differing_periods": differing,
        "corrections": corrections,
        "plan_id": record["plan"]["plan_id"],
        "trace": trace,
        "record_path": str(record_path),
        "html_path": str(html_path),
        "elapsed_s": elapsed,
    }


def _fail_fast(envelope: dict[str, Any]) -> None:
    """The demo asserts the system works; a structured error means it does
    not, and silently narrating around it would be the one unforgivable bug."""
    if "error" in envelope:
        raise RuntimeError(f"demo operator call failed: {envelope}")


def _side_by_side(n: _Narrator, then_rows: list[dict[str, Any]],
                  now_rows: list[dict[str, Any]]) -> list[list[int]]:
    """Align both belief states on a shared valid-time segmentation.

    The two answers do not share a row count -- that is the point -- so the
    columns are built over the union of their interval boundaries, and every
    segment shows what each belief state says about it.
    """
    bounds = sorted({b for r in then_rows + now_rows for b in (r["vt_s"], r["vt_e"])})
    left, right = "believed BEFORE correction", "believed NOW"
    n.cont(f"{'valid-time period':<26} {left:<28} {right}")
    n.cont(f"{'-' * 26} {'-' * 28} {'-' * 28}")
    differing: list[list[int]] = []
    for lo, hi in zip(bounds, bounds[1:]):
        a = _covering(then_rows, lo)
        b = _covering(now_rows, lo)
        mark = "" if _props(a) == _props(b) else "   <-- differs"
        if mark:
            differing.append([lo, hi])
        n.cont(f"{_vt(lo) + ' .. ' + _vt(hi):<26} {_props(a):<28} "
               f"{_props(b)}{mark}")
    return differing


def _covering(rows: list[dict[str, Any]], t: int) -> dict[str, Any] | None:
    return next((r for r in rows if r["vt_s"] <= t < r["vt_e"]), None)


def _run_plan(store: Any, root: Path,
              tt_before: int) -> tuple[dict[str, Any], dict[str, Any], int]:
    """The same three questions as one plan, run through the real executor.

    Not a parallel trace mechanism: this is `tgms ask`'s executor, its
    content-addressed `ResultStore`, and the record shape `tgms trace render`
    consumes -- only the planner (which needs a model) is replaced by a plan
    written out by hand.
    """
    from tgms.agent.executor import Executor, ResultStore
    from tgms.agent.ir import Plan
    from tgms.tools.server import ToolRouter

    question = (f"What does TGMS believe about {SUBJECT} now, what did it "
                f"believe before the audit, and how many of its beliefs have "
                f"been corrected?")
    plan = Plan.from_json({
        "plan_id": "demo-belief-vs-correction",
        "question": question,
        "steps": [
            {"id": "s1", "op": "entity_history", "args": {"uid": SUBJECT}},
            {"id": "s2", "op": "entity_history",
             "args": {"uid": SUBJECT, "as_of_tt": tt_before}},
            {"id": "s3", "op": "version_history",
             "args": {"kind": "node", "belief": "superseded",
                      "window": {"t_a": T0, "t_b": T0 + 365 * DAY}}},
        ],
        "answer_spec": {"kind": "count", "from": "s3.rows_total"},
    })
    executor = Executor(ToolRouter(store.adapter),
                        ResultStore(root / "demo-results"))
    trace = executor.run(plan)
    if not trace.ok:
        raise RuntimeError(f"demo plan did not execute cleanly: {trace.to_json()}")
    record = {
        "question": question,
        "answer": trace.answer,
        "plan": plan.to_json(),
        "trace": trace.to_json(),
        "receipts": f"tgms {tgms.__version__}; store {root}; "
                    f"produced by `tgms demo` (no model in the loop)",
    }
    return record, trace.to_json(), int(trace.answer)
