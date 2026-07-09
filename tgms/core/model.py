"""Bi-temporal data model: intervals, entity references, node/edge versions.

Global conventions (spec §1):
- All timestamps are int64 epoch microseconds, UTC.
- Open end sentinel: OPEN_END = 2**62.
- Intervals are half-open [start, end); valid iff start < end.
- All persisted props are canonical JSON (sorted keys, compact separators).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

OPEN_END: int = 2**62

Props = dict[str, Any]


def clamp_tt(as_of_tt: int) -> int:
    """as_of_tt = OPEN_END means "current beliefs"; clamp so the half-open
    belief predicate tt_s <= as_of < tt_e holds for open rows (tt_e = OPEN_END)."""
    return min(as_of_tt, OPEN_END - 1)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON serialization: sorted keys, compact, UTF-8 preserved."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest(obj: Any) -> str:
    """SHA-256 of the canonical-JSON payload (used as result_digest everywhere)."""
    return sha256_hex(canonical_json(obj))


@dataclass(frozen=True, slots=True)
class Interval:
    """Half-open interval [start, end) over int64 microseconds."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if not (self.start < self.end):
            raise ValueError(f"invalid interval: [{self.start}, {self.end})")

    def contains(self, t: int) -> bool:
        return self.start <= t < self.end

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end

    def intersect(self, other: "Interval") -> "Interval | None":
        s, e = max(self.start, other.start), min(self.end, other.end)
        return Interval(s, e) if s < e else None

    def to_json(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}


def edge_eid(src: str, dst: str, rel_type: str, disc: str = "") -> str:
    """Logical edge identity: hash(src, dst, rel_type, disc)."""
    return sha256_hex(canonical_json([src, dst, rel_type, disc]))[:24]


def version_vid(identity: str, tt_s: int) -> str:
    """Version id: hash(logical identity, tt_s)."""
    return sha256_hex(f"{identity}:{tt_s}")[:24]


@dataclass(frozen=True, slots=True)
class EntityRef:
    """Reference to a logical node or logical edge."""

    kind: str  # "node" | "edge"
    uid: str | None = None  # node uid
    src: str | None = None  # edge endpoints / type
    dst: str | None = None
    rel_type: str | None = None
    disc: str = ""

    def __post_init__(self) -> None:
        if self.kind == "node":
            if not self.uid:
                raise ValueError("node ref requires uid")
        elif self.kind == "edge":
            if not (self.src and self.dst and self.rel_type):
                raise ValueError("edge ref requires src, dst, rel_type")
        else:
            raise ValueError(f"unknown ref kind: {self.kind}")

    @property
    def identity(self) -> str:
        if self.kind == "node":
            assert self.uid is not None
            return self.uid
        return edge_eid(self.src, self.dst, self.rel_type, self.disc)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class NodeVersion:
    vid: str
    uid: str
    label: str
    vt_s: int
    vt_e: int
    tt_s: int
    tt_e: int
    props: Props = field(default_factory=dict)

    def believed_at(self, as_of_tt: int) -> bool:
        return self.tt_s <= clamp_tt(as_of_tt) < self.tt_e

    def valid_at(self, t: int) -> bool:
        return self.vt_s <= t < self.vt_e

    def to_json(self) -> dict[str, Any]:
        return {
            "vid": self.vid, "uid": self.uid, "label": self.label,
            "vt_s": self.vt_s, "vt_e": self.vt_e, "tt_s": self.tt_s, "tt_e": self.tt_e,
            "props": self.props,
        }


@dataclass(frozen=True, slots=True)
class EdgeVersion:
    eid: str
    vid: str
    src: str
    dst: str
    rel_type: str
    disc: str
    vt_s: int
    vt_e: int
    tt_s: int
    tt_e: int
    props: Props = field(default_factory=dict)

    def believed_at(self, as_of_tt: int) -> bool:
        return self.tt_s <= clamp_tt(as_of_tt) < self.tt_e

    def valid_at(self, t: int) -> bool:
        return self.vt_s <= t < self.vt_e

    def to_json(self) -> dict[str, Any]:
        return {
            "eid": self.eid, "vid": self.vid, "src": self.src, "dst": self.dst,
            "rel_type": self.rel_type, "disc": self.disc,
            "vt_s": self.vt_s, "vt_e": self.vt_e, "tt_s": self.tt_s, "tt_e": self.tt_e,
            "props": self.props,
        }
