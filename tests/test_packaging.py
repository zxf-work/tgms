"""The version is declared in three places and must agree in all three.

`tgms.__version__` was the literal `"0.1.0"` from the first release through
v0.4.0 — four releases in which `pip show tgms` and `tgms.__version__`
disagreed — because nothing read it at build time and nothing tested it. It
now comes from the installed distribution's metadata, so it cannot drift
from `pyproject.toml` at all; what remains drift-prone is the *other* two
hand-written declarations, and that is what this file pins.

Deliberately a file-vs-file check. Comparing against installed metadata
would fail on an editable install whose wheel metadata predates a version
bump — true, but a fact about the venv rather than about the repository.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def pyproject_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_the_citation_file_declares_the_packaged_version():
    """CITATION.cff is what a reader cites; a stale one misattributes the
    work to a release that did not contain it."""
    cff = (ROOT / "CITATION.cff").read_text()
    m = re.search(r"^version:\s*(\S+)\s*$", cff, re.MULTILINE)
    assert m, "CITATION.cff has no version field"
    assert m.group(1) == pyproject_version()


def test_the_changelog_leads_with_the_packaged_version():
    """The newest entry is the release being prepared or the one just cut.
    A version bumped without an entry is how v0.4.0 shipped with no
    changelog at all."""
    head = (ROOT / "CHANGELOG.md").read_text().splitlines()
    versions = [ln for ln in head if ln.startswith("## v")]
    assert versions, "CHANGELOG.md has no version headings"
    assert versions[0].startswith(f"## v{pyproject_version()} "), (
        f"newest changelog entry is {versions[0]!r}, "
        f"pyproject is {pyproject_version()!r}")


def test_the_import_does_not_restate_the_version():
    """The regression that motivated this file: a hand-written literal in
    `tgms/__init__.py` cannot be kept in step, so there must not be one."""
    src = (ROOT / "tgms" / "__init__.py").read_text()
    assert not re.search(r'^__version__\s*=\s*["\']', src, re.MULTILINE), (
        "tgms/__init__.py assigns a literal __version__; read it from the "
        "installed distribution instead")
