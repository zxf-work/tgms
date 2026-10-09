"""Tests for the paper figure set in ``scripts/sys_paper_figures.py``.

Three groups:

1. the macro parser reads every landed value, including thousand
   separators (``13{,}714``) and scientific notation
   (``1.55\\times 10^{-3}``), and refuses every non-numeric body by name;
2. each figure's data builder reads its numbers through the macros or the
   sha-gated records (a tampered record or a drifted frozen bar fails),
   and writes a CSV whose every row names its source;
3. when matplotlib is importable, each figure renders at its exact page
   size, with only embedded STIX TrueType fonts, no Type 3 font, panel
   tags as the only titles and no host name, codename, path or sha in
   any figure text. These are skipped with a reason when matplotlib is
   absent (the CSV half never needs it).
"""

from __future__ import annotations

import csv
import importlib.util
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

NEW_FIGURES = ("f_arcs", "f_scale", "f_ldbc", "f_open_cost", "f_correction_load",
               "f_durability", "f_external")


def _load():
    spec = importlib.util.spec_from_file_location(
        "sys_paper_figures", ROOT / "scripts" / "sys_paper_figures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _deliverable(mod, stem):
    for name, build, write_csv_fn, plot_fn, csv_filename in mod.DELIVERABLES:
        if csv_filename == f"{stem}.csv":
            return build, write_csv_fn, plot_fn
    raise AssertionError(f"{stem} is not a deliverable")


# --------------------------------------------------------------------------
# 1. the macro parser
# --------------------------------------------------------------------------

SYNTHETIC_MACROS = r"""
\providecommand{\rec}[1]{\csname rec#1\endcsname}
\expandafter\newcommand\csname recThousands\endcsname{13{,}714}          % a
\expandafter\newcommand\csname recThousandsFloat\endcsname{4{,}912.85}   % b
\expandafter\newcommand\csname recSci\endcsname{1.55\times 10^{-3}}      % c
\expandafter\newcommand\csname recSciPositive\endcsname{2.5\times 10^{4}} % d
\expandafter\newcommand\csname recThin\endcsname{1\,000}                 % e
\expandafter\newcommand\csname recPlain\endcsname{0.829}                 % f
\expandafter\newcommand\csname recNegative\endcsname{-3.4}               % g
\expandafter\newcommand\csname recText\endcsname{not measured}           % h
\expandafter\newcommand\csname recFlag\endcsname{true}                   % i
\expandafter\newcommand\csname recId\endcsname{D-090}                    % j
\expandafter\newcommand\csname recEscaped\endcsname{delete\_file}        % k
\expandafter\newcommand\csname recFraction\endcsname{89/710}             % l
\expandafter\newcommand\csname recPending\endcsname{\errmessage{sys_paper_macros: recPending is PENDING (lane (x)): {nested}}}  % m
\expandafter\newcommand\csname recAfterPending\endcsname{7}              % n
"""


@pytest.fixture()
def synthetic(tmp_path):
    path = tmp_path / "macros.tex"
    path.write_text(SYNTHETIC_MACROS, encoding="utf-8")
    return path


def test_macro_bodies_keep_balanced_inner_groups(synthetic):
    mod = _load()
    bodies = mod.macro_bodies(synthetic.read_text(encoding="utf-8"))
    assert bodies["recThousands"] == "13{,}714"
    assert bodies["recSci"] == "1.55\\times 10^{-3}"
    assert bodies["recPending"].startswith("\\errmessage{") and bodies["recPending"].endswith("}}")
    # a nested group inside one body does not swallow the next definition
    assert bodies["recAfterPending"] == "7"


def test_numeric_bodies_parse_to_numbers(synthetic):
    mod = _load()
    names = ["recThousands", "recThousandsFloat", "recSci", "recSciPositive", "recThin",
             "recPlain", "recNegative", "recAfterPending"]
    values = mod.load_macro_values(names, path=synthetic)
    assert values == {"recThousands": 13714.0, "recThousandsFloat": 4912.85,
                      "recSci": pytest.approx(1.55e-3), "recSciPositive": 25000.0,
                      "recThin": 1000.0, "recPlain": 0.829, "recNegative": -3.4,
                      "recAfterPending": 7.0}


@pytest.mark.parametrize("name", ["recText", "recFlag", "recId", "recEscaped", "recFraction",
                                  "recPending"])
def test_non_numeric_bodies_are_refused_by_name(synthetic, name):
    mod = _load()
    with pytest.raises(AssertionError, match=name):
        mod.load_macro_values([name], path=synthetic)


def test_missing_macro_is_refused(synthetic):
    mod = _load()
    with pytest.raises(AssertionError, match="recNowhere"):
        mod.load_macro_values(["recNowhere"], path=synthetic)


def test_text_macros_unescape_and_refuse_pending(synthetic):
    mod = _load()
    texts = mod.load_macro_texts(["recId", "recEscaped", "recFraction", "recThousands"],
                                 path=synthetic)
    assert texts == {"recId": "D-090", "recEscaped": "delete_file", "recFraction": "89/710",
                     "recThousands": "13,714"}
    with pytest.raises(AssertionError, match="recPending"):
        mod.load_macro_texts(["recPending"], path=synthetic)


def test_half_unit_follows_the_printed_precision():
    mod = _load()
    assert mod.macro_half_unit("x", "2.035") == pytest.approx(5e-4)
    assert mod.macro_half_unit("x", "13{,}714") == pytest.approx(0.5)
    assert mod.macro_half_unit("x", "8.51\\times 10^{-3}") == pytest.approx(5e-6)
    assert mod.macro_half_unit("x", "1.5") == pytest.approx(0.05)


def test_assert_matches_macro_uses_the_printed_precision(synthetic):
    mod = _load()
    mod.assert_matches_macro("recPlain", 0.82949, path=synthetic)
    mod.assert_matches_macro("recSci", 1.5549e-3, path=synthetic)
    with pytest.raises(AssertionError, match="recPlain"):
        mod.assert_matches_macro("recPlain", 0.8296, path=synthetic)


def test_every_landed_numeric_macro_in_the_generated_file_parses():
    """The old single-group regex stopped at the first '}' and failed on
    every thousand-separated or scientific value; the balanced parser reads
    them all. A PENDING or text macro is the only kind that may not parse."""
    mod = _load()
    if not mod.SYS_PAPER_MACROS.exists():
        pytest.skip("paper/sys/generated/sys-paper-macros.tex not generated in this checkout")
    bodies = mod.macro_bodies(mod.SYS_PAPER_MACROS.read_text(encoding="utf-8"))
    separated = {n: b for n, b in bodies.items() if "{,}" in b or "\\times" in b}
    assert len(separated) > 30
    for name, body in separated.items():
        if re.search(r"[A-Za-z]", body.replace("\\times", "")):
            continue  # a text macro that happens to carry a separator
        assert isinstance(mod.macro_number(name, body), float)
    values = mod.load_macro_values(["recSoakVerifyFullOverlapCount", "recExt2RatioArcFiveRecent",
                                    "recCrashWall"])
    assert values["recSoakVerifyFullOverlapCount"] == 13714.0
    assert values["recExt2RatioArcFiveRecent"] == pytest.approx(8.51e-3)
    assert values["recCrashWall"] == 4912.85


# --------------------------------------------------------------------------
# 2. data builders, CSVs, gates
# --------------------------------------------------------------------------

def _require_macros(mod):
    if not mod.SYS_PAPER_MACROS.exists():
        pytest.skip("paper/sys/generated/sys-paper-macros.tex not generated in this checkout")


@pytest.mark.parametrize("stem", NEW_FIGURES)
def test_figure_csv_rows_each_name_their_source(tmp_path, monkeypatch, stem):
    mod = _load()
    _require_macros(mod)
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    build, write_csv_fn, _plot = _deliverable(mod, stem)
    text = write_csv_fn(build())
    rows = list(csv.reader(text.splitlines()))
    assert rows[0] == mod.FIGURE_CSV_HEADER
    assert len(rows) > 2
    for row in rows[1:]:
        assert len(row) == len(rows[0])
        assert row[3] != "", f"{stem}: a row without a value: {row}"
        assert row[5] != "", f"{stem}: a row without a source: {row}"
    assert (tmp_path / f"{stem}.csv").read_text(encoding="utf-8") == text


def test_arcs_csv_carries_the_macro_values_and_the_record_bar(tmp_path, monkeypatch):
    mod = _load()
    _require_macros(mod)
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    data = mod.build_arcs_data()
    by_source = {r["source"]: r["value"] for r in data["rows"]}
    assert by_source["recSoakVerifyFullOverlapCount"] == 13714.0
    assert by_source["recStormV1CrossoverSpeedupSumN1k"] == pytest.approx(0.829)
    assert by_source["recStormV3ProbeSpeedup"] == pytest.approx(1.419)
    # the soak-2 writer bar is read from its record, not typed
    assert data["writer_bound"] == 50


def test_frozen_bars_are_verbatim_in_their_freeze_files():
    mod = _load()
    mod.check_frozen_bars()
    cited = [b for b in mod.FROZEN_BARS.values() if b["source"] is None]
    assert all("frozen" in b["citation"] for b in cited)


def test_a_drifted_frozen_bar_clause_fails(monkeypatch):
    mod = _load()
    bars = {k: dict(v) for k, v in mod.FROZEN_BARS.items()}
    bars["rollout_speedup_n1k_predicted"]["clause"] = "predicted: 4.3x (3.0-5.0)"
    monkeypatch.setattr(mod, "FROZEN_BARS", bars)
    with pytest.raises(AssertionError, match="rollout_speedup_n1k_predicted"):
        mod.check_frozen_bars()


def test_external_gate_refuses_a_tampered_record(tmp_path, monkeypatch):
    mod = _load()
    _require_macros(mod)
    tampered = tmp_path / mod.EXTERNAL_NEO4J.name
    shutil.copy(mod.EXTERNAL_NEO4J, tampered)
    tampered.write_text(tampered.read_text(encoding="utf-8").replace('"median": ', '"median":  ', 1),
                        encoding="utf-8")
    monkeypatch.setattr(mod, "EXTERNAL_NEO4J", tampered)
    with pytest.raises(AssertionError, match="sha256"):
        mod.build_external_data()


def test_external_medians_are_asserted_against_the_macros(tmp_path, monkeypatch):
    mod = _load()
    _require_macros(mod)
    data = mod.build_external_data()
    facets = {f["title"]: f for f in data["facets"]}
    assert set(facets) == {t for _s, _n, t, _k in mod._EXT_FACETS}
    college = facets["CollegeMsg, $N=10^3$"]
    assert len(college["series"][("neo", "after")]) == 9
    assert college["ratio"] == pytest.approx(1.67)
    probe = facets["synth, $N=10^4$"]
    assert len(probe["series"][("incr", "after")]) == 1
    # a macro that disagrees with the record must fail the build
    bad = tmp_path / "macros.tex"
    text = mod.SYS_PAPER_MACROS.read_text(encoding="utf-8")
    bad.write_text(text.replace("csname recExt1SpeedupLOneArcFiveProbe\\endcsname{8.51}",
                                "csname recExt1SpeedupLOneArcFiveProbe\\endcsname{8.61}"),
                   encoding="utf-8")
    monkeypatch.setattr(mod, "SYS_PAPER_MACROS", bad)
    with pytest.raises(AssertionError, match="recExt1SpeedupLOneArcFiveProbe"):
        mod.build_external_data()


def test_durability_strip_places_every_crash_boundary_once():
    mod = _load()
    _require_macros(mod)
    data = mod.build_durability_data()
    placed = [b for _s, bs in mod._PROTOCOL_STEPS for b in bs] + [b for _l, b in mod._SIDE_LANES]
    assert sorted(placed) == sorted(data["order"])
    assert len(placed) == len(set(placed)) == 10
    assert sum(sum(c.values()) for c in data["post"].values()) == 10_000
    assert all(c["SILENT"] == 0 for c in data["post"].values())


def test_scale_values_match_their_macros_and_record_bars():
    mod = _load()
    _require_macros(mod)
    data = mod.build_scale_data()
    assert data["band_h"] == [1.5, 5.0]
    assert data["forecast_h"] == (8.0, 10.0)
    assert data["rec_band_min"] == [29, 46]


# --------------------------------------------------------------------------
# 3. rendering (requires matplotlib)
# --------------------------------------------------------------------------

def _require_mpl(mod):
    if not mod.HAVE_MPL:
        pytest.skip("matplotlib is not installed in this interpreter; the figure script "
                    "falls back to CSV-only generation (see its HAVE_MPL guard)")


@pytest.mark.parametrize("stem", NEW_FIGURES)
def test_figure_renders_at_exact_size_with_embedded_stix_only(tmp_path, monkeypatch, stem):
    mod = _load()
    _require_mpl(mod)
    _require_macros(mod)
    monkeypatch.setattr(mod, "OUT_DIR", tmp_path)
    build, write_csv_fn, plot_fn = _deliverable(mod, stem)
    data = build()
    write_csv_fn(data)
    plot_fn(data)
    pdf = (tmp_path / f"{stem}.pdf").read_bytes()
    assert (tmp_path / f"{stem}.png").stat().st_size > 0
    box = re.search(rb"/MediaBox \[\s*0 0 ([\d.]+) ([\d.]+)\s*\]", pdf)
    w, h = mod.FIG_SIZES[stem]
    assert abs(float(box.group(1)) - w * 72) <= 0.5 and abs(float(box.group(2)) - h * 72) <= 0.5
    assert b"/Type3" not in pdf
    fonts = set(re.findall(rb"/BaseFont /(?:[A-Z]{6}\+)?([A-Za-z0-9-]+)", pdf))
    assert fonts and all(f.startswith(b"STIX") for f in fonts), fonts
    assert b"/FontFile2" in pdf  # embedded TrueType


@pytest.mark.parametrize("stem", NEW_FIGURES)
def test_figure_text_carries_no_title_host_codename_or_sha(monkeypatch, stem):
    mod = _load()
    _require_mpl(mod)
    _require_macros(mod)
    captured = {}
    monkeypatch.setattr(mod, "_savefig_paper",
                        lambda fig, path: captured.setdefault("fig", fig))
    build, _write, plot_fn = _deliverable(mod, stem)
    plot_fn(build())
    fig = captured["fig"]
    try:
        mod._assert_clean_figure(fig, stem)
        texts = mod.figure_texts(fig)
        assert texts
        for s in texts:
            assert not re.search(r"storm|tgms-L|\bB7\b|xzgpu|itiger", s, re.IGNORECASE), s
    finally:
        mod.plt.close(fig)


def test_figure_section_sets_no_font_below_six_points_and_no_font_family():
    source = (ROOT / "scripts" / "sys_paper_figures.py").read_text(encoding="utf-8")
    section = source[source.index("# Paper figure set (data plots)"):]
    sizes = [float(s) for s in re.findall(r"fontsize=([\d.]+)", section)]
    assert sizes and min(sizes) >= 6.0
    assert not re.search(r"fontfamily=|family=|fontname=", section)
    assert "bbox_inches" not in section and "suptitle(" not in section
