"""Experimental self-healing orchestration with deterministic Maida verification."""

try:
    from maida.heal._version import version as __version__
except ImportError:  # pragma: no cover - generated in wheels
    __version__ = "0.0.0"

__all__ = ["__version__"]
