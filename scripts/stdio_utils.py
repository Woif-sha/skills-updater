#!/usr/bin/env python3
"""Helpers for configuring Windows stdio safely."""

from __future__ import annotations

import io
import sys


def configure_windows_utf8_stdio() -> None:
    """Configure UTF-8 stdio on Windows without stacking TextIOWrapper objects."""
    if sys.platform != "win32":
        return

    _configure_stream("stdout")
    _configure_stream("stderr")


def _configure_stream(name: str) -> None:
    stream = getattr(sys, name)

    # Prefer in-place reconfiguration so repeated imports stay safe.
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")
        return

    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        return

    setattr(sys, name, io.TextIOWrapper(buffer, encoding="utf-8", errors="replace"))
