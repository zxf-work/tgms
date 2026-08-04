"""O13 `compute` arithmetic — the `AR` capability the independent-question
study asks for: `mean`, `median`, `ratio`, `diff`, `percent`.

`compute` never touches the store, so its ground truth is not the brute-force
store oracle in `tgms/temporal/oracle.py`; it is arithmetic. The reference
below is written from the *contract* using `fractions.Fraction`, which is an
independent path to the same answer: the kernel forms a quotient with
`divmod` and one IEEE rounding, the reference forms the exact rational first
and only then decides how to spell it.

THE BLESSED RULE (D-044's `_mean`, extended to say when a result stays exact):

  * every contributing value an `int` — the quotient is formed in exact
    integer arithmetic and returned as an `int` when it is exact; otherwise
    exactly one IEEE rounding, `q, r = divmod(num, den)` ->
    `float(q) + r / den`;
  * any contributing value a `float` — terms are summed with `math.fsum`
    (correctly rounded and order-independent) and divided once.

Determinism is what the rule is *for*, so the properties that never mention
the formula — permutation invariance, repeat-identity of the canonical
payload, and exactness above 2**53 — carry more weight here than the
formula-shaped equality tests.
"""

from __future__ import annotations

import math
import os
from fractions import Fraction
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from tgms.core.errors import InvalidArgError, SchemaError
from tgms.core.model import canonical_json
from tgms.temporal.algebra import (
    _canonicalize_floats,
    call_operator,
    ensure_all_registered,
)

from .conftest import fresh_adapter

ensure_all_registered()

N_EXAMPLES = int(os.environ.get("TGMS_HYP_EXAMPLES", "25"))
SETTINGS = settings(max_examples=N_EXAMPLES, deadline=None,
                    suppress_health_check=[HealthCheck.too_slow,
                                           HealthCheck.data_too_large])

_adapter: list[Any] = []


def adapter():
    """One empty store for the whole module: `compute` reads none of it, but
    `call_operator` still asks it for `dataset_extent`."""
    if not _adapter:
        _adapter.append(fresh_adapter())
    return _adapter[0]


def value(**args) -> Any:
    return call_operator(adapter(), "compute", args)["value"]


def payload(**args) -> str:
    """Canonical bytes of everything the digest is taken over."""
    r = call_operator(adapter(), "compute", args)
    return canonical_json({k: v for k, v in r.items()
                           if k not in ("op", "args_echo", "dataset_extent",
                                        "result_digest")})


# --- the reference ---------------------------------------------------------- #

def ref_quotient(num: Any, den: Any) -> Any:
    """`num / den` under the blessed rule, via exact rationals."""
    if isinstance(num, int) and isinstance(den, int):
        exact = Fraction(num, den)
        if exact.denominator == 1:
            return int(exact)
        q = math.floor(exact)          # Python's divmod floors; so does this
        return float(q) + (num - q * den) / den
    return num / den


def ref_mean(vals: list[Any]) -> Any:
    if all(isinstance(v, int) for v in vals):
        return ref_quotient(sum(vals), len(vals))
    return math.fsum(vals) / len(vals)


def ref_median(vals: list[Any]) -> Any:
    s = sorted(vals)
    n = len(s)
    if n % 2:
        return s[n // 2]             # the exact datum: no arithmetic performed
    return ref_mean([s[n // 2 - 1], s[n // 2]])


def ref_percent(x: Any, y: Any) -> Any:
    if isinstance(x, int) and isinstance(y, int):
        return ref_quotient(100 * x, y)
    return (100 * x) / y


def canon(v: Any) -> Any:
    """The operator's output passes through `_canonicalize_floats`; so must
    anything compared against it."""
    return _canonicalize_floats(v)


# --- oracle cases: the typing and exactness table --------------------------- #

@pytest.mark.parametrize("vals,expected", [
    ([1, 2, 3], 2),                       # exact -> int, not 2.0
    ([1, 2], 1.5),
    ([2, 3, 4, 5], 3.5),
    ([-1, -2], -1.5),
    ([-1, 2], 0.5),
    ([-3], -3),
    ([0, 0, 0], 0),
    ([1.5, 2.5], 2.0),                    # any float -> float
    ([1, 2.0], 1.5),
    ([7], 7),
])
def test_mean_oracle_cases(vals, expected):
    got = value(fn="mean", input=vals)
    assert got == expected and type(got) is type(expected), (got, expected)


@pytest.mark.parametrize("vals,expected", [
    ([3, 1, 2], 2),                       # odd -> the exact middle datum
    ([1, 2, 3, 4], 2.5),
    ([4, 1], 2.5),
    ([2, 2], 2),                          # even but exact -> int
    ([5], 5),
    ([-5, -1], -3),
    ([1, 2, 3, 4, 5, 6], 3.5),
    ([1.0, 2.0, 3.0], 2.0),               # odd: the datum, float in float out
])
def test_median_oracle_cases(vals, expected):
    got = value(fn="median", input=vals)
    assert got == expected and type(got) is type(expected), (got, expected)


@pytest.mark.parametrize("x,y,expected", [
    (6, 3, 2),                            # exact -> int
    (1, 2, 0.5),
    (-1, 2, -0.5),
    (1, -2, -0.5),
    (-6, 3, -2),
    (0, 5, 0),
    (1.0, 4, 0.25),
    (7, 2, 3.5),
])
def test_ratio_oracle_cases(x, y, expected):
    got = value(fn="ratio", x=x, y=y)
    assert got == expected and type(got) is type(expected), (got, expected)


@pytest.mark.parametrize("x,y,expected", [
    (5, 3, 2),
    (3, 5, -2),
    (0, 0, 0),
    (2.5, 1, 1.5),
    (-4, -9, 5),
])
def test_diff_oracle_cases(x, y, expected):
    got = value(fn="diff", x=x, y=y)
    assert got == expected and type(got) is type(expected), (got, expected)


@pytest.mark.parametrize("x,y,expected", [
    (1, 4, 25),                           # exact -> int
    (1, 3, 33.333333333),                 # canonicalized to 9 decimals
    (3, 4, 75),
    (0, 7, 0),
    (7, 7, 100),
    (1, 8, 12.5),
])
def test_percent_oracle_cases(x, y, expected):
    got = value(fn="percent", x=x, y=y)
    assert got == expected and type(got) is type(expected), (got, expected)


def test_exactness_survives_above_2_pow_53():
    """The reason the integer path stays integral: a float64 cannot hold
    these, and the answers are what an epoch-microsecond mean looks like."""
    big = 2**62
    assert value(fn="mean", input=[big, big]) == big
    assert value(fn="median", input=[big - 1, big + 1]) == big
    assert value(fn="diff", x=big + 3, y=big) == 3
    assert value(fn="ratio", x=big, y=2) == 2**61
    # and the near-miss is a float, one rounding, as the rule says
    odd = value(fn="mean", input=[big, big + 1])
    assert odd == float(big) + 0.5


# --- property tests against the reference ----------------------------------- #

ints = st.integers(-10**6, 10**6)
floats = st.floats(-1e6, 1e6, allow_nan=False, allow_infinity=False,
                   width=64)
numbers = st.one_of(ints, floats)
nonzero = st.one_of(ints.filter(lambda v: v != 0),
                    floats.filter(lambda v: abs(v) > 1e-3))


@SETTINGS
@given(vals=st.lists(numbers, min_size=1, max_size=40))
def test_mean_matches_the_reference(vals):
    assert canon(value(fn="mean", input=vals)) == canon(ref_mean(vals))


@SETTINGS
@given(vals=st.lists(numbers, min_size=1, max_size=40))
def test_median_matches_the_reference(vals):
    assert canon(value(fn="median", input=vals)) == canon(ref_median(vals))


@SETTINGS
@given(x=numbers, y=nonzero)
def test_ratio_matches_the_reference(x, y):
    assert canon(value(fn="ratio", x=x, y=y)) == canon(ref_quotient(x, y))


@SETTINGS
@given(x=numbers, y=numbers)
def test_diff_matches_the_reference(x, y):
    assert canon(value(fn="diff", x=x, y=y)) == canon(x - y)


@SETTINGS
@given(x=numbers, y=nonzero)
def test_percent_matches_the_reference(x, y):
    assert canon(value(fn="percent", x=x, y=y)) == canon(ref_percent(x, y))


@SETTINGS
@given(rows=st.lists(st.fixed_dictionaries({"c": ints, "other": st.text()}),
                     min_size=1, max_size=30))
def test_field_selection_agrees_with_the_bare_list(rows):
    """`field` is the only thing that should differ between a row form and a
    scalar form of the same call."""
    bare = [r["c"] for r in rows]
    for fn in ("mean", "median"):
        assert value(fn=fn, input=rows, field="c") == value(fn=fn, input=bare)


# --- metamorphic properties (formula-free) ---------------------------------- #

@SETTINGS
@given(vals=st.lists(ints, min_size=1, max_size=30), seed=st.integers(0, 10**6))
def test_mean_and_median_are_permutation_invariant(vals, seed):
    import random
    shuffled = list(vals)
    random.Random(seed).shuffle(shuffled)
    for fn in ("mean", "median"):
        assert payload(fn=fn, input=vals) == payload(fn=fn, input=shuffled)


@SETTINGS
@given(vals=st.lists(ints, min_size=1, max_size=30))
def test_mean_is_the_sum_over_the_count(vals):
    s = value(fn="sum", input=vals)
    n = value(fn="count", input=vals)
    assert value(fn="mean", input=vals) == value(fn="ratio", x=s, y=n)


@SETTINGS
@given(vals=st.lists(ints, min_size=1, max_size=30))
def test_mean_and_median_lie_between_min_and_max(vals):
    lo, hi = value(fn="min", input=vals), value(fn="max", input=vals)
    for fn in ("mean", "median"):
        assert lo <= value(fn=fn, input=vals) <= hi


@SETTINGS
@given(v=ints, n=st.integers(1, 20))
def test_a_constant_group_is_its_own_mean_and_median(v, n):
    assert value(fn="mean", input=[v] * n) == v
    assert value(fn="median", input=[v] * n) == v


@SETTINGS
@given(x=numbers, y=numbers)
def test_diff_is_antisymmetric(x, y):
    assert canon(value(fn="diff", x=x, y=y)) == canon(-value(fn="diff", x=y, y=x))


@SETTINGS
@given(x=ints, y=ints.filter(lambda v: v != 0))
def test_percent_relates_to_ratio_without_inheriting_its_rounding(x, y):
    """`percent` is not `ratio` times a hundred, and must not be implemented
    that way: percent(1, 5) is exactly 20 while ratio(1, 5) has already spent
    its one rounding on the float 0.2. The identity holds only where `ratio`
    itself stayed exact; everywhere else both are judged against the rational.
    """
    r, p = value(fn="ratio", x=x, y=y), value(fn="percent", x=x, y=y)
    if isinstance(r, int):
        assert p == 100 * r
    exact = Fraction(100 * x, y)
    assert abs(Fraction(p) - exact) <= abs(exact) / 10**9 + Fraction(1, 10**6)


@SETTINGS
@given(vals=st.lists(ints, min_size=1, max_size=30), k=ints)
def test_median_commutes_with_a_shift(vals, k):
    shifted = [v + k for v in vals]
    assert value(fn="median", input=shifted) == \
        value(fn="diff", x=value(fn="median", input=vals), y=-k)


@SETTINGS
@given(vals=st.lists(numbers, min_size=1, max_size=20))
def test_results_are_byte_identical_on_repeat(vals):
    for fn in ("mean", "median"):
        assert payload(fn=fn, input=vals) == payload(fn=fn, input=vals)


# --- argument contract ------------------------------------------------------ #

def test_argument_contract():
    # empty input has no mean and no median (`sum` still answers 0, unchanged)
    for fn in ("mean", "median"):
        with pytest.raises(InvalidArgError):
            value(fn=fn, input=[])
    # ...and requires an input at all
    for fn in ("mean", "median"):
        with pytest.raises(InvalidArgError):
            value(fn=fn)

    # binary functions require both operands, and reject a row list
    for fn in ("ratio", "diff", "percent"):
        with pytest.raises(InvalidArgError):
            value(fn=fn, x=1)
        with pytest.raises(InvalidArgError):
            value(fn=fn, y=1)
        with pytest.raises(InvalidArgError):
            value(fn=fn)

    # division by zero names the operand rather than raising ZeroDivisionError
    for fn in ("ratio", "percent"):
        with pytest.raises(InvalidArgError):
            value(fn=fn, x=1, y=0)

    # non-numeric values are rejected, booleans included (True is not 1 here)
    for fn in ("mean", "median"):
        with pytest.raises(InvalidArgError):
            value(fn=fn, input=[1, "2"])
        with pytest.raises(InvalidArgError):
            value(fn=fn, input=[1, True])
        with pytest.raises(InvalidArgError):
            value(fn=fn, input=[{"c": 1}], field="missing")

    # the schema rejects a boolean operand before the kernel sees it
    for fn in ("ratio", "diff", "percent"):
        with pytest.raises((SchemaError, InvalidArgError)):
            value(fn=fn, x=True, y=1)

    # non-finite input cannot survive: it would leave the digest unstable
    with pytest.raises(InvalidArgError):
        value(fn="mean", input=[1.0, float("inf")])
    with pytest.raises(InvalidArgError):
        value(fn="ratio", x=float("nan"), y=1)


def test_the_new_functions_are_advertised():
    """A function the planner cannot see is a function that does not exist.

    The prose it reads is **not** the registry description — `anthropic_tools`
    prefers `configs/tool_manual.yaml`, so the manual is the surface that has
    to name every function. Checking only the registry string is the way to
    ship an unreachable capability.
    """
    from tgms.temporal.algebra import REGISTRY
    from tgms.tools.schemas import anthropic_tools

    spec = REGISTRY["compute"]
    enum = spec.args_schema["properties"]["fn"]["enum"]
    shown = [t for t in anthropic_tools() if t["name"] == "compute"][0]
    for fn in ("mean", "median", "ratio", "diff", "percent"):
        assert fn in enum
        assert fn in spec.description
        assert fn in shown["description"], f"{fn} is missing from the manual"
    # the binary operands need naming too, or a planner has no way to bind them
    for arg in ("x", "y"):
        assert arg in shown["input_schema"]["properties"]
    assert "`x` and `y`" in shown["description"]


def test_binary_operands_accept_a_prior_step_scalar():
    """The shape the plans actually use: `x` and `y` arrive by $ref from two
    earlier steps, so they must accept a bare number rather than a row list."""
    a = value(fn="count", input=[1, 2, 3, 4])
    b = value(fn="count", input=[1, 2])
    assert value(fn="diff", x=a, y=b) == 2
    assert value(fn="ratio", x=a, y=b) == 2
    assert value(fn="percent", x=b, y=a) == 50
