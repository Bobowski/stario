"""Host header normalization, shared with the Cython request view."""

from stario_cython.exchange import host_without_port

__all__ = ["host_without_port"]
