"""The commands a stranger runs first, on the install a stranger actually has.

`pip install tgms` ships the native engine and nothing else — duckdb and kuzu
became optional extras with D-028/D-029. The CLI kept asking for duckdb by
default, so the documented first command after a clean install,

    tgms ingest events.jsonl --store x

died with "this store uses the duckdb backend, which is now an optional
extra": the entry path named a backend the wheel no longer contains. Nothing
caught it, because every development environment has duckdb installed from the
dev group, where the stale default merely produced the wrong store type in
silence.

So these tests assert the *type of store that appears on disk*, not just that
the command exited zero — in a dev venv the bug is invisible to an exit code.
"""

from __future__ import annotations

import argparse
import json

import pytest

import tgms
from tgms.cli import build_parser, main

#: Backends that are no longer in the wheel. Defaulting to one of these is
#: the bug this file exists to prevent.
OPTIONAL_EXTRAS = {"duckdb", "kuzu"}

EVENTS = [
    {"src": "a", "dst": "b", "rel_type": "MSG", "vt_s": 10},
    {"src": "b", "dst": "c", "rel_type": "MSG", "vt_s": 20},
    {"src": "c", "dst": "a", "rel_type": "MSG", "vt_s": 30},
]


def write_events(tmp_path) -> str:
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in EVENTS))
    return str(path)


def is_native(store_path) -> bool:
    return (store_path / "native").is_dir() \
        and not (store_path / "store.duckdb").exists() \
        and not (store_path / "store.kuzu").exists()


def test_ingest_without_a_backend_flag_builds_a_native_store(tmp_path, capsys):
    """The exact command from the quickstart, with no flags to guess at."""
    store = tmp_path / "x"
    assert main(["ingest", write_events(tmp_path), "--store", str(store)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stats"]["n_edge_versions"] == len(EVENTS)

    assert is_native(store), f"clean-install ingest built {list(store.iterdir())}"
    reopened = tgms.open(store)
    assert reopened.backend == "native"
    reopened.close()


def test_replay_without_a_backend_flag_rebuilds_natively(tmp_path, capsys):
    """`tgms replay` is the documented recovery and migration path, so it is
    reachable by anyone whose store is in trouble — with the same wheel."""
    src, dst = tmp_path / "src", tmp_path / "dst"
    assert main(["ingest", write_events(tmp_path), "--store", str(src)]) == 0
    capsys.readouterr()

    assert main(["replay", str(src / "eventlog.jsonl"), "--store", str(dst)]) == 0
    capsys.readouterr()
    assert is_native(dst)

    a, b = tgms.open(src), tgms.open(dst)
    assert a.digest() == b.digest()  # replay is byte-identical, backend aside
    a.close()
    b.close()


def test_no_subcommand_defaults_to_a_backend_the_wheel_does_not_ship(tmp_path):
    """Pin the class of bug, not the two instances of it: any future
    subcommand that grows a `--backend` must not default to an extra."""
    checked = 0
    for name, sub in _subparsers(build_parser()):
        for action in sub._actions:
            if action.dest != "backend":
                continue
            checked += 1
            assert action.default not in OPTIONAL_EXTRAS, (
                f"`tgms {name} --backend` defaults to {action.default!r}, which "
                f"a plain `pip install tgms` does not provide")
    assert checked >= 2, "expected ingest and replay to take --backend"


def test_an_existing_duckdb_store_still_opens_without_the_flag(tmp_path, capsys):
    """Why the default defers to `tgms.open` instead of naming "native".

    Hard-coding the modern backend would strand data written by an older
    release: the ingest would create an empty native store beside the DuckDB
    one and report success, which is what `detect_backend` exists to prevent.
    """
    pytest.importorskip("duckdb")
    store = tmp_path / "legacy"
    events = write_events(tmp_path)
    assert main(["ingest", events, "--store", str(store),
                 "--backend", "duckdb"]) == 0
    capsys.readouterr()
    assert (store / "store.duckdb").exists()

    assert main(["ingest", events, "--store", str(store)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["stats"]["n_edge_versions"] == 2 * len(EVENTS)
    assert not (store / "native").exists(), \
        "the flagless ingest built a second store beside the existing one"


def _subparsers(parser: argparse.ArgumentParser):
    """(name, parser) for every subcommand. argparse exposes no public
    accessor, and the alternative — re-listing the subcommands here — would
    quietly stop covering the one that gets added next."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            yield from action.choices.items()
