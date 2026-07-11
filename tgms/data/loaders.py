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
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(name: str, data_dir: str | Path) -> Path:
    spec = DATASETS[name]
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    raw = data_dir / spec["raw"]
    if not raw.exists():
        urllib.request.urlretrieve(spec["url"], raw)  # noqa: S310 — SNAP https
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


def load(name: str, data_dir: str | Path) -> Iterator[Event]:
    if name.startswith("synth"):
        path = Path(data_dir) / name / "events.jsonl"
        with open(path) as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)
        return
    spec = DATASETS[name]
    yield from snap_edge_stream(download(name, data_dir), spec["rel_type"])


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
