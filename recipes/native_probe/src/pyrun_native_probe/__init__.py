"""A deliberately tiny native package for validating the PyRun wheel pipeline."""

from ._native import add, native_abi

__all__ = ["add", "native_abi"]
