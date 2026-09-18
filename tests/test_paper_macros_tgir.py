"""`scripts/tgir_paper_macros.py`'s pure helpers and its `--root` re-rooting.

The generator itself is self-checking — it runs 759 assertions over the
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

import ast
import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

import yaml

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


def test_is2_macro_values_pin_the_ldbc_short_read() -> None:
    """IS2's paper-cited numbers, recomputed from the checked-in benchmark
    records --- no docs/design/ needed, unlike most of this generator's
    sources.  \\tgIsTwoActualMs is 12,307 ms (rounded), \\tgIsTwoRows is 10,
    \\tgIsTwoEstMs is 840, and the characterization arm's guard classifier for
    IS2 is a false admission (the guard would have admitted a plan that in
    fact cost 12.3 s against an 840 ms estimate).
    """
    campaign = json.loads(TPM.SF1_CAMPAIGN.read_text(encoding="utf-8"))
    is2 = next(r for r in campaign["records"] if r["plan_id"] == "IS2")
    assert is2["arm"] == "characterization-interactive"
    assert is2["outcome"] == "COMPLETED"
    assert is2["rows"] == 10
    assert TPM.tex_num(round(is2["ms"])) == "12{,}307"
    assert TPM.tex_num(is2["estimate"]["time_est_ms"]) == "840"

    frontier = json.loads(TPM.FRONTIER.read_text(encoding="utf-8"))
    char_plans = frontier["arms"]["characterization-interactive"]["per_plan"]
    is2_classifier = next(p["classifier"] for p in char_plans if p["plan_id"] == "IS2")
    assert is2_classifier == "false-admission"


def test_ref_v1_macro_values_pin_the_external_baseline() -> None:
    """The \\tgRef* family, recomputed from the checked-in ldbc-ref-v1 records
    --- like the IS2 test above, this needs no docs/design/.

    2026-09-18 is the revision of record: the 7 templates whose reference side
    was invalid on 2026-09-17 (BI4 BI9 BI11 BI12 IC2 IC5 IC9) were re-run with
    valid temporal parameters.  Five of them (BI9 BI11 BI12 IC9, plus BI4 on
    99/100 rows) turned out to agree; the sixth, IC2, turned out to hide the
    same KNOWS both-ways defect IS3 already showed.  What is worth pinning
    here is not arithmetic but *which* arithmetic: the six verdict classes
    have to partition the 24 templates with the timeout counted from the TGIR
    outcome rather than from a verdict (the comparator never saw a row dump
    for it), the row fraction has to be taken over the 23 templates that
    produced a comparison rather than over all 24 (only BI6.v2 has none), and
    the Neo4j figure has to be the median of the three *timed* runs with the
    warm-up excluded.  Each of those is a plausible wrong reading, and each
    would still produce a number.
    """
    compare = json.loads(TPM.REF_COMPARE.read_text(encoding="utf-8"))
    timings = json.loads(TPM.REF_TIMINGS.read_text(encoding="utf-8"))
    manifest = json.loads(TPM.REF_MANIFEST.read_text(encoding="utf-8"))
    campaign = yaml.safe_load(TPM.REF_CAMPAIGN_YAML.read_text(encoding="utf-8"))

    # the revision of record supersedes 2026-09-17, which is kept, not deleted
    assert compare["manifest"]["supersedes"] == "compare-2026-09-17.json"
    assert TPM.REF_COMPARE_SUPERSEDED.exists()
    assert TPM.REF_TIMINGS_SUPERSEDED.exists()
    assert TPM.REF_MANIFEST_SUPERSEDED.exists()

    verdicts = compare["verdicts"]
    entries = {e["template"]: e for e in timings["templates"]}
    assert len(verdicts) == compare["manifest"]["plans"] == 24      # \tgRefTemplates
    assert sorted(entries) == sorted(v["plan_id"].split(".")[0] for v in verdicts)

    classes = Counter(v.get("verdict") for v in verdicts)
    counts = campaign["addendum_3"]["verdict_counts"]
    outcomes = Counter(e["tgir"]["outcome"] for e in timings["templates"])
    assert classes["agreeing"] == counts["agree"] == 18             # \tgRefAgree
    assert (classes["reference-column-not-projected"]
            == counts["reference_column_not_projected"] == 3)       # \tgRefRefColNotProjected
    assert classes["disagreeing"] == counts["disagree"] == 2        # \tgRefDisagree
    assert counts["not_comparable"] == 0                            # \tgRefNotComparable
    assert outcomes["TIMEOUT"] == counts["timeout"] == 1            # \tgRefTimeout
    assert outcomes["ERRORED"] == counts["error"] == 0              # \tgRefError
    assert 18 + 3 + 0 + 2 + 1 + 0 == len(verdicts)

    # the comparable subset (only BI6.v2 never produced a verdict), and the
    # row fraction over exactly it
    comparable = [v for v in verdicts if v.get("verdict") is not None]
    assert len(comparable) == 23                                    # \tgRefComparable
    rows_agree = sum(v["agreeing"] for v in comparable)
    rows_total = sum(v["compared"] for v in comparable)
    assert (rows_agree, rows_total) == (678, 736)                   # \tgRefRows{Agree,Total}
    assert f"{rows_agree / rows_total:.3f}" == "0.921"              # \tgRefRowsAgreeFrac
    assert round(rows_agree / rows_total, 4) == campaign["addendum_3"]["row_agreement"]["ratio"]
    assert rows_agree == sum(v["agreeing"] for v in verdicts)
    # pooling BI6.v2 in would be the wrong reading (it contributes 0 either way)
    assert sum(v["compared"] for v in verdicts) == rows_total

    disagreeing = {v["plan_id"].split(".")[0]: v for v in comparable if v.get("verdict") == "disagreeing"}
    assert sorted(disagreeing) == ["IC2", "IS3"]                    # \tgRefDisagreeRows
    assert "IS3" in disagreeing and "IC2" in disagreeing            # \tgRefDisagreeRow (IS3)
    assert entries["IS3"]["tgir"]["rows"] == 48                     # \tgRefIsThreeTgir
    assert entries["IS3"]["neo4j"]["rows"] == 24                    # \tgRefIsThreeNeo
    assert entries["IS3"]["tgir"]["rows"] == 2 * entries["IS3"]["neo4j"]["rows"]

    # IC2: newly visible at 09-18 -- 20 rows on both sides, but only 10 of
    # TGIR's are distinct (the LIMIT 20 hides the KNOWS both-ways doubling as
    # a row count rather than a 2x row-count blowup the way IS3 shows it)
    assert entries["IC2"]["tgir"]["rows"] == 20                     # \tgRefIcTwoTgir
    assert entries["IC2"]["neo4j"]["rows"] == 20                    # \tgRefIcTwoNeo
    ic2_rows = json.loads(TPM.REF_TGMS_IC2_ROWS.read_text(encoding="utf-8"))["rows"]
    assert len(ic2_rows) == 20
    assert len({r["messageId"] for r in ic2_rows}) == 10            # \tgRefIcTwoDistinct

    # BI4: reference-column-not-projected, but on the 2 columns it does
    # project, 99 of its 100 rows agree exactly -- the one exception is a
    # genuine count mismatch (419 vs 435) for the same person, structured in
    # compare's own `causes`
    bi4 = next(v for v in verdicts if v["plan_id"] == "BI4")
    tgms_only = next(c["detail"] for c in bi4["causes"] if c["detail"].startswith("tgms-only row:"))
    ref_only = next(c["detail"] for c in bi4["causes"] if c["detail"].startswith("reference-only row:"))
    tgms_row = ast.literal_eval(tgms_only.split("tgms-only row: ", 1)[1])
    ref_row = ast.literal_eval(ref_only.split("reference-only row: ", 1)[1])
    assert tgms_row["personId"] == ref_row["personId"]
    assert tgms_row["messageCount"] == 419                          # \tgRefBiFourTgirCount
    assert ref_row["messageCount"] == 435                           # \tgRefBiFourNeoCount

    timed_out = [e for e in timings["templates"] if e["tgir"]["outcome"] == "TIMEOUT"]
    assert [e["tgir_plan"] for e in timed_out] == ["BI6.v2.json"]   # \tgRefTimeoutRow
    ceilings = manifest["protocol"]["ceilings"]
    assert (ceilings["tgir_bypass_ceiling_s"]
            + ceilings["tgir_child_open_allowance_s"]) == 1020      # \tgRefTimeoutCeilingS

    families = Counter(v["plan_id"][:2] for v in verdicts)
    agreeing = Counter(v["plan_id"][:2] for v in verdicts if v.get("verdict") == "agreeing")
    assert (families["BI"], families["IC"], families["IS"]) == (10, 7, 7)  # \tgRef{Bi,Ic,Is}
    assert (agreeing["BI"], agreeing["IC"], agreeing["IS"]) == (8, 4, 6)   # \tgRefAgree{Bi,Ic,Is}
    assert sum(families.values()) == 24 and sum(agreeing.values()) == 18

    # not_comparable is 0 in every group at this revision -- the 09-17 defect
    # it used to count is exactly what the 09-18 re-run fixed
    notcomp_any = sum(1 for v in verdicts if v.get("verdict") == "disagreeing"
                      and v["plan_id"].split(".")[0] not in disagreeing)
    assert notcomp_any == 0                                          # \tgRefNotComparable{Bi,Ic,Is}

    # per-group row agreement (new at 09-18), cross-checked against
    # campaign.yaml addendum_3's own by_group block
    by_group = campaign["addendum_3"]["row_agreement"]["by_group"]
    group_rows: dict[str, tuple[int, int]] = {}
    for g in ("BI", "IC", "IS"):
        members = [v for v in comparable if v["plan_id"][:2] == g]
        group_rows[g] = (sum(v["agreeing"] for v in members), sum(v["compared"] for v in members))
    assert group_rows["BI"] == (606, 607)                            # \tgRefRowsAgreeBi / TotalBi
    assert group_rows["IC"] == (56, 66)                              # \tgRefRowsAgreeIc / TotalIc
    assert group_rows["IS"] == (16, 63)                              # \tgRefRowsAgreeIs / TotalIs
    for g, (agree, total) in group_rows.items():
        assert f"{agree}/{total}" == by_group[g]["rows"]
        assert round(agree / total, 4) == by_group[g]["ratio"]

    # the KNOWS both-ways defect behind both disagreements: nine plan
    # artifacts expand KNOWS with dir="both" (README §5.5, ledger D-090)
    def _expands_knows_both(node) -> bool:
        if isinstance(node, dict):
            if (node.get("op") == "Expand" and node.get("dir") == "both"
                    and node.get("rel_type") == "KNOWS"):
                return True
            return any(_expands_knows_both(v) for v in node.values())
        if isinstance(node, list):
            return any(_expands_knows_both(item) for item in node)
        return False

    knows_both = {p.stem for p in TPM.PLANS_DIR.glob("*.json")
                  if _expands_knows_both(json.loads(p.read_text(encoding="utf-8")).get("root", {}))}
    assert knows_both == {"BI10", "IC2", "IC5", "IC6", "IC9", "IC11", "IC12", "IS3", "IS7"}
    assert len(knows_both) == 9                                      # \tgRefKnowsBothPlans

    # ops/failure_ledger.jsonl D-090, read from this script's own checkout
    # (FAILURE_LEDGER is fixed to it, immune to --root)
    assert TPM.FAILURE_LEDGER.exists()
    ledger = [json.loads(line) for line in
              TPM.FAILURE_LEDGER.read_text(encoding="utf-8").splitlines() if line.strip()]
    d090 = [e for e in ledger if e.get("id", "").startswith("D-090")]
    assert len(d090) == 1
    assert d090[0]["id"] == "D-090-is3-knows-both-ways-double-count"  # \tgRefLedgerId
    assert "IS3 and IC2" in d090[0]["root_cause"]

    # the median of t1/t2/t3 --- never the warm-up, which is systematically slower
    median = {t: statistics.median([e["neo4j"]["wall_s"][k] for k in ("t1", "t2", "t3")])
              for t, e in entries.items()}
    assert f"{min(median.values()):.3f}" == "0.008"                 # \tgRefNeoMinS
    assert f"{max(median.values()):.3f}" == "24.754"                # \tgRefNeoMaxS
    warmups = {t: e["neo4j"]["wall_s"]["warmup"] for t, e in entries.items()}
    assert median != warmups

    ms = {t: e["tgir"]["ms"] for t, e in entries.items() if e["tgir"]["ms"] is not None}
    assert len(ms) == 23
    assert f"{min(ms.values()) / 1000:.1f}" == "0.1"                # \tgRefTgirMinS
    assert f"{max(ms.values()) / 1000:.1f}" == "148.1"              # \tgRefTgirMaxS
    assert manifest["config"]["neo4j_version"] == "5.26.0"          # \tgRefNeoVersion
    # no import wall is recorded in the manifest, so no macro is emitted for one
    assert "import_wall_s" not in manifest["config"]

    assert f"{median['IS2']:.3f}" == "0.016"                        # \tgRefIsTwoNeoMedianS
    assert f"{ms['IS2'] / 1000:.1f}" == "6.7"                       # \tgRefIsTwoTgirS


def test_char_rerun_macro_values_pin_the_reproduction() -> None:
    """The \\tgSfOneChar* family: the corrected interactive-set reproduction
    against the original campaign's 11 Interactive rows.

    The denominator is the trap.  The reproduction ran 14 plans (the original
    11 plus the three post-freeze IS1/IS4/IS5 rows), but only 11 of them have
    anything to be compared against, so the row-count and wall-ratio claims
    are over 11 while the completion claim is over 14.
    """
    rerun = json.loads(TPM.SF1_CHAR_RERUN.read_text(encoding="utf-8"))
    original = json.loads(TPM.SF1_CAMPAIGN.read_text(encoding="utf-8"))

    assert rerun["manifest"]["commit"] == "54dcab03b311"      # \tgSfOneCharRerunCommit
    assert rerun["manifest"]["utc"].split("T")[0] == "2026-09-16"  # \tgSfOneCharRerunDate
    assert rerun["manifest"]["supersedes"]["arm"] == "characterization-interactive"

    records = {r["plan_id"]: r for r in rerun["records"]}
    completed = [r for r in rerun["records"] if r["outcome"] == "COMPLETED"]
    assert len(completed) == len(records) == 14              # \tgSfOneCharCompleted

    originals = {r["plan_id"]: r for r in original["records"]
                 if r["arm"] == "characterization-interactive"}
    assert len(originals) == 11
    assert set(originals) < set(records)
    assert sorted(set(records) - set(originals)) == ["IS1", "IS4", "IS5"]

    equal = sum(1 for p, o in originals.items() if records[p]["rows"] == o["rows"])
    assert equal == 11                                       # \tgSfOneCharCountEqual
    ratios = {p: records[p]["ms"] / o["ms"] for p, o in originals.items()}
    assert f"{min(ratios.values()):.2f}" == "0.07"           # \tgSfOneCharWallMin
    assert f"{max(ratios.values()):.2f}" == "0.96"           # \tgSfOneCharWallMax
    assert max(ratios.values()) < 1.0


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
                 if isinstance(value, Path) and name.isupper() and name != "ROOT"
                 # FAILURE_LEDGER is deliberately fixed to this script's own
                 # checkout (like osdi_paper_macros.py's constant of the same
                 # name) -- ops/failure_ledger.jsonl is a public-worktree
                 # file, not one --root's docs/paper indirection reaches.
                 and name != "FAILURE_LEDGER"}
        assert paths, "the module should expose its sources as upper-case Path constants"
        for name, value in paths.items():
            assert value.is_absolute(), f"{name} is not absolute after set_root"
            assert value.is_relative_to(tmp_path.resolve()), f"{name} was left behind"
        assert TPM.OUT_DIR == tmp_path.resolve() / "paper" / "tgir"
        # the one deliberate exception: FAILURE_LEDGER stays put
        assert TPM.FAILURE_LEDGER == original / "ops" / "failure_ledger.jsonl"
        assert not TPM.FAILURE_LEDGER.is_relative_to(tmp_path.resolve())
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
