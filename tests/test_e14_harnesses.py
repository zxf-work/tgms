"""The two E14 harnesses cover exactly what their claims say (§C1, §C2).

These are measurement scripts, so what is testable is their *scope*: a claim
about "all fifteen operators" that quietly measured twelve, or a claim about
"the two compiled operators" that drifted to include an opaque leaf, would be
an overclaim no reviewer could see from the numbers.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


STATS = {"vt_min": 0, "vt_max": 1_000_000_000}


def test_p1_covers_every_operator_the_registry_has():
    """§C1 claims all fifteen. `compute` is the sixteenth case because two
    `entity_history` shapes are timed, so the operator set is what matters."""
    from tgms.temporal.algebra import REGISTRY, ensure_all_registered

    ensure_all_registered()
    p1 = _load("bench_leaf_overhead")
    covered = {op for _id, op, _args in p1.cases(STATS)}
    missing = set(REGISTRY) - covered
    assert not missing, f"P1 does not time these operators: {sorted(missing)}"
    assert len(REGISTRY) == 15


def test_p1_runs_one_condition_per_process():
    """`engine_lessons.md` §9g: operators in one process tax each other, so the
    two arms must not share an interpreter."""
    p1 = _load("bench_leaf_overhead")
    src = (ROOT / "scripts" / "bench_leaf_overhead.py").read_text()
    assert "subprocess.run" in src
    assert "TGIR_PLAN_PATH" in src
    assert p1.WARMUPS == 5


def test_p2_measures_exactly_the_compiled_pairs_and_no_leaf():
    """The population is two. Including an operator with no compiled form would
    be measuring the leaf against itself."""
    from tgms.tgir.compiled import COMPILED

    p2 = _load("bench_compiled_vs_kernel")
    assert set(p2.cases(STATS)) == set(COMPILED) == {"entity_history",
                                                     "version_history"}


def test_p2_states_the_band_its_claim_was_frozen_at():
    p2 = _load("bench_compiled_vs_kernel")
    assert p2.BAND == 3.0
