"""[tests] Structured JSON logging (Lane B5/F2): every tool call logs a
`"started"`/`"finished"` pair as parseable JSON lines carrying a
`request_id` that matches the one riding in the successful envelope's
`annotations` channel — the correlation `scripts/tgms_supervise.py` depends
on to find a hung request. Off by default in library use; on when a caller
(`tgms serve`, `tgms webapp`) opts in.
"""

from __future__ import annotations

import json
import logging

import pytest

import tgms
from tgms.tools.jsonlog import CallLogger, JsonFormatter, configure_logging, new_request_id
from tgms.tools.server import ToolRouter

pytest.importorskip("tgms._engine", reason="native engine extension not built")


def _seeded_store(tmp_path):
    store = tgms.open(tmp_path / "s", backend="native")
    store.assert_node("n0", "N")
    return store


def test_new_request_id_is_a_bare_hex_token():
    rid = new_request_id()
    assert isinstance(rid, str) and len(rid) == 32
    int(rid, 16)  # must be plain hex, no dashes -- a safe bare token


def test_call_logger_disabled_by_default_emits_nothing(caplog):
    logger = logging.getLogger("tgms.tools.test.disabled")
    logger.addHandler(logging.NullHandler())
    call_logger = CallLogger(logger)  # enabled=False default
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        call_logger.log_start(request_id="r1", tool="entity_history")
        call_logger.log_finish(request_id="r1", tool="entity_history",
                               store_identity=None, generation=None,
                               wall_ms=1.0, outcome="ok")
    assert caplog.records == []


def test_json_formatter_round_trips_structured_fields():
    logger = logging.getLogger("tgms.tools.test.formatter")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    lines: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    handler = Capture()
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)

    call_logger = CallLogger(logger, enabled=True)
    call_logger.log_start(request_id="req-1", tool="entity_history")
    call_logger.log_finish(request_id="req-1", tool="entity_history",
                           store_identity="store-abc", generation=3,
                           wall_ms=12.5, outcome="ok", refusal_stage=None)

    assert len(lines) == 2
    started = json.loads(lines[0])
    finished = json.loads(lines[1])
    assert started["event"] == "started"
    assert started["request_id"] == "req-1"
    assert started["tool"] == "entity_history"
    # fields that don't apply to "started" are omitted, not null
    assert "wall_ms" not in started and "outcome" not in started

    assert finished["event"] == "finished"
    assert finished["request_id"] == "req-1"
    assert finished["store_identity"] == "store-abc"
    assert finished["generation"] == 3
    assert finished["wall_ms"] == 12.5
    assert finished["outcome"] == "ok"
    assert "ts" in started and "ts" in finished
    assert "level" in started and "level" in finished


def test_configure_logging_is_idempotent(tmp_path):
    path = tmp_path / "log.jsonl"
    logger_name = "tgms.tools.test.idempotent"
    logger1 = configure_logging(path, logger_name=logger_name)
    logger2 = configure_logging(path, logger_name=logger_name)
    assert logger1 is logger2
    assert len(logger1.handlers) == 1


def test_configure_logging_writes_parseable_jsonl(tmp_path):
    path = tmp_path / "log.jsonl"
    logger = configure_logging(path, logger_name="tgms.tools.test.writes")
    call_logger = CallLogger(logger, enabled=True)
    call_logger.log_start(request_id="abc", tool="op")
    call_logger.log_finish(request_id="abc", tool="op", store_identity=None,
                           generation=None, wall_ms=1.23, outcome="ok")
    for h in logger.handlers:
        h.flush()
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)  # must parse
        assert rec["request_id"] == "abc"


def test_tool_router_call_logs_matching_request_id_and_envelope_annotation(
        tmp_path):
    store = _seeded_store(tmp_path)
    logger = configure_logging(
        tmp_path / "server.jsonl", logger_name="tgms.tools.test.router")
    call_logger = CallLogger(logger, enabled=True)
    router = ToolRouter(store.adapter, tt_source=store, call_logger=call_logger)

    env = router.call("entity_history", {"uid": "n0", "limit": 5})
    assert "error" not in env
    request_id = env["annotations"]["request_id"]

    for h in logger.handlers:
        h.flush()
    lines = [json.loads(line) for line in
            (tmp_path / "server.jsonl").read_text().splitlines()]
    started = [r for r in lines if r.get("event") == "started"]
    finished = [r for r in lines if r.get("event") == "finished"]
    assert any(r["request_id"] == request_id for r in started)
    match = [r for r in finished if r["request_id"] == request_id]
    assert len(match) == 1
    assert match[0]["outcome"] == "ok"
    assert match[0]["tool"] == "entity_history"
    assert isinstance(match[0]["wall_ms"], (int, float))


def test_tool_router_call_logger_disabled_by_default_and_still_annotates(
        tmp_path):
    """A bare `ToolRouter(adapter)` logs nothing (library default) but still
    stamps `request_id` on the envelope -- logging and the annotation are
    independent."""
    store = _seeded_store(tmp_path)
    router = ToolRouter(store.adapter, tt_source=store)
    assert router.call_logger.enabled is False
    env = router.call("entity_history", {"uid": "n0", "limit": 5})
    assert "request_id" in env["annotations"]


def test_digest_is_unaffected_by_request_id_annotation(tmp_path):
    """The annotation rides outside `result_digest` -- calling the same op
    twice yields the same digest even though each call gets a fresh
    request_id."""
    store = _seeded_store(tmp_path)
    router = ToolRouter(store.adapter, tt_source=store)
    a = router.call("entity_history", {"uid": "n0", "limit": 5})
    b = router.call("entity_history", {"uid": "n0", "limit": 5})
    assert a["annotations"]["request_id"] != b["annotations"]["request_id"]
    assert a["result_digest"] == b["result_digest"]
