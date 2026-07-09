"""TGMS — Agent-Native Temporal Graph Management System."""

from tgms.core.model import OPEN_END, EntityRef, Interval
from tgms.store import Store, open

__version__ = "0.1.0"
__all__ = ["OPEN_END", "EntityRef", "Interval", "Store", "open"]
