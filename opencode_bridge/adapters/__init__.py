"""Lane B — messaging adapter package (CONTRACT.md §2.4)."""

from .base import Adapter, AdapterError, build

__all__ = ["Adapter", "build", "AdapterError"]
