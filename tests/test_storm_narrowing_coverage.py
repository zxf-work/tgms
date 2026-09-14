"""storm-v1 addendum-4 (D-161): `tgms.eval.storm.narrowing_coverage`.

Addendum-1's finding (confirmed in code, not a checker defect): most of the
storm harness's own registered population gets the coarse `"*"` scope
fallback, because `tgms.tgir.leaves.LEAF_SCOPES` derives a real scope for
only 3 of the 14 `TEMPLATES` (`entity_history`, `neighborhood_evolution`,
`aggregate_events`); every other template's registration lands on
`all_terms() == (TOP_TERM,)`, and `compute`'s own `∅`-scope control lands
on `all_terms() == ()`. `narrowing_coverage` counts this, once at
registration time, for the coordinator's own population-vs-checker
analysis (never touching the oracle/arms/digest chain).

Fixture idiom shared with `tests/test_storm_dag.py`/`tests/
test_storm_dag_seeding.py`: a small event-stream store built via the normal
write API, and a *hand-built* three-artifact population (one `compute`, one
`entity_history`, one `version_history` -- a template `LEAF_SCOPES` does not
name) rather than `Storm`'s own random population, so every count below is
exact and independent of `TEMPLATES`' random draw order.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

import tgms
from tgms.eval.storm import ArtifactMeta, RegisteredArtifact, Storm, narrowing_coverage

BACKEND = "native"


def _build_fixture(store_dir: Path, *, n_nodes: int = 10, n_events: int = 30,
                   seed: int = 1) -> None:
    """Restated verbatim from tests/test_storm_dag.py::_build_fixture (this
    file's own copy, the same "restated, not imported" reasoning that
    module's own docstring gives for theirs)."""
    store = tgms.open(store_dir, backend=BACKEND)
    rng = random.Random(seed)
    uids = [f"n{i}" for i in range(n_nodes)]
    events = []
    for i in range(n_events):
        src, dst = rng.sample(uids, 2)
        events.append({"src": src, "dst": dst, "rel_type": "FOLLOWS", "vt_s": i * 5})
    store.ingest_events(events, node_label="Person")
    store.close()


@pytest.fixture()
def small_store(tmp_path: Path) -> Path:
    store_dir = tmp_path / "store"
    _build_fixture(store_dir)
    return store_dir


def _register(storm: Storm, name: str, op: str, args: dict, entities, window) -> None:
    record, env = storm._register_operator_artifact(name, op, args)
    storm.artifacts[name] = RegisteredArtifact(
        name=name, meta=ArtifactMeta(op=op, args=args, entities=entities, window=window),
        last_env={"result_digest": env.get("result_digest")})


def test_one_compute_one_entity_history_one_version_history(small_store: Path) -> None:
    """The exact three-artifact population this module's own docstring
    describes: one `compute` (empty scope), one `entity_history` (a real
    derivation, 3 terms since `include_edges=True`), one `version_history`
    (not in `LEAF_SCOPES` -- the coarse `(TOP_TERM,)` fallback)."""
    storm = Storm(small_store, n_artifacts=0, seed=0, backend=BACKEND)
    try:
        _register(storm, "a-compute", "compute",
                 {"fn": "count", "input": [{"x": 1}, {"x": 2}]}, frozenset(), None)
        _register(storm, "a-entity", "entity_history",
                 {"uid": "n0", "include_edges": True}, frozenset({"n0"}), None)
        _register(storm, "a-version", "version_history",
                 {"kind": "node", "window": {"t_a": 0, "t_b": 50}}, frozenset(), (0, 50))

        nc = narrowing_coverage(storm.registry, storm.artifacts)

        assert nc["n_artifacts"] == 3
        assert nc["n_empty_scope"] == 1          # a-compute only
        assert nc["n_all_top_term"] == 1         # a-version only
        assert nc["per_template_counts"] == {
            "compute": 1, "entity_history": 1, "version_history": 1,
        }
        # a-compute: 0 terms; a-entity: 3 (node, dense-id, edge -- include_edges
        # is True); a-version: 1 (the coarse TOP_TERM fallback)
        assert nc["total_terms"] == 4
        assert nc["top_axis_counts"] == {
            # kinds/targets is TOP only for the coarse a-version term --
            # entity_history's own derivation always names a concrete kind
            "kinds": 1, "targets": 1,
            # rel_types is TOP on all 3 entity_history terms *and* a-version's
            "rel_types": 4,
            # vt is TOP on all 3 entity_history terms (entity_history_terms's
            # own unconditional choice) *and* a-version's
            "vt": 4,
            # props is TOP on entity_history's node+edge terms (not its
            # dense-id term, which names "@identity") plus a-version's
            "props": 3,
        }
    finally:
        storm.close()


def test_empty_population_is_all_zero(small_store: Path) -> None:
    """No artifacts registered -- every count is zero, not an error."""
    storm = Storm(small_store, n_artifacts=0, seed=0, backend=BACKEND)
    try:
        nc = narrowing_coverage(storm.registry, storm.artifacts)
        assert nc == {
            "n_artifacts": 0, "n_all_top_term": 0, "n_empty_scope": 0,
            "per_template_counts": {}, "total_terms": 0,
            "top_axis_counts": {"kinds": 0, "targets": 0, "rel_types": 0, "vt": 0, "props": 0},
        }
    finally:
        storm.close()


def test_narrowing_coverage_lands_in_the_manifest_summary(small_store: Path) -> None:
    """The wiring `scripts/bench_correction_storm.py` does (registration-time
    computation, merged into `summary`) -- reproduced directly against
    `Storm`/`narrowing_coverage` rather than by shelling out to the CLI, the
    same "test the library, not the script" preference `tests/
    test_storm.py` already follows. Confirms the two mutually-exclusive
    counts (`n_all_top_term` + `n_empty_scope` + every other template's
    non-top-non-empty artifacts) never double-count against `n_artifacts`,
    the property the campaign's own analysis relies on."""
    storm = Storm(small_store, n_artifacts=0, seed=0, backend=BACKEND)
    try:
        _register(storm, "a-compute", "compute",
                 {"fn": "count", "input": [{"x": 1}, {"x": 2}]}, frozenset(), None)
        _register(storm, "a-version", "version_history",
                 {"kind": "node", "window": {"t_a": 0, "t_b": 50}}, frozenset(), (0, 50))
        _register(storm, "a-neighborhood", "neighborhood_evolution",
                 {"uid": "n0", "t1": 0, "t2": 20, "stride": 2}, frozenset({"n0"}), (0, 20))

        nc = narrowing_coverage(storm.registry, storm.artifacts)
        assert nc["n_artifacts"] == 3
        # every artifact is accounted for exactly once across the three
        # mutually-exclusive buckets a reader would sum:
        n_narrow = nc["n_artifacts"] - nc["n_all_top_term"] - nc["n_empty_scope"]
        assert n_narrow == 1  # a-neighborhood: neither all-TOP_TERM nor empty
    finally:
        storm.close()
