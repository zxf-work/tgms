"""`scripts/tgir_paper_figures.py`: the TGIR VLDB paper's figure generator.

The generator is self-checking (it runs the same `eq`/`require` pattern
`scripts/tgir_paper_macros.py` uses and refuses to write on any failed
assertion), so this file does not re-derive its numbers. What it pins is the
contract a paper build and a source reviewer both depend on:

  - the three PDFs land where `--out` says, at the acmart single-column size
    (3.3in x 2.2in -- 237.6 x 158.4 pt);
  - `figures.json`'s admission points are not a re-typed summary -- they are
    the *same* dicts as `e14-p3-frontier.json`'s own `per_plan` records, so a
    reviewer of the source can trace every plotted point back to its record
    without trusting the plotting code in between;
  - when `benchmarks/ldbc-ref-v1/{tgms,neo4j}-campaign.json` do not exist
    (true as of this writing -- the LDBC reference campaign has not landed),
    `fig-neo4j.pdf` is the documented placeholder and `figures.json` says so,
    rather than the script failing or silently fabricating a comparison.

Every test that actually runs the generator needs matplotlib in the
interpreter running pytest (the generator imports it lazily, inside
`main()` via `_require_matplotlib()`, but still needs it to render); those
are marked `@requires_matplotlib` and skip individually rather than via a
module-level `pytest.importorskip`, which would make pytest report "no
tests collected" (exit 5) when this file is the only one selected -- a
result some CI treats as a failure rather than a skip. One test,
`test_module_importable_without_matplotlib`, runs unconditionally: it pins
the lazy-import contract itself, so it must run in exactly the interpreter
that lacks matplotlib (the project .venv, today) to mean anything.
"""

from __future__ import annotations

import importlib
import json
import re
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "tgir_paper_figures.py"


def _matplotlib_available() -> bool:
    try:
        import matplotlib  # noqa: F401
    except ModuleNotFoundError:
        return False
    return True


requires_matplotlib = pytest.mark.skipif(
    not _matplotlib_available(),
    reason="matplotlib not importable in this interpreter -- see "
           "scripts/tgir_paper_figures.py's docstring for how to get it "
           "without `uv pip install`-ing it into the project .venv",
)


_PAREN_STRING = re.compile(rb"\((?:[^()\\]|\\.)*\)", re.S)
_STREAM = re.compile(rb"stream\r?\n(.*?)endstream", re.S)


def _extract_pdf_text(path: Path) -> str:
    """A small, dependency-free PDF text extractor, good enough to find a
    literal caption string in a matplotlib-emitted PDF.  matplotlib's PDF
    backend embeds a Type0/CID font and shows text as UTF-16BE-ish string
    literals -- each character `c` as `\\x00c` -- inside `Tj`/`TJ` operators,
    in content streams that are, by default, Flate-compressed. This
    decompresses every stream (best-effort; non-text streams such as the
    embedded font's binary just fail to decompress usefully and are
    skipped), pulls out every parenthesized string literal, strips the
    interleaved null bytes, and concatenates the result in document order.
    """
    raw = path.read_bytes()
    chunks: list[bytes] = []
    for m in _STREAM.finditer(raw):
        blob = m.group(1)
        try:
            blob = zlib.decompress(blob)
        except zlib.error:
            pass  # not Flate-compressed (or not text) -- search it as-is
        for s in _PAREN_STRING.finditer(blob):
            chunks.append(s.group(0)[1:-1].replace(b"\x00", b""))
    return b"".join(chunks).decode("latin-1", errors="replace")


def _run(tmp_path: Path) -> tuple[subprocess.CompletedProcess, Path]:
    out_dir = tmp_path / "figs"
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(ROOT), "--out", str(out_dir)],
        capture_output=True, text=True,
    )
    return proc, out_dir


def test_module_importable_without_matplotlib() -> None:
    """scripts/tgir_paper_figures.py must import cleanly with no matplotlib
    in the interpreter (true of the project .venv today) -- only `main()`,
    via `_require_matplotlib()`, actually needs it. Deliberately not gated
    by `requires_matplotlib`: this is the one test that means something
    specifically when matplotlib is absent, and it must also keep passing
    when matplotlib happens to be present (the lazy-import contract holds
    either way)."""
    sys.path.insert(0, str(ROOT / "scripts"))
    mod = importlib.import_module("tgir_paper_figures")
    assert callable(mod._require_matplotlib)
    assert hasattr(mod, "plt")  # None until _require_matplotlib() runs


@requires_matplotlib
def test_writes_three_pdfs_and_figures_json(tmp_path: Path) -> None:
    proc, out_dir = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    for name in ("fig-admission.pdf", "fig-cost.pdf", "fig-neo4j.pdf", "figures.json"):
        assert (out_dir / name).exists(), f"{name} missing; stderr:\n{proc.stderr}"


@requires_matplotlib
def test_pdfs_are_acmart_single_column_sized(tmp_path: Path) -> None:
    """3.3in x 2.2in, in PDF points (1 pt = 1/72 in): 237.6 x 158.4 pt."""
    proc, out_dir = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    for name in ("fig-admission.pdf", "fig-cost.pdf", "fig-neo4j.pdf"):
        raw = (out_dir / name).read_bytes()
        m = None
        for line in raw.split(b"\n"):
            if b"/MediaBox" in line:
                m = line
                break
        assert m is not None, f"{name}: no /MediaBox found"
        nums = [float(x) for x in m.split(b"[")[1].split(b"]")[0].split()]
        _, _, width, height = nums
        assert width == pytest.approx(237.6, abs=0.5), f"{name}: width {width}pt"
        assert height == pytest.approx(158.4, abs=0.5), f"{name}: height {height}pt"


@requires_matplotlib
def test_admission_points_equal_the_source_record(tmp_path: Path) -> None:
    proc, out_dir = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr

    figures = json.loads((out_dir / "figures.json").read_text(encoding="utf-8"))
    frontier = json.loads(
        (ROOT / "benchmarks" / "results-v1" / "e14-p3-frontier.json").read_text(encoding="utf-8"))

    admission = figures["fig-admission"]
    scored_src = {r["plan_id"]: r for r in frontier["arms"]["scored-bi"]["per_plan"]}
    char_src = {r["plan_id"]: r for r in frontier["arms"]["characterization-interactive"]["per_plan"]}

    scored_json = {r["plan_id"]: r for r in admission["scored"]}
    char_json = {r["plan_id"]: r for r in admission["characterization"]}

    assert set(scored_json) == set(scored_src)
    assert set(char_json) == set(char_src)
    for pid, rec in scored_json.items():
        assert rec == scored_src[pid], f"scored {pid}: figures.json diverges from the record"
    for pid, rec in char_json.items():
        assert rec == char_src[pid], f"characterization {pid}: figures.json diverges from the record"

    # the single false admission the figure highlights
    assert admission["false_admission_plan"] == "BI18"
    assert scored_src["BI18"]["classifier"] == "false-admission"
    only_false_admission = [pid for pid, r in scored_src.items()
                            if r["classifier"] == "false-admission"]
    assert only_false_admission == ["BI18"]

    # the admission ceiling the figure draws as a vertical line
    assert admission["ceiling_ms"] == frontier["manifest"]["budget_ms"]


@requires_matplotlib
def test_neo4j_figure_is_the_placeholder_when_records_are_absent(tmp_path: Path) -> None:
    """As of this writing benchmarks/ldbc-ref-v1/{tgms,neo4j}-campaign.json do
    not exist (only campaign.yaml / RUNBOOK.md / sort_keys.yaml do) -- the
    reader must be tolerant of that and the paper must still build."""
    ref_dir = ROOT / "benchmarks" / "ldbc-ref-v1"
    assert not (ref_dir / "tgms-campaign.json").exists()
    assert not (ref_dir / "neo4j-campaign.json").exists()

    proc, out_dir = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr

    figures = json.loads((out_dir / "figures.json").read_text(encoding="utf-8"))
    neo4j = figures["fig-neo4j"]
    assert neo4j["available"] is False
    assert neo4j["tgms"] == {}
    assert neo4j["neo4j"] == {}
    assert neo4j["common_plan_ids"] == []

    text = _extract_pdf_text(out_dir / "fig-neo4j.pdf")
    assert "NEED EXPERIMENTAL RESULT: ldbc-ref-v1" in text, text


@requires_matplotlib
def test_summary_lines_printed_for_each_figure(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path)
    assert proc.returncode == 0, proc.stderr
    for stem in ("fig-admission.pdf", "fig-cost.pdf", "fig-neo4j.pdf"):
        assert stem in proc.stdout, f"no summary line for {stem} in stdout:\n{proc.stdout}"
