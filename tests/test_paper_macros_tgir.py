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
BANNED_CAPTION_WORDS = (
    "arm", "campaign", "frozen", "bypassed-but-recording", "receipt", "instrument",
)


def _caption_line(tex: str) -> str:
    cap_lines = [ln for ln in tex.splitlines() if ln.startswith("\\caption{")]
    assert len(cap_lines) == 1, "expected exactly one caption line in the rendered table"
    return cap_lines[0]


def _numeric_cells(tex: str) -> list[str]:
    """The tabular body's numeric tokens, in order, ignoring float/caption prose."""
    lines = tex.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == "\\midrule") + 1
    end = next(i for i, ln in enumerate(lines) if ln.strip() == "\\bottomrule")
    body = "\n".join(lines[start:end])
    return re.findall(r"-?\d[\d,{}]*(?:\.\d+)?", body)


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
