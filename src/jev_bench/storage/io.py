"""Atomic persistence and cross-process writer locks."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile

def atomic_write(path, write):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', newline='', encoding='utf-8') as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def output_lock(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.benchmark.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another benchmark is writing to this output directory') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
