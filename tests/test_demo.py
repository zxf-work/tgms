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

from tgms.demo import SUBJECT, run_demo

DIGEST16 = re.compile(r"\b[0-9a-f]{16}\b")


def test_the_demo_narrates_five_beats_and_shows_both_belief_states(tmp_path, capsys):
    """The story, as a reader sees it on the terminal."""
    from tgms.cli import main

    assert main(["demo", "--store", str(tmp_path / "demo")]) == 0
    out = capsys.readouterr().out

    for beat in range(1, 6):
        assert f"[{beat}/5]" in out, f"beat {beat} is missing from the narration"

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


def test_the_demo_is_fast_enough_to_watch(tmp_path):
    """A ceiling, not a benchmark: the adoption claim is 'under a minute from
    install to a temporal result', and the run itself must not be what eats
    it. On a laptop this is ~0.2 s."""
    receipts = run_demo(tmp_path / "demo")
    assert receipts["elapsed_s"] < 15.0
    assert receipts["rows_now"][0]["uid"] == SUBJECT
