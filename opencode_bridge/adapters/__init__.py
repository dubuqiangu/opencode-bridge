"""Messaging adapter package (CONTRACT.md §2.4)."""

from .base import (
    Adapter,
    AdapterError,
    adapter_class,
    build,
    registered_names,
    register,
)

__all__ = [
    "Adapter",
    "build",
    "AdapterError",
    "adapter_class",
    "registered_names",
    "register",
]
