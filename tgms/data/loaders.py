"""Dataset loaders (spec §4).

Loader contract: every loader yields the canonical event iterator
{src, dst, rel_type, vt_s, vt_e?, props?} and produces a dataset-card JSON
(extent, counts, label vocab) consumed by the planner context. Raw-file
SHA-256 manifests are checked in under data_manifests/.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any, Iterator

Event = dict[str, Any]

DATASETS: dict[str, dict[str, Any]] = {
    "collegemsg": {
        "url": "https://snap.stanford.edu/data/CollegeMsg.txt.gz",
        "raw": "CollegeMsg.txt.gz",
        "rel_type": "MSG",
        "notes": "1,899 nodes / 59,835 timestamped edges; instantaneous events",
    },
    "email-eu": {
        "url": "https://snap.stanford.edu/data/email-Eu-core-temporal.txt.gz",
        "raw": "email-Eu-core-temporal.txt.gz",
        "rel_type": "EMAIL",
        "notes": "986 nodes / 332k events",
    },
    "bitcoinotc": {
        "url": "https://snap.stanford.edu/data/soc-sign-bitcoinotc.csv.gz",
        "raw": "soc-sign-bitcoinotc.csv.gz",
        "rel_type": "TRUST",
        "format": "csv_rated",
        "notes": "5,881 nodes / 35,592 signed trust ratings (-10..10); "
                 "financial trust domain (who-trusts-whom on the Bitcoin "
                 "OTC market); timestamped, instantaneous events",
    },
    # Stack-Exchange interaction networks ship one file per edge type; the
    # loader streams them in the listed (fixed) order, so the recorded event
    # log is deterministic but interleaves valid time across types — a real
    # tt≠vt workload, unlike the roughly time-sorted single-file datasets.
    "sx-mathoverflow": {
        "files": [
            {"url": "https://snap.stanford.edu/data/sx-mathoverflow-a2q.txt.gz",
             "raw": "sx-mathoverflow-a2q.txt.gz", "rel_type": "A2Q"},
            {"url": "https://snap.stanford.edu/data/sx-mathoverflow-c2q.txt.gz",
             "raw": "sx-mathoverflow-c2q.txt.gz", "rel_type": "C2Q"},
            {"url": "https://snap.stanford.edu/data/sx-mathoverflow-c2a.txt.gz",
             "raw": "sx-mathoverflow-c2a.txt.gz", "rel_type": "C2A"},
        ],
        "notes": "~25k nodes / ~506k events over ~6.5 years; three edge "
                 "types (answer-to-question, comment-to-question, "
                 "comment-to-answer); instantaneous events",
    },
    "sx-superuser": {
        "files": [
            {"url": "https://snap.stanford.edu/data/sx-superuser-a2q.txt.gz",
             "raw": "sx-superuser-a2q.txt.gz", "rel_type": "A2Q"},
            {"url": "https://snap.stanford.edu/data/sx-superuser-c2q.txt.gz",
             "raw": "sx-superuser-c2q.txt.gz", "rel_type": "C2Q"},
            {"url": "https://snap.stanford.edu/data/sx-superuser-c2a.txt.gz",
             "raw": "sx-superuser-c2a.txt.gz", "rel_type": "C2A"},
        ],
        "notes": "~194k nodes / ~1.44M events; same three edge types as "
                 "sx-mathoverflow; the 1M-class real graph",
    },
    "wiki-talk": {
        "url": "https://snap.stanford.edu/data/wiki-talk-temporal.txt.gz",
        "raw": "wiki-talk-temporal.txt.gz",
        "rel_type": "TALK",
        "notes": "~1.14M nodes / ~7.8M talk-page edits over ~6.4 years; "
                 "extreme hub skew (admins/bots) — the guardrail stressor; "
                 "instantaneous events",
    },
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_pinned(url: str, raw: Path, name: str) -> Path:
    if not raw.exists():
        urllib.request.urlretrieve(url, raw)  # noqa: S310 — SNAP https
    manifest = raw.with_suffix(raw.suffix + ".sha256")
    digest = sha256_file(raw)
    if manifest.exists():
        pinned = manifest.read_text().split()[0]
        if pinned != digest:
            raise RuntimeError(f"{name}: SHA-256 mismatch — expected {pinned}, "
                               f"got {digest}")
    else:
        manifest.write_text(f"{digest}  {raw.name}\n")
    return raw


def download(name: str, data_dir: str | Path) -> Path | list[tuple[Path, str]]:
    """Fetch and SHA-pin a dataset's raw file(s).

    Single-file datasets return the raw Path (unchanged contract);
    multi-file datasets return [(raw, rel_type), ...] in the DATASETS order,
    which is the order `load` streams them — part of the recorded log's
    determinism, so never reorder the spec list.
    """
    spec = DATASETS[name]
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    if "files" in spec:
        return [(_fetch_pinned(f["url"], data_dir / f["raw"], name), f["rel_type"])
                for f in spec["files"]]
    return _fetch_pinned(spec["url"], data_dir / spec["raw"], name)


def snap_edge_stream(raw: Path, rel_type: str) -> Iterator[Event]:
    """SNAP temporal format: `src dst unix_seconds` per line. Times are mapped
    to microseconds; events are instantaneous (vt_e = vt_s + 1 downstream)."""
    opener = gzip.open if raw.suffix == ".gz" else open
    with opener(raw, "rt") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(("#", "%")):
                continue
            s, d, t = line.split()[:3]
            yield {"src": f"n{s}", "dst": f"n{d}", "rel_type": rel_type,
                   "vt_s": int(t) * 1_000_000}


def csv_rated_stream(raw: Path, rel_type: str) -> Iterator[Event]:
    """SNAP signed-rating CSV: `SOURCE,TARGET,RATING,TIME` per line. TIME is
    epoch seconds (possibly fractional); RATING becomes an edge property.
    Events are instantaneous (vt_e = vt_s + 1 downstream)."""
    opener = gzip.open if raw.suffix == ".gz" else open
    with opener(raw, "rt") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith(("#", "%")):
                continue
            s, d, rating, t = line.split(",")[:4]
            yield {"src": f"n{s}", "dst": f"n{d}", "rel_type": rel_type,
                   "vt_s": int(float(t) * 1_000_000),
                   "props": {"rating": int(rating)}}


def load(name: str, data_dir: str | Path) -> Iterator[Event]:
    if name.startswith("synth"):
        path = Path(data_dir) / name / "events.jsonl"
        with open(path) as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
        return
    spec = DATASETS[name]
    raw = download(name, data_dir)
    if "files" in spec:
        for path, rel_type in raw:
            yield from snap_edge_stream(path, rel_type)
        return
    if spec.get("format") == "csv_rated":
        yield from csv_rated_stream(raw, spec["rel_type"])
        return
    yield from snap_edge_stream(raw, spec["rel_type"])


def ingest_dataset(name: str, data_dir: str | Path, store_path: str | Path,
                   backend: str = "duckdb") -> dict[str, Any]:
    """Download (if needed), ingest, and write the dataset card."""
    import tgms
    from tgms.agent.agent import dataset_card

    store = tgms.open(store_path, backend=backend)
    store.ingest_events(load(name, data_dir))
    card = dataset_card(store)
    card["dataset"] = name
    (Path(store_path) / "dataset_card.json").write_text(
        json.dumps(card, indent=1, sort_keys=True))
    store.close()
    return card
