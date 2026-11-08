"""Writing and reading the store's JSON files with other writers about.

Many processes share one store: the daemon, a hook's fallback, the miner, a
tool's subprocess. Writers used one temp name per file, so one replaced another's
half-written temp or raised PermissionError (four processes, 160 appends: 94
raised, and 2 of the 66 acknowledged were left on disk). On Windows a replace
also fails while any reader has the target open, and a read during a replace
can fail the same way.

``write_text`` takes a temp name of its own and retries a busy target.
``locked`` serialises a read-modify-write across processes, for a bounded wait:
past it the writer goes ahead unlocked, as every writer did before, because a
hook held up long enough gets its plugin unloaded. ``read_json`` tells three
cases apart, since a caller about to write must not take the wrong one for an
empty file: no file (the default), a corrupt file (moved aside, then the
default, so it is never written over) and a file it could not read (raises).

Standard library only, imported by the hooks. No fsync: a crash can lose the
last write, as it always could, and a sync per hook is too dear on a busy disk.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import os
import threading
import time
from pathlib import Path

from windvane import proc_lock

_counter = itertools.count()

LOCK_WAIT_S = 1.0
_LOCK_POLL_S = 0.01
# Sleeps between attempts: a sharing violation lasts as long as a reader's read.
_REPLACE_WAITS = (0.005, 0.01, 0.02, 0.04, 0.08, 0.16)
_READ_WAITS = (0.005, 0.02, 0.08)


def write_text(path, text: str) -> None:
    """Replace ``path`` with ``text``, whole or not at all. UTF-8, LF as given."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{next(_counter)}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        for wait in (*_REPLACE_WAITS, None):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if wait is None:
                    raise
                time.sleep(wait)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def write_json(path, data, indent=None) -> None:
    """``write_text`` of ``data``. Pure ASCII, so a reader on any code page can
    read it back."""
    write_text(path, json.dumps(data, indent=indent))


def lock_path(path) -> Path:
    path = Path(path)
    return path.with_name(path.name + ".lock")


@contextlib.contextmanager
def locked(path, wait: float = LOCK_WAIT_S):
    """Hold the lock for ``path`` across processes and threads. Yields whether
    it was got; the body runs either way once ``wait`` has passed."""
    target = lock_path(path)
    deadline = time.monotonic() + wait
    lock = proc_lock.acquire(target)
    while lock is None and time.monotonic() < deadline:
        time.sleep(_LOCK_POLL_S)
        lock = proc_lock.acquire(target)
    try:
        yield lock is not None
    finally:
        if lock is not None:
            lock.release()


def quarantine(path) -> None:
    """Move a damaged file aside, under a name that keeps it, for a person to
    look at. A file that cannot be moved stays where it is."""
    path = Path(path)
    with contextlib.suppress(OSError):
        os.replace(path, path.with_name(f"{path.name}.corrupt-{int(time.time() * 1000)}"))


def read_json(path, default=None):
    """The parsed file; ``default`` when there is none or it was corrupt (then
    moved aside). Raises OSError when it exists and cannot be read."""
    path = Path(path)
    text = _read_retrying(path)
    if text is None:
        return default
    try:
        return json.loads(text)
    except ValueError:
        quarantine(path)
        return default


def _read_retrying(path: Path):
    for wait in (*_READ_WAITS, None):
        try:
            return path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            return None
        except UnicodeDecodeError:
            return "\0"  # not JSON: the caller's parse moves it aside
        except OSError:
            if wait is None:
                raise
            time.sleep(wait)
    return None
