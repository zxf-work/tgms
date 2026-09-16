"""`scripts/tgir_paper_macros.py`'s pure helpers and its `--root` re-rooting.

The generator itself is self-checking — it runs 585 assertions over the
row-level records and refuses to write on any failure — so there is nothing
useful to re-assert about its *values* here.  What a test can pin, and what
this repository cannot exercise end to end, is different:

  - **The two number registers.**  `sf1_ratio` reproduces the evidence
    report's own formatting of an estimate/actual ratio: three decimals below
    1.0 (where the leading digits are the finding — an under-estimate), a
    whole number at or above it (where the ratio is an order-of-magnitude
    statement).  `tab-sf1.tex` is asserted against that report's §1.4 table
    cell for cell, so a change here silently breaks the generator's strongest
    cross-check; a test makes it break here instead, with a reason.

  - **`tex_num`'s threshold.**  Four-digit numbers take no separator, which is
    why BI18's 5918 ms appears unseparated beside BI17's 1{,}278{,}984{,}344.
    That is deliberate and is easy to "fix" by accident.

  - **`set_root` re-points every source together.**  This is the whole point
    of `--root`: the public repository carries the script and the benchmark
    records but not `docs/design/` or `paper/`, so the script is run against
    the internal checkout.  If a path were left behind by a re-rooting, the
    generator would read half of one tree and half of another and its
    assertions would still pass.  The test asserts that after `set_root`,
    every `Path` constant in the module lies under the new root.

Importing the module is safe without the internal tree: `set_root` only
builds `Path` objects, and no source is read until `main()` runs.

**Why this file is not called `test_tgir_paper_macros.py`.**  The generator
counts `tests/test_tgir_*.py` and their `def test_` bodies into `\tgTestFiles`
and `\tgTests`, which the manuscript cites as the TGIR *implementation's* test
receipt.  Tests of the paper's own tooling are not part of that receipt, and a
matching name would inflate a published number with meta-work.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import tgir_paper_macros as TPM  # noqa: E402

# The PVLDB terminology sheet's banned words (paper/tgir-vldb/PAPER_MAP.md):
# internal-process vocabulary that must never reach a reader-facing caption.
# "ruling" and "tgir-v1" were added after a review-round note: the draft names
# the adjudication rules A1-A7 (never the derivation's own R1-R7 "rulings"),
# and the system is "TGIR", never "TGIR-v1".  "\tgspecanchor" and
# "\tgsfonecommit" were added after a second note: §9.1 states the
# pre-registration anchors once, so the per-table captions drop the commit
# hashes and keep only the date.
BANNED_CAPTION_WORDS = (
    "arm", "campaign", "frozen", "bypassed-but-recording", "receipt", "instrument",
    "ruling", "tgir-v1", "\\tgspecanchor", "\\tgsfonecommit",
)

# A bare rule letter like "R4" or "R3b" (as opposed to its vldb rename "A4" /
# "A3b"). `\b` keeps this from matching inside "TGIR" or similar.
_BARE_RULE_LETTER = re.compile(r"\bR[1-9][a-z]?\b")


def _caption_line(tex: str) -> str:
    cap_lines = [ln for ln in tex.splitlines() if ln.startswith("\\caption{")]
    assert len(cap_lines) == 1, "expected exactly one caption line in the rendered table"
    return cap_lines[0]


def _numeric_cells(tex: str) -> list[str]:
    """The tabular body's numeric tokens, in order, from every column but the
    first.

    Column 1 is always a row label (a suite, a rung, a plan id, a primitive
    name) rather than measured data, and a style rewrite may reword it --
    e.g. the vldb rule-letter rename turns "R1--R3b" into "A1 to A3b", which
    would otherwise register as a spurious numeric difference.  Ignoring that
    column is what lets this helper assert what actually matters: every real
    number renders identically between styles.
    """
    lines = tex.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "\\midrule") + 1
    end = next(i for i, ln in enumerate(lines) if ln.strip() == "\\bottomrule")
    cells: list[str] = []
    for row in lines[start:end]:
        data_fields = "&".join(row.split("&")[1:])
        cells.extend(re.findall(r"-?\d[\d,{}]*(?:\.\d+)?", data_fields))
    return cells


def test_sf1_ratio_uses_three_decimals_below_one() -> None:
    """The three under-estimates of the scored arm, as §1.4 prints them."""
    assert TPM.sf1_ratio(5918 / 29734.52877998352) == "0.199"   # BI18
    assert TPM.sf1_ratio(33254 / 122200.7782459259) == "0.272"  # BI12
    assert TPM.sf1_ratio(31811 / 66036.57650947571) == "0.482"  # BI9


def test_sf1_ratio_uses_whole_numbers_at_or_above_one() -> None:
    """The over-estimates, rounded, with a thousands separator where needed."""
    assert TPM.sf1_ratio(170296 / 19193.984508514404) == "9"        # BI10
    assert TPM.sf1_ratio(97927 / 28782.920360565186) == "3"         # BI11
    assert TPM.sf1_ratio(5580034 / 22765.509366989136) == "245"     # BI3
    assert TPM.sf1_ratio(471369 / 44509.54818725586) == "11"        # BI4
    assert TPM.sf1_ratio(303037 / 13075.624227523804) == "23"       # BI7
    assert TPM.sf1_ratio(1278984344 / 51990.78273773193) == "24,600"  # BI17


def test_sf1_ratio_switches_register_exactly_at_one() -> None:
    assert TPM.sf1_ratio(0.9999) == "1.000"
    assert TPM.sf1_ratio(1.0) == "1"


def test_tex_num_leaves_four_digits_unseparated() -> None:
    """BI18's 5918 ms estimate is printed without a separator, on purpose."""
    assert TPM.tex_num(5918) == "5918"
    assert TPM.tex_num(9999) == "9999"
    assert TPM.tex_num(10000) == "10{,}000"
    assert TPM.tex_num(1278984344) == "1{,}278{,}984{,}344"
    assert TPM.tex_num(0) == "0"


def test_set_root_repoints_every_source(tmp_path: Path) -> None:
    """No source of record may be left behind by a re-rooting.

    A stale absolute path would make the generator read one tree's records
    against another tree's documents, and every assertion would still pass.
    """
    original = TPM.ROOT
    try:
        TPM.set_root(tmp_path)
        assert TPM.ROOT == tmp_path.resolve()
        paths = {name: value for name, value in vars(TPM).items()
                 if isinstance(value, Path) and name.isupper() and name != "ROOT"}
        assert paths, "the module should expose its sources as upper-case Path constants"
        for name, value in paths.items():
            assert value.is_absolute(), f"{name} is not absolute after set_root"
            assert value.is_relative_to(tmp_path.resolve()), f"{name} was left behind"
        assert TPM.OUT_DIR == tmp_path.resolve() / "paper" / "tgir"
    finally:
        TPM.set_root(original)
    assert TPM.ROOT == original


def test_set_root_is_idempotent() -> None:
    """Re-rooting twice must not compound: the relative spellings are fixed."""
    original = TPM.ROOT
    try:
        TPM.set_root(original)
        once = TPM.MEASURED
        TPM.set_root(original)
        assert TPM.MEASURED == once
    finally:
        TPM.set_root(original)


# A hand-built fixture, not read from any source of record: this table only
# checks the *rendering*, so the numbers just need to exercise every cell
# shape sf1_rows can carry (an admit, a refuse, and the unscoreable row with
# actual/ratio/rows all None).
_SF1_FIXTURE = [
    ("BI18", "admit", 5918, 29734.52877998352, 5918 / 29734.52877998352, 20, "false-admission"),
    ("BI10", "refuse", 170296, 19193.984508514404, 170296 / 19193.984508514404, 100, "true-rejection"),
    ("BI6", "refuse", 161120, None, None, None, "unscoreable"),
]


def test_vldb_style_keeps_the_same_numbers_as_arxiv() -> None:
    """--style vldb only changes the float wrapping and caption prose.

    Every number the table renders --- the actual data, never invented here
    --- must come out identical between styles: the vldb caption rewrite and
    the narrower single-column tabular must not touch a single cell value.
    """
    arxiv_tex = TPM.render_sf1_table(_SF1_FIXTURE, style="arxiv")
    vldb_tex = TPM.render_sf1_table(_SF1_FIXTURE, style="vldb")
    assert _numeric_cells(arxiv_tex) == _numeric_cells(vldb_tex)
    assert _numeric_cells(vldb_tex)  # the fixture actually exercises some cells


def test_vldb_style_float_is_single_column() -> None:
    """table* spans both acmart columns; vldb needs the narrower `table`."""
    arxiv_tex = TPM.render_sf1_table(_SF1_FIXTURE, style="arxiv")
    vldb_tex = TPM.render_sf1_table(_SF1_FIXTURE, style="vldb")
    assert "\\begin{table*}" in arxiv_tex
    assert "\\begin{table*}" not in vldb_tex
    assert "\\begin{table}" in vldb_tex


def test_vldb_caption_carries_none_of_the_banned_words() -> None:
    """PAPER_MAP.md's terminology sheet bans "arm", "campaign", "frozen",
    "bypassed-but-recording", "receipt" and "instrument" from reader-facing
    text.  The arxiv caption is the pre-existing draft prose and is checked
    first to confirm the fixture actually exercises the banned vocabulary --
    otherwise the vldb-side assertion would pass vacuously.
    """
    arxiv_caption = _caption_line(TPM.render_sf1_table(_SF1_FIXTURE, style="arxiv")).lower()
    hit = [w for w in BANNED_CAPTION_WORDS if w in arxiv_caption]
    assert hit, "fixture is stale: the arxiv caption no longer uses any banned word"

    vldb_caption = _caption_line(TPM.render_sf1_table(_SF1_FIXTURE, style="vldb")).lower()
    hit = [w for w in BANNED_CAPTION_WORDS if w in vldb_caption]
    assert not hit, f"vldb caption still carries banned word(s): {hit}"


# A hand-built ladder fixture exercising both renamed labels: the core rung
# ("R1, R2, R3, R3b, R5" -> "A1 to A3b, A5") and the targeted rung ("TGIR-v1"
# -> "TGIR").  Not read from any source of record.
_LADDER_FIXTURE = [
    {"rung": "v1-core (R1, R2, R3, R3b, R5)", "yes_count": 16, "partial_columns": 0,
     "partial_rows": 8, "no_count": 28, "unlocked": 16},
    {"rung": "+ var-length-unbounded", "yes_count": 28, "partial_columns": 1,
     "partial_rows": 9, "no_count": 14, "unlocked": 29},
    {"rung": "everything", "yes_count": 52, "partial_columns": 0,
     "partial_rows": 0, "no_count": 0, "unlocked": 52},
]


def test_vldb_ladder_renames_rules_and_drops_tgir_v1() -> None:
    """A review-round note: the draft calls these adjudication rules A1-A7,

    never the derivation's own R1-R7 "rulings", and the system is "TGIR",
    never "TGIR-v1".  Checked against the arxiv rendering first so the
    assertion is not vacuous, then against numeric identity between styles.
    """
    arxiv_tex = TPM.render_ladder_table(_LADDER_FIXTURE, style="arxiv")
    vldb_tex = TPM.render_ladder_table(_LADDER_FIXTURE, style="vldb")

    assert _BARE_RULE_LETTER.search(arxiv_tex), "fixture is stale: no bare rule letter in arxiv"
    assert "TGIR-v1" in arxiv_tex, "fixture is stale: no TGIR-v1 in arxiv"
    assert "ruling" in arxiv_tex.lower(), "fixture is stale: no 'ruling' in arxiv"

    assert not _BARE_RULE_LETTER.search(vldb_tex), \
        f"vldb ladder still carries a bare rule letter: {_BARE_RULE_LETTER.search(vldb_tex)}"
    assert "TGIR-v1" not in vldb_tex
    assert "ruling" not in vldb_tex.lower()
    assert "A1 to A3b, A5" in vldb_tex
    assert "adjudication rule A4" in vldb_tex

    assert _numeric_cells(arxiv_tex) == _numeric_cells(vldb_tex)


_DEMAND_FIXTURE_CORE = [
    "Aggregate", "Project", "Expand", "NodeScan", "Order", "Limit",
    "PropertyPredicate", "TypeConstraint", "Filter", "PatternMatch", "Join", "EdgeScan",
]
_DEMAND_FIXTURE_COUNTS = {
    "Aggregate": 41, "Project": 41, "Expand": 40, "NodeScan": 40, "Order": 38,
    "Limit": 36, "PropertyPredicate": 36, "TypeConstraint": 34, "Filter": 22,
    "PatternMatch": 19, "Join": 15, "EdgeScan": 12,
}


def test_vldb_demand_table_renames_rules() -> None:
    """The same A1-A7 rename applies to tab-demand's "rulings~R1 and~R3/R3b"."""
    arxiv_tex = TPM.render_demand_table(_DEMAND_FIXTURE_COUNTS, _DEMAND_FIXTURE_CORE,
                                        32, 26, style="arxiv")
    vldb_tex = TPM.render_demand_table(_DEMAND_FIXTURE_COUNTS, _DEMAND_FIXTURE_CORE,
                                       32, 26, style="vldb")

    assert _BARE_RULE_LETTER.search(arxiv_tex), "fixture is stale: no bare rule letter in arxiv"
    assert "ruling" in arxiv_tex.lower(), "fixture is stale: no 'ruling' in arxiv"

    assert not _BARE_RULE_LETTER.search(vldb_tex), \
        f"vldb demand table still carries a bare rule letter: {_BARE_RULE_LETTER.search(vldb_tex)}"
    assert "ruling" not in vldb_tex.lower()
    assert "adjudication rules A1 and A3/A3b" in vldb_tex

    assert _numeric_cells(arxiv_tex) == _numeric_cells(vldb_tex)


_MAIN_FIXTURE_MEASURED = {"totals": {"rows": 4, "predicted_unlocked": 4, "delivered": 4,
                                      "scoreable_rows": 4, "over_delivered": 0}}
_MAIN_FIXTURE_SUITES = ["ldbc-is"]
_MAIN_FIXTURE_STATS = {"ldbc-is": (4, 4, 4)}
_MAIN_FIXTURE_LABELS = {"ldbc-is": "LDBC Interactive Short"}


def test_vldb_drops_the_pre_registration_anchors_from_captions() -> None:
    """A review-round note: Sec 9.1 states \\tgSpecAnchor and \\tgSfOneCommit

    once, so the per-table vldb captions must not restate them -- only the
    pre-registration/campaign date stays.  arxiv keeps both anchors.
    """
    main_arxiv = _caption_line(TPM.render_main_table(
        _MAIN_FIXTURE_MEASURED, _MAIN_FIXTURE_STATS, _MAIN_FIXTURE_SUITES,
        _MAIN_FIXTURE_LABELS, style="arxiv"))
    main_vldb = _caption_line(TPM.render_main_table(
        _MAIN_FIXTURE_MEASURED, _MAIN_FIXTURE_STATS, _MAIN_FIXTURE_SUITES,
        _MAIN_FIXTURE_LABELS, style="vldb"))
    assert "\\tgSpecAnchor" in main_arxiv, "fixture is stale: no tgSpecAnchor in arxiv tab-main"
    assert "\\tgSpecAnchor" not in main_vldb
    assert "\\tgFreezeDate" in main_vldb

    sf1_arxiv = _caption_line(TPM.render_sf1_table(_SF1_FIXTURE, style="arxiv"))
    sf1_vldb = _caption_line(TPM.render_sf1_table(_SF1_FIXTURE, style="vldb"))
    assert "\\tgSfOneCommit" in sf1_arxiv, "fixture is stale: no tgSfOneCommit in arxiv tab-sf1"
    assert "\\tgSfOneCommit" not in sf1_vldb
    assert "\\tgSfOneDate" in sf1_vldb
