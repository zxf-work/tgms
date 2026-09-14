"""[tests] Health surfaces (Lane B5/F2): the webapp's `GET /live` / `GET
/ready`, `tgms store ready`, and the `ReadinessProbe` both sit on.

**"Ready" means, precisely** (see `tgms.tools.webapp.ReadinessProbe`): the
adapter's manifest generation is readable, and — for a backend that keeps a
replay cursor — that cursor is internally consistent with the event log
(within the log, landing on a record boundary, chain matches). A writer
that exists at all has already recovered (`Store.__init__` runs `_recover`
to completion first), so `recovery_pending` is always `False` for a writer
handle; a reader never recovers by design (D-049), so this probe is what
notices a torn/inconsistent log under a reader instead of nothing at all.
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer

import pytest
import urllib.request

import tgms
from tgms.cli import main as cli_main
from tgms.tools.webapp import DemoApp, ReadinessProbe, make_handler

pytest.importorskip("tgms._engine", reason="native engine extension not built")


def _healthy_store(tmp_path):
    store = tgms.open(tmp_path / "s", backend="native")
    store.assert_node("a", "N")
    store.assert_node("b", "N")
    return store


def test_readiness_probe_true_on_a_healthy_writer(tmp_path):
    store = _healthy_store(tmp_path)
    probe = ReadinessProbe(store)
    result = probe.check()
    assert result["ready"] is True
    assert result["recovery_pending"] is False
    assert result["read_only"] is False
    assert "generation" in result and "store_identity" in result
    store.close()


def test_readiness_probe_true_on_a_healthy_reader(tmp_path):
    store = _healthy_store(tmp_path)
    store.close()
    reader = tgms.open(tmp_path / "s", backend="native", read_only=True)
    result = ReadinessProbe(reader).check()
    assert result["ready"] is True
    assert result["read_only"] is True
    reader.close()


def test_readiness_probe_false_on_a_torn_tail(tmp_path):
    """Readers skip `_recover()` entirely (D-049) -- so a log rewritten
    short of what the manifest's cursor claims was applied (here: the last
    of two write batches silently dropped, at a clean record boundary so
    the log still *parses* fine) is invisible to the store's own open path
    under a reader, and only this probe's own (independent, read-only)
    consistency check catches it."""
    store = _healthy_store(tmp_path)  # two separate write batches: a, b
    store.close()

    log_path = tmp_path / "s" / "eventlog.jsonl"
    lines = log_path.read_bytes().split(b"\n")
    non_empty = [ln for ln in lines if ln]
    assert len(non_empty) >= 3, "expected header + 2 batch records"
    # keep the header and only the first batch -- a clean boundary, so
    # parsing still succeeds; the manifest's cursor still names the offset
    # past *both* batches, which no longer exists in this file.
    truncated = b"\n".join(non_empty[:2]) + b"\n"
    log_path.write_bytes(truncated)

    reader = tgms.open(tmp_path / "s", backend="native", read_only=True)
    result = ReadinessProbe(reader).check()
    assert result["ready"] is False
    assert "reason" in result and result["reason"]
    assert "ahead of the event log" in result["reason"]
    reader.close()


def test_readiness_probe_reflects_a_broken_adapter_on_recheck(tmp_path):
    """`check()` re-polls the adapter every call (the cheap part); an
    adapter that stops answering after open must flip `ready` to False even
    though the one-time verdict at construction was True."""
    store = _healthy_store(tmp_path)
    probe = ReadinessProbe(store)
    assert probe.check()["ready"] is True

    def _boom():
        raise RuntimeError("adapter exploded")

    store.adapter.stats = _boom
    result = probe.check()
    assert result["ready"] is False
    store.close()


def test_cli_store_ready_happy_path(tmp_path, capsys):
    store = _healthy_store(tmp_path)
    store.close()
    rc = cli_main(["store", "ready", "--store", str(tmp_path / "s"), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["ready"] is True


def test_cli_store_ready_reports_open_failure_as_not_ready(tmp_path, capsys):
    store = _healthy_store(tmp_path)
    store.close()
    log_path = tmp_path / "s" / "eventlog.jsonl"
    original = log_path.read_bytes()
    log_path.write_bytes(original[: len(original) // 2])

    # opening as the WRITER runs `_recover()`, which raises loudly on a
    # cursor pointing past the (now-truncated) log's end -- the CLI must
    # turn that raise into a not-ready report and exit 2, not a traceback.
    rc = cli_main(["store", "ready", "--store", str(tmp_path / "s"),
                  "--writer", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["ready"] is False
    assert out["reason"]


@pytest.fixture()
def demo_server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("health")
    from tgms.data.synth import generate
    generate(tmp / "synth", n_nodes=20, n_events=100, seed=1)
    store = tgms.open(tmp / "store")
    with open(tmp / "synth" / "events.jsonl") as f:
        store.ingest_events(json.loads(line) for line in f if line.strip())
    suite = {"dev": [], "test": []}
    app = DemoApp(store, suite, "none", lambda *a, **k: "not json",
                 tmp / "results")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    store.close()


def test_webapp_live_endpoint(demo_server):
    with urllib.request.urlopen(demo_server + "/live", timeout=10) as r:
        assert r.status == 200
        body = json.loads(r.read())
    assert body["live"] is True


def test_webapp_ready_endpoint_reports_healthy(demo_server):
    with urllib.request.urlopen(demo_server + "/ready", timeout=10) as r:
        assert r.status == 200
        body = json.loads(r.read())
    assert body["ready"] is True
    assert body["recovery_pending"] is False
