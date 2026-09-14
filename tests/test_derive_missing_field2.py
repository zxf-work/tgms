"""[tests] compute derive with a missing field2 raises InvalidArgError,
never a raw KeyError.

The guard existed one line below the access — dead code — so a
model-supplied absent field2 escaped the TgmsError wrapper and killed the
task row instead of entering the repair loop (found by M8's 32B arm on
busiest_bucket-018, diagnosable only after rows learned to carry their
tracebacks). Pinned so the ordering cannot regress.
"""

from __future__ import annotations

import pytest

from tgms.core.errors import InvalidArgError
from tgms.temporal.algebra import REGISTRY, validate_args
from tgms.tools.server import ensure_all_registered


def test_derive_missing_field2_raises_invalid_arg():
    ensure_all_registered()
    rows = [{"t_a": 1, "t_b": 2, "value": 3}]
    filled = validate_args("compute", {
        "fn": "derive", "input": rows, "field": "value",
        "field2": "bucket_end", "op": "sub", "into": "d", "limit": 100})
    with pytest.raises(InvalidArgError, match="'bucket_end' missing"):
        REGISTRY["compute"].fn(None, filled)
