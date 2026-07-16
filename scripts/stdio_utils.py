#!/usr/bin/env python3
"""Helpers for configuring Windows stdio safely."""

from __future__ import annotations

import argparse
import io
import json
import sys
from collections.abc import Callable


class JsonArgumentParser(argparse.ArgumentParser):
    """Emit the CLI's declared JSON error shape when --json parsing fails."""

    def __init__(
        self,
        *args,
        json_error_factory: Callable[[str], object],
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._json_error_factory = json_error_factory

    def error(self, message: str) -> None:
        if "--json" not in sys.argv[1:]:
            super().error(message)
        print(
            json.dumps(
                self._json_error_factory(message),
                indent=2,
                ensure_ascii=False,
            )
        )
        self.exit(2)


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
