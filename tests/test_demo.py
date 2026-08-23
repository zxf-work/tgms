"""`tgms demo` is the first thing a stranger runs, so it is the one command
whose *output* is the contract.

The demo exists to make one claim visible in under a minute: the same question
asked of two belief states returns two different answers, and the answer that
was returned can be re-derived from a stored trace. This file asserts exactly
that claim — both belief states appear, they differ, and a content-addressed
digest ties the narrated answers to the executed plan — rather than pinning
the prose, which is meant to be edited.

It also runs the real thing end to end: a real store on disk, the real write
API, the real `ToolRouter` dispatch, the real executor and result store. A
demo that had drifted out of step with the engine would fail here first.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from tgms.demo import SUBJECT, run_demo

DIGEST16 = re.compile(r"\b[0-9a-f]{16}\b")


def test_the_demo_narrates_six_beats_and_shows_both_belief_states(tmp_path, capsys):
    """The story, as a reader sees it on the terminal."""
    from tgms.cli import main

    assert main(["demo", "--store", str(tmp_path / "demo")]) == 0
    out = capsys.readouterr().out

    for beat in range(1, 7):
        assert f"[{beat}/6]" in out, f"beat {beat} is missing from the narration"

    # the two belief states, side by side, with the row that differs marked
    assert "believed BEFORE correction" in out and "believed NOW" in out
    assert "status=cleared" in out and "status=sanctioned" in out
    assert "<-- differs" in out

    # the evidence: a trace whose steps carry the digests the answers were
    # printed with, so the narration and the executed plan are the same run
    assert "result_digest" in out
    assert "entity_history" in out and "version_history" in out
    digests = DIGEST16.findall(out)
    assert any(digests.count(d) >= 2 for d in digests), (
        "no digest appears both as a narrated answer and as a trace step")


def test_the_two_belief_states_disagree_and_the_disagreement_is_recorded(tmp_path):
    """The claim itself, checked on structure rather than on prose."""
    receipts = run_demo(tmp_path / "demo")

    now, then = receipts["rows_now"], receipts["rows_then"]
    assert now and then and now != then
    assert receipts["digest_now"] != receipts["digest_then"]
    # belief-time travel, not valid-time travel: the past belief state knows
    # nothing of the correction, so it holds strictly fewer versions
    assert len(then) < len(now)
    assert {r["props"]["status"] for r in then} == {"cleared"}
    assert {r["props"]["status"] for r in now} == {"cleared", "sanctioned"}
    assert receipts["differing_periods"], "the two answers agree everywhere"
    assert receipts["corrections"] == 1
    assert receipts["tt_before"] < receipts["tt_after"]

    trace = receipts["trace"]
    assert trace["ok"] and len(trace["steps"]) == 3
    assert {s["result_digest"] for s in trace["steps"]} >= {
        receipts["digest_now"], receipts["digest_then"]}


def test_the_demo_leaves_a_renderable_record_and_its_step_results(tmp_path):
    """Beat 5 reuses `tgms trace render`'s machinery; the artifacts it leaves
    must be the shape that command consumes, and every step's full result must
    be on disk under its own hash."""
    root = tmp_path / "demo"
    receipts = run_demo(root)

    record = json.loads((root / "demo-record.json").read_text())
    assert record["plan"]["plan_id"] == receipts["plan_id"]
    assert record["trace"]["ok"] and record["question"] and record["receipts"]

    from tgms.tools.trace_viewer import render_trace_html
    html = render_trace_html(record)  # the exact call `tgms trace render` makes
    assert receipts["digest_now"][:16] in html
    assert (root / "demo-trace.html").read_text() == html

    for step in receipts["trace"]["steps"]:
        blob = json.loads(
            (root / "demo-results" / f"{step['result_digest']}.json").read_text())
        assert blob["result_digest"] == step["result_digest"]


def test_the_demo_refuses_to_build_on_top_of_an_existing_store(tmp_path):
    """Running it twice into one directory would ingest the story twice and
    narrate a second correction that the script never mentions."""
    import pytest

    root = tmp_path / "demo"
    run_demo(root)
    with pytest.raises(SystemExit) as excinfo:
        run_demo(root)
    assert excinfo.value.code == 2


def test_beat_six_invalidates_the_saved_answer_and_certifies_the_re_run(
        tmp_path, capsys):
    """The beat the rest of the demo exists to earn.

    A reader who never thinks to ask "is this still true?" is told anyway: the
    answer filed before the correction comes back `POSSIBLY_STALE` with the
    write that invalidated it **named**, and the same question re-run after the
    correction comes back `FRESH`. The contrast is the product claim, so both
    halves are asserted, in order.
    """
    from tgms.cli import main

    assert main(["demo", "--store", str(tmp_path / "demo")]) == 0
    out = capsys.readouterr().out

    beat6 = out.split("[6/6]", 1)
    assert len(beat6) == 2, "the freshness beat is missing"
    beat6 = beat6[1]

    stale_at = beat6.find("POSSIBLY_STALE")
    fresh_at = beat6.find("FRESH")
    assert stale_at != -1, "the pre-correction answer was not invalidated"
    assert fresh_at != -1, "the re-run answer was not certified fresh"
    assert stale_at < fresh_at, (
        "the beat must land stale-then-fresh; the contrast is the point")
    assert "UNDECIDABLE" not in beat6, (
        "the demo store is anchored and unrewritten; UNDECIDABLE here means "
        "the check could not read what it should have")

    # the memo sentence, on its own terms: produced-when, what changed, and
    # the instruction to reconsider rather than a repaired answer
    assert "This answer was produced on" in beat6
    assert "corrected node vendor-orion" in beat6
    assert "Reconsider." in beat6
    # and the correction's valid-time reach, which is what makes it actionable
    assert "[2020-01-04 .. open)" in beat6

    # it goes through the real verb, not a bespoke path
    assert "tgms trace check" in beat6


def test_beat_six_verdicts_and_witness_are_real_not_narrated(tmp_path):
    """The claim checked on structure rather than prose: the verdicts come back
    from `Store.check_trace` — the call `tgms trace check` makes — and the
    witness names the actual logged correction."""
    receipts = run_demo(tmp_path / "demo")

    assert receipts["verdict_before"] == "POSSIBLY_STALE"
    assert receipts["verdict_after"] == "FRESH"

    assert receipts["witnesses"], "POSSIBLY_STALE with no witness (D1.14)"
    w = receipts["witnesses"][0]
    assert w["kind"] == "correct"
    assert w["identity"] == {"uid": SUBJECT}
    assert w["arm"] == "value"
    assert w["matched_on"], "the witness attributes the match to no conjunct"
    # the correction's own transaction time, not a paraphrase of it
    assert w["tt"] == receipts["tt_after"]

    # the saved record is the shape `tgms trace check` consumes, and the
    # verdict is reproducible from it by the same public API
    import tgms
    record = json.loads(Path(receipts["before_record_path"]).read_text())
    store = tgms.open(receipts["store"], read_only=True)
    try:
        again = store.check_trace(record["trace"])
        assert not again.actionable_fresh
        assert [x.to_json() for x in again.witnesses] == receipts["witnesses"]
    finally:
        store.close()


def test_beat_six_costs_almost_nothing(tmp_path):
    """Freshness is a scan of the writes since the read, not a recomputation.
    On this store that is a fraction of a millisecond, and the beat must not be
    what makes the demo feel slow."""
    import time as _time

    import tgms
    receipts = run_demo(tmp_path / "demo")
    record = json.loads(Path(receipts["before_record_path"]).read_text())
    store = tgms.open(receipts["store"], read_only=True)
    try:
        t0 = _time.perf_counter()
        for _ in range(20):
            store.check_trace(record["trace"])
        per_call_ms = (_time.perf_counter() - t0) / 20 * 1000
    finally:
        store.close()
    assert per_call_ms < 50.0, f"check_trace took {per_call_ms:.1f} ms"


def test_the_demo_is_fast_enough_to_watch(tmp_path):
    """A ceiling, not a benchmark: the adoption claim is 'under a minute from
    install to a temporal result', and the run itself must not be what eats
    it. On a laptop this is ~0.2 s."""
    receipts = run_demo(tmp_path / "demo")
    assert receipts["elapsed_s"] < 15.0
    assert receipts["rows_now"][0]["uid"] == SUBJECT
