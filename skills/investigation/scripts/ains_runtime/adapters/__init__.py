"""Adapter registry: capability name -> Adapter instance.

Shipped adapters register here as they land (R4 nous, R5
spotlights-static, R6 spotlights-telemetry). Tests register a fake adapter
with register(..., replace=True) and restore the previous entry with
unregister(); no environment hook or plugin path exists, so an installed
skill can only run the adapters it ships.
"""
from __future__ import annotations

from .base import Adapter, AdapterContext, LaunchSpec, NativeState, Prepared

__all__ = ["Adapter", "AdapterContext", "LaunchSpec", "NativeState", "Prepared", "AdapterError",
           "register", "unregister", "get", "names"]

_REGISTRY: dict[str, Adapter] = {}


class AdapterError(LookupError):
    pass


def register(adapter: Adapter, *, replace: bool = False) -> Adapter | None:
    """Register adapter under adapter.capability. Returns the one it replaced."""
    if not isinstance(adapter, Adapter):
        raise TypeError("not an Adapter")
    if not adapter.capability or not adapter.version:
        raise ValueError("adapter needs capability and version")
    prev = _REGISTRY.get(adapter.capability)
    if prev is not None and not replace:
        raise AdapterError(f"adapter for {adapter.capability!r} already registered")
    _REGISTRY[adapter.capability] = adapter
    return prev


def unregister(capability: str, restore: Adapter | None = None) -> None:
    if restore is None:
        _REGISTRY.pop(capability, None)
    else:
        _REGISTRY[capability] = restore


def get(capability: str) -> Adapter:
    try:
        return _REGISTRY[capability]
    except KeyError:
        raise AdapterError(f"no adapter for capability {capability!r} in this "
                           "runtime version") from None


def names() -> list[str]:
    return sorted(_REGISTRY)


from .nous import NousAdapter  # noqa: E402  (R4)
register(NousAdapter())

from .spotlights_static import SpotlightsStaticAdapter  # noqa: E402  (R5)
register(SpotlightsStaticAdapter())
