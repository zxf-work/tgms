"""Durability-injection hook, Python side (D-086).

Mirrors the engine's `crash_point` (`crates/tgms-engine-core/src/store.rs`):
a no-op unless `TGMS_CRASH_POINT` names this exact point, in which case the
process dies immediately — no unwinding, no `finally`, no destructors — so
the on-disk state left behind is exactly what a power cut at this line would
leave. The env read costs one lookup per call on the write path only;
`scripts/eval_durability.py` is the only intended setter.
"""

from __future__ import annotations

import os
import sys


def crash_point(name: str) -> None:
    if os.environ.get("TGMS_CRASH_POINT") == name:
        sys.stderr.write(f"TGMS_CRASH_POINT hit: {name}\n")
        sys.stderr.flush()
        os._exit(137)
