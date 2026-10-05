"""Regression test for `neo4j_recompute.runner.load_oracle` (found running
lane N1's real timed grid, 2026-10-05, on the real exported cell
`collegemsg-c1-deep-n1000-s0`: every epoch scored `agree=0,
disagree=['digests', 'refused']`).

`oracle.jsonl`'s real row shape, written by `scripts/export_storm_workload.py`
and consumed independently by `scripts/external_check.py::load_oracle_rows`,
is `{"epoch": k, "digests": {name: digest, ...}, "refused": [name, ...]}` --
never a flat `{name: digest, ..., "epoch": k}` row. `load_oracle` used to
flatten the raw row (keeping every key but `"epoch"`), so
`score_against_oracle` iterated over the two literal pseudo-artifacts
`"digests"` (value: the whole digest dict) and `"refused"` (value: the whole
refused list) instead of the real per-artifact digests -- always
`agree=0`. This test pins the real row shape directly; it does not use
`tests/external/neo4j_recompute/fixtures/tiny-cell/oracle.jsonl`, whose own
rows are flat and therefore exercise the pre-fix shape, not a real export's
-- a mismatch this test does not attempt to resolve (see the README's
"Known gaps" / this lane's own hand-back note).
"""
import json

from neo4j_recompute.runner import load_oracle


def test_load_oracle_flattens_digests_and_maps_refused_to_sentinel(tmp_path):
    oracle_path = tmp_path / "oracle.jsonl"
    rows = [
        {"epoch": 0, "digests": {"f1": "aaa", "f2": "bbb"}, "refused": []},
        {"epoch": 1, "digests": {"f1": "ccc"}, "refused": ["f2"]},
    ]
    oracle_path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    out = load_oracle(oracle_path)

    assert out == {
        0: {"f1": "aaa", "f2": "bbb"},
        1: {"f1": "ccc", "f2": "refused"},
    }
