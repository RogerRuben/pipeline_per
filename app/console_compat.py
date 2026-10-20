# -*- coding: utf-8 -*-
"""Plain, device-safe logging for the application's own Windows processes."""
from __future__ import print_function

import atexit
import os
import re
import sys
import threading
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class PlainOutput(object):
    """Never probe console handles; failed terminal writes go to an explicit log.

    Only standard-output operations are guarded. File/data/model I/O failures
    elsewhere are not intercepted or reclassified as console failures.
    """
    _product_plain_output = True

    def __init__(self, stream, directory, name):
        self.stream, self.directory, self.name = stream, Path(directory), name
        self.lock = threading.RLock()
        self.failure = None

    @property
    def encoding(self):
        return getattr(self.stream, "encoding", None) or "utf-8"

    @property
    def errors(self):
        return "replace"

    @property
    def closed(self):
        return False

    def isatty(self):
        return False

    def fileno(self):
        return -1

    def _fallback(self, value, exc):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / ("console_fallback_%d.log" % os.getpid())
        with path.open("a", encoding="utf-8") as handle:
            if self.failure is None:
                self.failure = "%s: %s" % (type(exc).__name__, exc)
                handle.write("[%s device unavailable; using this log] %s\n" % (self.name, self.failure))
            handle.write(value)

    def write(self, value):
        if not isinstance(value, str):
            raise TypeError("standard output requires text")
        text = _ANSI.sub("", value)
        with self.lock:
            try:
                if self.stream is None or self.failure is not None:
                    raise OSError("standard output device unavailable")
                self.stream.write(text)
            except (OSError, ValueError, UnicodeError) as exc:
                self._fallback(text, exc)
        return len(value)

    def flush(self):
        with self.lock:
            try:
                if self.stream is not None and self.failure is None:
                    self.stream.flush()
            except (OSError, ValueError) as exc:
                self._fallback("", exc)

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _no_console_initialization(*args, **kwargs):
    return None


def enable_plain_console(directory=None):
    """Run before Optuna/Colorlog imports, including in spawn worker processes."""
    directory = Path(directory or Path(__file__).resolve().parents[1] / "logs")
    os.environ["NO_COLOR"] = "1"
    os.environ["TERM"] = "dumb"
    os.environ["PRODUCT_PLAIN_LOGS"] = "1"
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if not isinstance(stream, PlainOutput) and not getattr(type(stream), "_product_safe_console_stream", False):
            setattr(sys, name, PlainOutput(stream, directory, name))
    # NO_COLOR alone does not stop Colorlog's automatic Colorama init.
    try:
        import colorama
        from colorama import initialise
    except ImportError:
        return
    atexit.unregister(initialise.reset_all)
    for name in ("init", "reinit", "deinit", "just_fix_windows_console"):
        setattr(colorama, name, _no_console_initialization)
        setattr(initialise, name, _no_console_initialization)
    initialise.reset_all = _no_console_initialization
