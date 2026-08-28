"""`scripts/tgir_measure.py` had no CLI layer of its own: `main()` went
straight to `ensure_all_registered()` and a real measured run. On 2026-08-27,
`uv run python scripts/tgir_measure.py --help` was typed to check the script's
options and instead ran the full 52-row measurement — real store queries
against `stores/ldbc-fixture`, `stores/bitcoinotc`, and `stores/collegemsg`,
on course to rewrite
`benchmarks/tgir-v1/measured.yaml` — and had to be killed mid-run.

The fix adds a minimal `argparse.ArgumentParser` at the top of `main()` (so
`--help` and any unknown flag exit via argparse before anything else runs)
plus a TTY confirmation guard (so a person at a terminal is asked before an
expensive run starts, while the documented bare non-interactive invocation —
the one `gen_measured_report.py`'s reproducibility recipe depends on — keeps
running and writing without a prompt).

These tests pin both properties without touching a store or running a real
measurement: `tgir_measure.ensure_all_registered` is monkeypatched to raise a
sentinel exception, so reaching it proves the guard let the run through, and
not reaching it proves argparse or the confirmation guard stopped it first.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

tgir_measure = pytest.importorskip("tgir_measure")


class _ReachedRealWork(Exception):
    """Raised in place of `ensure_all_registered`; reaching it means the CLI
    guard let the run through to real store work."""


class _FakeStdin:
    """A stand-in for `sys.stdin` whose `isatty()` is fixed at construction —
    pytest replaces `sys.stdin` with its own capture object, so patching a
    tiny stub is the reliable way to control what `.isatty()` reports."""

    def __init__(self, is_tty: bool) -> None:
        self._is_tty = is_tty

    def isatty(self) -> bool:
        return self._is_tty


@pytest.fixture(autouse=True)
def _sentinel_registered(monkeypatch):
    monkeypatch.setattr(tgir_measure, "ensure_all_registered",
                         lambda: (_ for _ in ()).throw(_ReachedRealWork()))


def test_help_exits_before_any_work(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        tgir_measure.main(["--help"])
    assert excinfo.value.code == 0


def test_unknown_flag_exits_before_any_work(monkeypatch):
    with pytest.raises(SystemExit) as excinfo:
        tgir_measure.main(["--frobnicate"])
    assert excinfo.value.code == 2


def test_tty_declined_prompt_returns_2_without_running(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(True))
    monkeypatch.setattr("builtins.input", lambda *_args, **_kw: "n")
    assert tgir_measure.main([]) == 2


def test_tty_with_write_flag_skips_prompt(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(True))

    def _fail_if_called(*_args, **_kw):
        raise AssertionError("input() should not be called with --write")

    monkeypatch.setattr("builtins.input", _fail_if_called)
    with pytest.raises(_ReachedRealWork):
        tgir_measure.main(["--write"])


def test_non_tty_bare_invocation_runs_without_prompting(monkeypatch):
    monkeypatch.setattr(sys, "stdin", _FakeStdin(False))

    def _fail_if_called(*_args, **_kw):
        raise AssertionError("input() should not be called when stdin is not a tty")

    monkeypatch.setattr("builtins.input", _fail_if_called)
    with pytest.raises(_ReachedRealWork):
        tgir_measure.main([])
