"""OSV advisory feed -> TGMS event-log ops (Lane F3, P0.5).

Design of record: `docs/design/LIVE_WORKLOAD_OSV_DESIGN_2026-09-13.md`. This
module mirrors `tgms/data/snb_loader.py`'s discipline: **one definition site**
for identity (`osv_uid`, §1), a frozen mapping from the source's own two
clocks to TGMS's two clocks (§2 — `published` is valid time, `modified` never
is, it only triggers a re-read), and a hard split between the pure
record-to-ops mapping (this module, no I/O) and the file/network readers that
feed it (`scripts/live_osv_poller.py`).

**Identity (§1).** OSV ids are strings, not LDBC's sparse integers, so the
SNB interleave does not transfer. One separator, one function:

    osv_uid(kind, *parts) = kind + ":" + "\\x1f".join(parts)
      A  Advisory        A:GHSA-xxxx-xxxx-xxxx
      P  Package         P:PyPI\\x1fdjango
      V  PackageVersion  V:PyPI\\x1fdjango\\x1f4.2.1
      R  Range           R:<advisory_id>\\x1f<pkg_uid>\\x1f<ordinal>

The design's own table stops at those four; this loader extends the same
`kind + ":" + parts`-joined-by-\\x1f scheme (never a second definition site)
to the two more node labels §1 names but does not spell a uid rule for:

      E  Ecosystem       E:PyPI
      F  Reference       F:<url>            ("R" is already Range)

`\\x1f` cannot occur in an OSV id, ecosystem, or package name, so collision
freedom holds by the same construction argument §1 gives for the original
four.

**What is a correction (§2).** `published` -> `vt_s` of the advisory and
every edge it implies, `vt_e = OPEN_END`. `modified` never touches valid
time; it is purely a poll trigger. The design's own table is realized here
as:

| OSV change | this module | why |
|---|---|---|
| new id never seen | `record_to_ops` -> `assert_*` | append |
| `affected[].ranges` gains/loses/edits an event | `diff_to_ops` -> `assert_*` (new range) or `correct` (edited range) or `retract` (removed range) | belief about the past changed |
| `severity`/`summary`/`details` changes | `diff_to_ops` -> `correct` on the Advisory node | same |
| `aliases` gains a member | `diff_to_ops` -> `assert_edge`, both arms | held since publication |
| `withdrawn` appears | `diff_to_ops` -> `correct` (Advisory node) + `retract` (every open `affects` edge, `t=withdrawn_ts`) | §2's "deliberately both" |
| `references` gains a URL | `diff_to_ops` -> `assert_edge` at `vt_s=modified` | acquired, not retroactively true |
| new `affected` package appears | `diff_to_ops` -> `assert_edge`/`assert_node` at `vt_s=published` | belief is new, the package was always affected |
| `modified` bumps, canonicalised content unchanged | `diff_to_ops` -> `[]` | `noop_revisions` |

**`MAL-` is filtered everywhere** (`is_malicious`): OpenSSF Malicious-Packages
entries are bulk-published and essentially never revised (§3), and would
swamp the correction signal.

**Ranges, never enumerated versions** (§1's rejection of materialising
`affected[].versions`). A `Range` node holds one `affected[].ranges[]` entry
verbatim (`range_type` + the raw `events` list, so multi-event ranges are not
lossy) and a `range_of` edge to the affected `Package`. `PackageVersion`
nodes, and the `introduced_in`/`fixed_by` edges to them, are created **only**
where a range event names a concrete version — `introduced: "0"` is the
schema's own sentinel for "since the beginning" and is not a version, so it
never gets a node or an edge; every other `introduced`/`fixed`/
`last_affected` value does, git commit hashes included.

**Cross-advisory shared nodes (Package, Ecosystem, PackageVersion, and alias
stub Advisories) are asserted maximally per-record** — `record_to_ops` does
not know what any other record already wrote, by design (no I/O, no hidden
state). Two call sites dedupe the resulting `assert_node` ops against
whatever already exists, each with the visibility it actually has:
`bootstrap_ops` dedupes across one in-memory pass over the whole corpus,
and the live poller (`scripts/live_osv_poller.py`) dedupes against the open
store before writing a cycle's batch. Both go through the one shared,
pure filter here: `dedupe_assert_nodes`.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from tgms.core.model import OPEN_END, canonical_json, digest
from tgms.storage.base import make_op

Op = dict[str, Any]

#: §3's selection: the four ecosystems whose data is CC-BY-4.0/CC0-1.0 and
#: whose `MAL-` share is low enough not to swamp the correction signal.
#: Frozen order matters nowhere here (a `set` would do), but a tuple keeps
#: the module's public surface as boring to read as `snb_loader`'s.
ALLOWED_ECOSYSTEMS: tuple[str, ...] = ("PyPI", "Go", "Maven", "crates.io")

#: §3: OpenSSF Malicious-Packages ids. Filtered in every ecosystem.
MAL_PREFIX = "MAL-"

_SEP = "\x1f"


# --------------------------------------------------------------------------
# §1 — identity
# --------------------------------------------------------------------------

def osv_uid(kind: str, *parts: str) -> str:
    """**The one definition site.** `kind + ":" + "\\x1f".join(parts)` —
    collision-free because `\\x1f` cannot occur in an OSV id, ecosystem, or
    package name (§1), so the tuple `(kind, *parts)` is always recoverable
    from the string and no two tuples can spell the same one."""
    return f"{kind}:{_SEP.join(parts)}"


def is_malicious(advisory_id: str) -> bool:
    return advisory_id.startswith(MAL_PREFIX)


def _kept_affected(record: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for a in record.get("affected") or []:
        pkg = a.get("package") or {}
        if pkg.get("ecosystem") in ALLOWED_ECOSYSTEMS and pkg.get("name"):
            out.append(a)
    return out


def _pkg_ranges(kept: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """OSV allows more than one `affected[]` entry for the same
    `(ecosystem, name)` in one record (different `database_specific`
    qualifiers on each, e.g. a per-Python-version split) — real in this
    fixture's own `GHSA-22jm-p2vv-j2hc`. Every range across every such entry
    is one continuous, order-preserving list per package, so a Range node's
    `ordinal` is stable and collision-free even when the package is split
    across multiple `affected[]` entries: two entries never race for
    ordinal 0."""
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for a in kept:
        key = (a["package"]["ecosystem"], a["package"]["name"])
        out.setdefault(key, []).extend(a.get("ranges") or [])
    return out


# --------------------------------------------------------------------------
# §2 — time
# --------------------------------------------------------------------------

def parse_osv_ts(s: str) -> int:
    """OSV/RFC3339 timestamp -> epoch microseconds, UTC.

    Unlike `snb_loader.parse_ts` this cannot be a single frozen shape check:
    OSV timestamps come from many upstream databases and vary in fractional
    precision (`...Z`, `...000Z`, `...123456Z`, or an explicit `+00:00`).
    What is **not** negotiable is the zone: a naive or non-UTC timestamp
    would shift `vt_s` silently, exactly the hazard `snb_loader.parse_ts`
    guards against, so a missing/non-UTC offset is asserted, not assumed.
    """
    text = s.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = _dt.datetime.fromisoformat(text)
    except ValueError as e:
        raise ValueError(f"unparseable OSV timestamp {s!r}: {e}") from None
    if dt.tzinfo is None:
        raise ValueError(f"OSV timestamp {s!r} carries no timezone; refusing to guess UTC")
    dt = dt.astimezone(_dt.timezone.utc)
    epoch = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
    return round((dt - epoch).total_seconds() * 1_000_000)


# --------------------------------------------------------------------------
# canonicalisation — the digest §2 diffs on, never raw bytes
# --------------------------------------------------------------------------

def _canon_severity(sev: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        ({"type": s.get("type", ""), "score": s.get("score", "")} for s in sev),
        key=lambda s: (s["type"], s["score"]),
    )


def _canon_range(r: dict[str, Any]) -> dict[str, Any]:
    return {"type": r.get("type", ""), "events": list(r.get("events") or [])}


def _canon_affected(a: dict[str, Any]) -> dict[str, Any]:
    pkg = a.get("package") or {}
    return {
        "ecosystem": pkg.get("ecosystem", ""),
        "name": pkg.get("name", ""),
        "ranges": [_canon_range(r) for r in a.get("ranges") or []],
    }


def _canon_references(refs: list[dict[str, Any]]) -> list[list[str]]:
    return sorted({(r.get("type", ""), r.get("url", "")) for r in refs or []})


def canonical_digest(record: dict[str, Any]) -> str:
    """SHA-256 over exactly the fields this loader models, canonicalised —
    never raw bytes (§2's "not corrections" rule; `database_specific` and the
    enumerated `affected[].versions` are dropped entirely, §1). Two records
    with the same digest cannot produce a different write from `diff_to_ops`,
    and the whole no-op path (`revisions_seen` with zero `corrections_written`
    or `noop_revisions++`) rests on that equivalence."""
    kept = sorted(_kept_affected(record), key=lambda a: (
        (a.get("package") or {}).get("ecosystem", ""),
        (a.get("package") or {}).get("name", ""),
    ))
    payload = {
        "id": record["id"],
        "published": record.get("published"),
        "withdrawn": record.get("withdrawn"),
        "summary": record.get("summary") or "",
        "details": record.get("details") or "",
        "severity": _canon_severity(record.get("severity") or []),
        "aliases": sorted(record.get("aliases") or []),
        "references": _canon_references(record.get("references") or []),
        "affected": [_canon_affected(a) for a in kept],
    }
    return digest(payload)


def _belief_key(record: dict[str, Any]) -> tuple[Any, ...]:
    """The subset of the Advisory node's own props that a `correct` should
    fire on — deliberately excludes `modified`/`content_digest`, which
    change on every observation and would otherwise turn every `noop_revision`
    into a spurious correction (§2's last row)."""
    return (
        record.get("summary") or "",
        record.get("details") or "",
        canonical_json(_canon_severity(record.get("severity") or [])),
        bool(record.get("withdrawn")),
    )


def _advisory_props(record: dict[str, Any], content_digest: str) -> dict[str, Any]:
    return {
        "summary": record.get("summary") or "",
        "details": record.get("details") or "",
        "severity": _canon_severity(record.get("severity") or []),
        "withdrawn": bool(record.get("withdrawn")),
        "withdrawn_ts": record.get("withdrawn"),
        "source_ecosystems": sorted({
            (a.get("package") or {}).get("ecosystem", "")
            for a in _kept_affected(record)
        }),
        "osv_modified": record.get("modified"),
        "content_digest": content_digest,
    }


def _range_props(r: dict[str, Any]) -> dict[str, Any]:
    events = list(r.get("events") or [])
    introduced = next((e["introduced"] for e in events if "introduced" in e), None)
    fixed = next((e["fixed"] for e in events if "fixed" in e), None)
    last_affected = next((e["last_affected"] for e in events if "last_affected" in e), None)
    return {
        "range_type": r.get("type", ""),
        "introduced": introduced,
        "fixed": fixed,
        "last_affected": last_affected,
        "events": events,
    }


def _version_edges(adv_uid: str, range_uid: str, eco: str, name: str,
                    events: list[dict[str, Any]], vt_s: int) -> Iterator[Op]:
    """`fixed_by`/`introduced_in` — only where an event names a concrete
    version (`introduced: "0"` is the schema's -infinity sentinel, not a
    version, and is skipped)."""
    for ev in events:
        for key, rel in (("introduced", "introduced_in"), ("fixed", "fixed_by"),
                         ("last_affected", "fixed_by")):
            val = ev.get(key)
            if val is None:
                continue
            if key == "introduced" and val == "0":
                continue
            ver_uid = osv_uid("V", eco, name, str(val))
            yield make_op("assert_node", uid=ver_uid, label="PackageVersion",
                          props={"ecosystem": eco, "name": name, "version": str(val)},
                          vt_s=vt_s, vt_e=OPEN_END, source="osv",
                          provenance_ref=range_uid)
            yield make_op("assert_edge", src=adv_uid, dst=ver_uid, rel_type=rel,
                          props={}, vt_s=vt_s, vt_e=OPEN_END, disc="",
                          source="osv", provenance_ref=range_uid)


# --------------------------------------------------------------------------
# record_to_ops — a never-seen-before advisory, appended in full
# --------------------------------------------------------------------------

def record_to_ops(record: dict[str, Any]) -> list[Op]:
    """Every op a brand-new observation of `record` implies (§2's "new id
    never seen" row). Pure: no I/O, no cross-record memory — Package,
    Ecosystem and PackageVersion nodes are asserted as if this were the
    first time they were ever seen, and cross-record deduplication is the
    caller's job (`dedupe_assert_nodes`).

    An already-withdrawn record ingested for the first time (the bootstrap's
    931) needs no separate `retract`: its `affects` edges are asserted
    directly with `vt_e = withdrawn_ts` rather than `OPEN_END`.
    """
    if is_malicious(record["id"]):
        return []
    adv_uid = osv_uid("A", record["id"])
    published_raw = record.get("published") or record.get("modified")
    if not published_raw:
        raise ValueError(f"{record['id']}: no published/modified timestamp to anchor vt_s")
    published = parse_osv_ts(published_raw)
    withdrawn_raw = record.get("withdrawn")
    withdrawn_ts = parse_osv_ts(withdrawn_raw) if withdrawn_raw else None
    content_digest = canonical_digest(record)

    ops: list[Op] = [make_op(
        "assert_node", uid=adv_uid, label="Advisory",
        props=_advisory_props(record, content_digest),
        vt_s=published, vt_e=OPEN_END, source="osv", provenance_ref=record["id"],
    )]

    for alias in sorted(set(record.get("aliases") or [])):
        alias_uid = osv_uid("A", alias)
        ops.append(make_op("assert_node", uid=alias_uid, label="Advisory", props={},
                           vt_s=published, vt_e=OPEN_END, source="osv",
                           provenance_ref=record["id"]))
        # symmetric and transitive by schema (§1): written both arms.
        ops.append(make_op("assert_edge", src=adv_uid, dst=alias_uid, rel_type="aliases",
                           props={}, vt_s=published, vt_e=OPEN_END, disc="",
                           source="osv", provenance_ref=None))
        ops.append(make_op("assert_edge", src=alias_uid, dst=adv_uid, rel_type="aliases",
                           props={}, vt_s=published, vt_e=OPEN_END, disc="",
                           source="osv", provenance_ref=None))

    for ref in record.get("references") or []:
        url = ref.get("url")
        if not url:
            continue
        ref_uid = osv_uid("F", url)
        ops.append(make_op("assert_node", uid=ref_uid, label="Reference",
                           props={"url": url, "type": ref.get("type", "")},
                           vt_s=published, vt_e=OPEN_END, source="osv", provenance_ref=None))
        ops.append(make_op("assert_edge", src=adv_uid, dst=ref_uid, rel_type="references",
                           props={}, vt_s=published, vt_e=OPEN_END, disc="",
                           source="osv", provenance_ref=None))

    # §7 risk 3 / withdraws: best-effort extraction, not spelled precisely by
    # the design memo (flagged in the F3 report). A withdrawn record's own
    # `related` ids are read as the advisories that superseded it.
    if withdrawn_ts is not None:
        for rel_id in sorted(set(record.get("related") or [])):
            rel_uid = osv_uid("A", rel_id)
            ops.append(make_op("assert_node", uid=rel_uid, label="Advisory", props={},
                               vt_s=published, vt_e=OPEN_END, source="osv",
                               provenance_ref=record["id"]))
            ops.append(make_op("assert_edge", src=rel_uid, dst=adv_uid, rel_type="withdraws",
                               props={}, vt_s=withdrawn_ts, vt_e=OPEN_END, disc="",
                               source="osv", provenance_ref=None))

    affects_vt_e = withdrawn_ts if withdrawn_ts is not None else OPEN_END
    for (eco, name), ranges in _pkg_ranges(_kept_affected(record)).items():
        eco_uid = osv_uid("E", eco)
        pkg_uid = osv_uid("P", eco, name)
        ops.append(make_op("assert_node", uid=eco_uid, label="Ecosystem", props={"name": eco},
                           vt_s=published, vt_e=OPEN_END, source="osv", provenance_ref=None))
        ops.append(make_op("assert_node", uid=pkg_uid, label="Package",
                           props={"ecosystem": eco, "name": name},
                           vt_s=published, vt_e=OPEN_END, source="osv", provenance_ref=None))
        # A handful of real GHSA records (6 of 32,912 in the 2026-09-13
        # bootstrap corpus, all dated 2021-02-24) carry `withdrawn ==
        # published` to the microsecond -- duplicate/superseded advisories
        # withdrawn at the instant they were published. `affects_vt_e` then
        # equals `published`, and `Interval` (`tgms/core/model.py:43-51`) is
        # half-open and requires `start < end` strictly, so asserting
        # `[published, affects_vt_e)` would raise `InvalidArgError` on a
        # zero-width (or, if a feed ever reports `withdrawn < published`,
        # negative-width) interval. Semantically this package was never
        # validly affected for any positive duration, so the edge is simply
        # never asserted -- the Ecosystem/Package/Range nodes above and below
        # are unaffected, since they carry no narrowed interval. This is the
        # same case `diff_to_ops`'s live withdrawal-transition `retract`
        # already tolerates without a fix: `StorageAdapter._retract`
        # (`tgms/storage/base.py:398-422`) filters replacement fragments on
        # `v.vt_s < t`, so a `retract(t=vt_s)` supersedes the open version
        # and inserts no zero-width replacement -- it never calls
        # `_interval()` at all. `record_to_ops`'s direct `assert_edge` has no
        # such filter, which is what made this the crashing path.
        if affects_vt_e > published:
            ops.append(make_op("assert_edge", src=adv_uid, dst=pkg_uid, rel_type="affects",
                               props={}, vt_s=published, vt_e=affects_vt_e, disc="",
                               source="osv", provenance_ref=None))
        for ordinal, r in enumerate(ranges):
            range_uid = osv_uid("R", record["id"], pkg_uid, str(ordinal))
            events = list(r.get("events") or [])
            ops.append(make_op("assert_node", uid=range_uid, label="Range",
                               props=_range_props(r), vt_s=published, vt_e=OPEN_END,
                               source="osv", provenance_ref=None))
            ops.append(make_op("assert_edge", src=range_uid, dst=pkg_uid, rel_type="range_of",
                               props={}, vt_s=published, vt_e=OPEN_END, disc="",
                               source="osv", provenance_ref=None))
            ops.extend(_version_edges(adv_uid, range_uid, eco, name, events, published))

    return ops


# --------------------------------------------------------------------------
# diff_to_ops — a re-observed advisory
# --------------------------------------------------------------------------

def diff_to_ops(old: dict[str, Any], new: dict[str, Any]) -> list[Op]:
    """§2's table, for a record already believed once as `old`.

    Every `correct`/`retract` below targets an entity `record_to_ops` (or an
    earlier `diff_to_ops`) is guaranteed to have already asserted, at exactly
    `vt_s=published, vt_e=OPEN_END` — the one exception being `affects`
    edges, whose `vt_e` narrows to `withdrawn_ts` on the withdrawal
    transition (§2's "deliberately both"). Nothing here ever asks for a `vt`
    window wider than what was actually asserted, which is what keeps every
    `correct` an honest narrowing/restatement rather than a silent
    re-opening of an interval that was already closed.
    """
    if old["id"] != new["id"]:
        raise ValueError(f"diff_to_ops: id mismatch {old['id']!r} vs {new['id']!r}")
    if is_malicious(new["id"]):
        return []
    if canonical_digest(old) == canonical_digest(new):
        return []  # noop_revision: canonicalised content is byte-identical

    adv_uid = osv_uid("A", new["id"])
    published = parse_osv_ts(new.get("published") or old.get("published") or new["modified"])
    ops: list[Op] = []

    old_withdrawn = old.get("withdrawn")
    new_withdrawn = new.get("withdrawn")
    if new_withdrawn and not old_withdrawn:
        withdrawn_ts = parse_osv_ts(new_withdrawn)
        for a in _kept_affected(old):
            pkg_uid = osv_uid("P", a["package"]["ecosystem"], a["package"]["name"])
            ops.append(make_op("retract", ref={"kind": "edge", "src": adv_uid, "dst": pkg_uid,
                                              "rel_type": "affects", "disc": ""},
                               t=withdrawn_ts, source="osv", provenance_ref=None))
        for rel_id in sorted(set(new.get("related") or [])):
            rel_uid = osv_uid("A", rel_id)
            ops.append(make_op("assert_node", uid=rel_uid, label="Advisory", props={},
                               vt_s=published, vt_e=OPEN_END, source="osv",
                               provenance_ref=new["id"]))
            ops.append(make_op("assert_edge", src=rel_uid, dst=adv_uid, rel_type="withdraws",
                               props={}, vt_s=withdrawn_ts, vt_e=OPEN_END, disc="",
                               source="osv", provenance_ref=None))

    if _belief_key(old) != _belief_key(new):
        ops.append(make_op(
            "correct", ref={"kind": "node", "uid": adv_uid},
            props=_advisory_props(new, canonical_digest(new)),
            vt_s=published, vt_e=OPEN_END, source="osv", provenance_ref=new["id"],
        ))

    old_aliases = set(old.get("aliases") or [])
    for alias in sorted(set(new.get("aliases") or []) - old_aliases):
        alias_uid = osv_uid("A", alias)
        ops.append(make_op("assert_node", uid=alias_uid, label="Advisory", props={},
                           vt_s=published, vt_e=OPEN_END, source="osv",
                           provenance_ref=new["id"]))
        ops.append(make_op("assert_edge", src=adv_uid, dst=alias_uid, rel_type="aliases",
                           props={}, vt_s=published, vt_e=OPEN_END, disc="",
                           source="osv", provenance_ref=None))
        ops.append(make_op("assert_edge", src=alias_uid, dst=adv_uid, rel_type="aliases",
                           props={}, vt_s=published, vt_e=OPEN_END, disc="",
                           source="osv", provenance_ref=None))

    modified = parse_osv_ts(new.get("modified") or new.get("published"))
    old_refs = {(r.get("type", ""), r.get("url", "")) for r in old.get("references") or []}
    for r in new.get("references") or []:
        key = (r.get("type", ""), r.get("url", ""))
        url = r.get("url")
        if key in old_refs or not url:
            continue
        ref_uid = osv_uid("F", url)
        ops.append(make_op("assert_node", uid=ref_uid, label="Reference",
                           props={"url": url, "type": r.get("type", "")},
                           vt_s=modified, vt_e=OPEN_END, source="osv", provenance_ref=None))
        ops.append(make_op("assert_edge", src=adv_uid, dst=ref_uid, rel_type="references",
                           props={}, vt_s=modified, vt_e=OPEN_END, disc="",
                           source="osv", provenance_ref=None))

    old_pkgs = _pkg_ranges(_kept_affected(old))
    new_pkgs = _pkg_ranges(_kept_affected(new))

    for key, new_ranges in new_pkgs.items():
        eco, name = key
        pkg_uid = osv_uid("P", eco, name)
        if key not in old_pkgs:
            eco_uid = osv_uid("E", eco)
            ops.append(make_op("assert_node", uid=eco_uid, label="Ecosystem",
                               props={"name": eco}, vt_s=published, vt_e=OPEN_END,
                               source="osv", provenance_ref=None))
            ops.append(make_op("assert_node", uid=pkg_uid, label="Package",
                               props={"ecosystem": eco, "name": name},
                               vt_s=published, vt_e=OPEN_END, source="osv",
                               provenance_ref=None))
            ops.append(make_op("assert_edge", src=adv_uid, dst=pkg_uid, rel_type="affects",
                               props={}, vt_s=published, vt_e=OPEN_END, disc="",
                               source="osv", provenance_ref=None))
            for ordinal, r in enumerate(new_ranges):
                range_uid = osv_uid("R", new["id"], pkg_uid, str(ordinal))
                events = list(r.get("events") or [])
                ops.append(make_op("assert_node", uid=range_uid, label="Range",
                                   props=_range_props(r), vt_s=published, vt_e=OPEN_END,
                                   source="osv", provenance_ref=None))
                ops.append(make_op("assert_edge", src=range_uid, dst=pkg_uid,
                                   rel_type="range_of", props={}, vt_s=published,
                                   vt_e=OPEN_END, disc="", source="osv", provenance_ref=None))
                ops.extend(_version_edges(adv_uid, range_uid, eco, name, events, published))
            continue

        old_ranges = old_pkgs[key]
        for ordinal in range(max(len(old_ranges), len(new_ranges))):
            range_uid = osv_uid("R", new["id"], pkg_uid, str(ordinal))
            if ordinal >= len(old_ranges):
                r = new_ranges[ordinal]
                events = list(r.get("events") or [])
                ops.append(make_op("assert_node", uid=range_uid, label="Range",
                                   props=_range_props(r), vt_s=published, vt_e=OPEN_END,
                                   source="osv", provenance_ref=None))
                ops.append(make_op("assert_edge", src=range_uid, dst=pkg_uid,
                                   rel_type="range_of", props={}, vt_s=published,
                                   vt_e=OPEN_END, disc="", source="osv", provenance_ref=None))
                ops.extend(_version_edges(adv_uid, range_uid, eco, name, events, published))
            elif ordinal >= len(new_ranges):
                ops.append(make_op("retract", ref={"kind": "node", "uid": range_uid},
                                   t=modified, source="osv", provenance_ref=None))
            elif _canon_range(old_ranges[ordinal]) != _canon_range(new_ranges[ordinal]):
                r = new_ranges[ordinal]
                events = list(r.get("events") or [])
                ops.append(make_op("correct", ref={"kind": "node", "uid": range_uid},
                                   props=_range_props(r), vt_s=published, vt_e=OPEN_END,
                                   source="osv", provenance_ref=None))
                ops.extend(_version_edges(adv_uid, range_uid, eco, name, events, published))

    for key in old_pkgs.keys() - new_pkgs.keys():
        eco, name = key
        pkg_uid = osv_uid("P", eco, name)
        ops.append(make_op("retract", ref={"kind": "edge", "src": adv_uid, "dst": pkg_uid,
                                          "rel_type": "affects", "disc": ""},
                           t=modified, source="osv", provenance_ref=None))

    return ops


# --------------------------------------------------------------------------
# cross-record dedup — the one place shared-node re-assertion is prevented
# --------------------------------------------------------------------------

def dedupe_assert_nodes(ops: Iterable[Op],
                        already_exists: Callable[[str], bool]) -> Iterator[Op]:
    """Drop `assert_node` ops naming a uid `already_exists` reports true for,
    or that this call has already yielded once. `already_exists` is the
    caller's own visibility into what happened before this stream started —
    an in-memory set for `bootstrap_ops` (one pass over the whole corpus),
    the live store for the poller (`scripts/live_osv_poller.py`). Every
    other op passes through untouched."""
    seen: set[str] = set()
    for op in ops:
        if op.get("op") == "assert_node":
            uid = op["uid"]
            if uid in seen or already_exists(uid):
                continue
            seen.add(uid)
        yield op


def bootstrap_ops(records: Iterable[dict[str, Any]]) -> Iterator[Op]:
    """`record_to_ops` over a whole corpus, deterministically ordered
    (sorted by id, an explicit property rather than an accident of
    `iter_ecosystem_records`'s directory listing) and deduped against an
    in-memory seen-set built as the pass goes — one assert per shared
    Package/Ecosystem/PackageVersion/alias-stub uid across the whole
    bootstrap, however many advisories reference it."""
    seen: set[str] = set()

    def exists(uid: str) -> bool:
        return uid in seen

    for record in sorted(records, key=lambda r: r["id"]):
        for op in dedupe_assert_nodes(record_to_ops(record), exists):
            if op.get("op") == "assert_node":
                seen.add(op["uid"])
            yield op


# --------------------------------------------------------------------------
# file readers — the loader's only I/O, kept separate from the pure mapping
# --------------------------------------------------------------------------

def iter_ecosystem_records(root: str | Path, ecosystem: str) -> Iterator[dict[str, Any]]:
    """`root/ecosystem/*.json`, sorted by filename for reproducibility,
    `MAL-` filtered. This is the bootstrap directory shape: one flat
    directory of advisory JSON files per ecosystem, exactly what an
    extracted per-ecosystem OSV `all.zip` looks like."""
    d = Path(root) / ecosystem
    if not d.is_dir():
        raise FileNotFoundError(f"missing OSV bootstrap directory: {d}")
    for p in sorted(d.glob("*.json")):
        if p.stem.startswith(MAL_PREFIX):
            continue
        with open(p, encoding="utf-8") as f:
            record = json.load(f)
        if is_malicious(record.get("id", p.stem)):
            continue
        yield record


def iter_bootstrap_records(root: str | Path,
                           ecosystems: tuple[str, ...] = ALLOWED_ECOSYSTEMS,
                           ) -> Iterator[dict[str, Any]]:
    for eco in ecosystems:
        yield from iter_ecosystem_records(root, eco)


def read_modified_id_csv(path: str | Path) -> Iterator[tuple[str, str]]:
    """`modified_id.csv`: `<iso modified>,<id>`, reverse-chronological
    (§3 — "stop processing when you encounter a timestamp you have already
    seen"). Yields `(modified_iso, id)` in file order; `MAL-` is filtered
    here too so a caller never even fetches one."""
    with open(path, encoding="utf-8", newline="") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            modified_iso, _, advisory_id = line.partition(",")
            if not advisory_id or is_malicious(advisory_id):
                continue
            yield modified_iso, advisory_id


# --------------------------------------------------------------------------
# dry-run reporting
# --------------------------------------------------------------------------

def summarize_ops(ops: Iterable[Op]) -> dict[str, int]:
    """Op-kind histogram — what `--dry-run` prints and what the loader test
    checks node/edge counts against."""
    counts: dict[str, int] = {}
    for op in ops:
        kind = op.get("op", "?")
        counts[kind] = counts.get(kind, 0) + 1
    return counts


__all__ = [
    "ALLOWED_ECOSYSTEMS", "MAL_PREFIX", "bootstrap_ops", "canonical_digest",
    "dedupe_assert_nodes", "diff_to_ops", "iter_bootstrap_records",
    "iter_ecosystem_records", "is_malicious", "osv_uid", "parse_osv_ts",
    "read_modified_id_csv", "record_to_ops", "summarize_ops",
]
