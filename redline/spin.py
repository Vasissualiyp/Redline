"""A spinner on stderr for slow command-line steps (only when stderr is a terminal)."""
from __future__ import annotations

import contextlib
import sys
import threading
import time

FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@contextlib.contextmanager
def spinning(text: str):
    """`with spinning("pulling X"): ...` shows an animated line until the block ends."""
    if not sys.stderr.isatty():
        yield
        return
    stop = threading.Event()

    def run() -> None:
        i = 0
        while not stop.is_set():
            sys.stderr.write(f"\r\x1b[33m{FRAMES[i % len(FRAMES)]}\x1b[0m {text} ")
            sys.stderr.flush()
            i += 1
            time.sleep(0.1)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()
        sys.stderr.write("\r\x1b[2K")
        sys.stderr.flush()
