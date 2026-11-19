"""`tgms store verify` and `tgms check` on a store written by a newer build.

This build refuses to read a store a newer build wrote, rather than misread
it: a manifest whose format it does not know is refused at open, and a
segment whose header carries a key it does not read is refused when the
segment is decoded. Each refusal names one remedy (open the store with the
newer build, or rebuild it from its event log with `tgms replay`), and the
generic advice for damage ("restore the previous generation") is not added
to it.

Such a store is not damaged, and this build has not checked it, so verify
and check say "not checked" and exit 2, the code they already use for a
store they cannot check. A store that is damaged is still reported CORRUPT
with exit 1, also when a refused segment sits beside the damage.

The newer stores are made here from a store this build writes, the way the
engine's own segment tests make them
(`crates/tgms-engine-core/src/segment.rs`): rewrite a segment's header with
the column keys a newer build writes (`base`, `divisor`), keeping every data
byte, or set the current manifest's `format` to 4. Nothing written by another
build is needed.

The segment format number is the exception that runs the other way: no build
has written a segment format below the current one, so a lower number is
damage and is reported CORRUPT, while a higher one is a newer build's.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("tgms._engine", reason="native engine extension not built")

import tgms  # noqa: E402
from tgms._engine import FORMAT_VERSION, NEWER_WRITER_REMEDY  # noqa: E402
from tgms.cli import main  # noqa: E402

GENERIC_TAIL = "restore the previous generation"


def build(root: Path) -> Path:
    """A small native store with more than one segment. Returns its root, closed."""
    store = tgms.open(root, backend="native")
    for b in range(3):
        store.assert_edge(f"n{b}", f"n{b + 1}", "R", {"w": b},
                          vt_s=b, vt_e=b + 10, disc=f"#{b}")
    store.assert_node("n0", "N", {"p": 1}, vt_s=0, vt_e=50)
    store.close()
    return root


def segments(root: Path) -> list[Path]:
    segs = sorted((root / "native" / "seg").glob("*.tgs"))
    assert len(segs) >= 2, "expected the fixture to have written several segments"
    return segs


def align(n: int) -> int:
    return -(-n // 64) * 64


def rewrite_header_as_newer(seg: Path) -> None:
    """Give the first column the scale keys a newer build writes, keeping the
    preamble's format word and every data and footer byte (the engine's
    `rewrite_header` test helper, in Python)."""
    b = seg.read_bytes()
    header_len = int.from_bytes(b[8:12], "little")
    header = json.loads(b[12:12 + header_len])
    header["columns"][0]["base"] = 1000
    header["columns"][0]["divisor"] = 2
    new = json.dumps(header, separators=(",", ":")).encode()
    out = bytearray(b[:4] + b[4:8] + len(new).to_bytes(4, "little") + new)
    out += bytes(align(len(out)) - len(out))
    out += b[align(12 + header_len):]
    seg.write_bytes(bytes(out))


def set_manifest_format(root: Path, fmt: int) -> None:
    """The manifest `CURRENT` names, with its `format` set to `fmt`: what a
    build writing a newer manifest format leaves on disk, as this build sees
    it (the format is checked before the manifest's own digest)."""
    native = root / "native"
    gen = int((native / "CURRENT").read_text().split()[0])
    path = native / "manifests" / f"{gen:020}.json"
    doc = json.loads(path.read_text())
    doc["format"] = fmt
    path.write_text(json.dumps(doc, indent=2))


def set_segment_format(seg: Path, fmt: int) -> None:
    """Set the preamble's format word to `fmt`, keeping every other byte."""
    b = bytearray(seg.read_bytes())
    b[4:8] = fmt.to_bytes(4, "little")
    seg.write_bytes(bytes(b))


def flip_body_byte(seg: Path) -> None:
    """One byte inside the first column extent: the checksum path."""
    b = bytearray(seg.read_bytes())
    header_len = int.from_bytes(b[8:12], "little")
    b[align(12 + header_len) + 8] ^= 0xFF
    seg.write_bytes(bytes(b))


def run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str]:
    rc = main(argv)
    cap = capsys.readouterr()
    return rc, cap.out + cap.err


def assert_not_checked(rc: int, out: str) -> None:
    assert rc == 2, out
    assert "verdict: NOT CHECKED" in out, out
    assert "written by a newer TGMS build and was not checked by this build" in out, out
    assert "open it with that newer build" in out and "tgms replay" in out, out
    assert "CORRUPT" not in out, out
    assert GENERIC_TAIL not in out, out


# --- a newer segment header, manifest format 3 ---------------------------- #


def test_a_newer_segment_header_is_not_checked_not_corrupt(tmp_path, capsys):
    root = build(tmp_path / "s")
    for seg in segments(root):
        rewrite_header_as_newer(seg)

    for argv in (["store", "verify", "--store", str(root)],
                 ["store", "verify", "--store", str(root), "--full"],
                 ["check", str(root)]):
        rc, out = run(capsys, argv)
        assert_not_checked(rc, out)
        assert "REFUSED, WRITTEN BY A NEWER BUILD" in out, out
        assert "columns[].base, columns[].divisor" in out, out


def test_a_newer_segment_header_in_the_json_report(tmp_path, capsys):
    root = build(tmp_path / "s")
    for seg in segments(root):
        rewrite_header_as_newer(seg)

    for argv in (["store", "verify", "--store", str(root), "--json"],
                 ["check", str(root), "--json"]):
        assert main(argv) == 2
        report = json.loads(capsys.readouterr().out)
        assert report["readable"] is False
        assert report["written_by_newer_build"] is True
        assert "written by a newer TGMS build" in report["reason"]
        assert {f["kind"] for f in report["findings"]} == {"segment-unreadable"}
        assert all(NEWER_WRITER_REMEDY in f["detail"] for f in report["findings"])


def test_reading_a_newer_segment_names_only_its_own_remedy(tmp_path):
    root = build(tmp_path / "s")
    for seg in segments(root):
        rewrite_header_as_newer(seg)

    query = json.dumps({"src": "n0", "window": {"t_a": 0, "t_b": 100}})
    with pytest.raises(Exception) as ei:
        main(["call", "--store", str(root), "temporal_reachability", query])
    msg = str(ei.value)
    assert "which this build does not understand" in msg, msg
    assert msg.endswith(NEWER_WRITER_REMEDY), msg
    assert GENERIC_TAIL not in msg, msg


# --- the segment format number --------------------------------------------- #


def test_a_higher_segment_format_is_not_checked(tmp_path, capsys):
    root = build(tmp_path / "s")
    for seg in segments(root):
        set_segment_format(seg, FORMAT_VERSION + 1)

    for argv in (["store", "verify", "--store", str(root)], ["check", str(root)]):
        rc, out = run(capsys, argv)
        assert_not_checked(rc, out)
        assert f"segment format {FORMAT_VERSION + 1} is not supported" in out, out

    assert main(["store", "verify", "--store", str(root), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["written_by_newer_build"] is True


def test_a_lower_segment_format_is_corrupt(tmp_path, capsys):
    root = build(tmp_path / "s")
    set_segment_format(segments(root)[0], FORMAT_VERSION - 1)

    for argv in (["store", "verify", "--store", str(root)],
                 ["store", "verify", "--store", str(root), "--full"],
                 ["check", str(root)]):
        rc, out = run(capsys, argv)
        assert rc == 1, out
        assert "verdict: CORRUPT" in out, out
        assert f"segment format {FORMAT_VERSION - 1} is below {FORMAT_VERSION}" in out, out
        assert GENERIC_TAIL in out, out
        assert "NOT CHECKED" not in out and "newer TGMS build" not in out, out

    assert main(["store", "verify", "--store", str(root), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["readable"] is True
    assert report["healthy"] is False
    assert "written_by_newer_build" not in report


# --- manifest format 4 ------------------------------------------------------ #


def test_a_newer_manifest_format_is_not_checked(tmp_path, capsys):
    root = build(tmp_path / "s")
    set_manifest_format(root, 4)

    for argv in (["store", "verify", "--store", str(root)], ["check", str(root)]):
        rc, out = run(capsys, argv)
        assert_not_checked(rc, out)
        assert "format 4 is not supported by this build" in out, out
        assert "UNREADABLE" not in out, out

    assert main(["check", str(root), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["readable"] is False
    assert report["written_by_newer_build"] is True

    with pytest.raises(Exception) as ei:
        tgms.open(root, backend="native", read_only=True)
    msg = str(ei.value)
    assert msg.endswith(NEWER_WRITER_REMEDY), msg
    assert GENERIC_TAIL not in msg, msg


# --- damage is still damage ------------------------------------------------- #


def test_a_damaged_store_is_still_corrupt(tmp_path, capsys):
    root = build(tmp_path / "s")
    flip_body_byte(segments(root)[0])

    for argv in (["store", "verify", "--store", str(root)], ["check", str(root)]):
        rc, out = run(capsys, argv)
        assert rc == 1, out
        assert "verdict: CORRUPT" in out, out
        assert "checksum" in out, out
        assert GENERIC_TAIL in out, out  # damage keeps the generic remedy
        assert "NOT CHECKED" not in out and "newer TGMS build" not in out, out

    assert main(["store", "verify", "--store", str(root), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["readable"] is True
    assert "written_by_newer_build" not in report


def test_damage_beside_a_newer_segment_is_still_corrupt(tmp_path, capsys):
    root = build(tmp_path / "s")
    first, second = segments(root)[:2]
    rewrite_header_as_newer(first)
    flip_body_byte(second)

    for argv in (["store", "verify", "--store", str(root)], ["check", str(root)]):
        rc, out = run(capsys, argv)
        assert rc == 1, out
        assert "verdict: CORRUPT" in out, out
        assert "REFUSED, WRITTEN BY A NEWER BUILD (1)" in out, out
        assert "NOT CHECKED" not in out, out

    assert main(["store", "verify", "--store", str(root), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["readable"] is True
    assert report["written_by_newer_build"] is True
