"""tgms.telemetry — a dependency-free JSONL metrics sink.

This package is a skeleton (P0.6): `Metrics` is standalone and is not yet
wired into `tgms.store` or the server — that belongs to the lanes that own
those call sites.
"""

from tgms.telemetry.metrics import Metrics

__all__ = ["Metrics"]
