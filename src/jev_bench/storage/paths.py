"""Repository locations and immutable evidence relocation."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
from functools import wraps

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = PACKAGE_ROOT.parents[1]
RESULTS_ROOT = PROJECT_ROOT / "results"
_dataset = ContextVar("dataset", default=None)


@contextmanager
def dataset_context(directory):
    token = _dataset.set(Path(directory).resolve())
    try:
        yield
    finally:
        _dataset.reset(token)


def in_dataset(function):
    @wraps(function)
    def wrapped(directory, *args, **kwargs):
        with dataset_context(directory):
            return function(directory, *args, **kwargs)
    return wrapped


def resolve_evidence(value):
    path = Path(value)
    directory = _dataset.get()
    if not path.is_absolute():
        if directory is None:
            raise ValueError("Relative evidence requires a dataset context")
        resolved = (directory / path).resolve()
        if not resolved.is_relative_to(directory):
            raise ValueError("Evidence path escapes the dataset")
        return resolved
    path = path.resolve()
    if directory is not None:
        for ancestor in (directory, *directory.parents):
            manifest = ancestor / "migrations/relocations.json"
            if manifest.exists():
                for old, new in json.loads(manifest.read_text())["paths"].items():
                    if path.is_relative_to(old):
                        return ancestor / new / path.relative_to(old)
                break
    return path


def ensure_writable(directory):
    directory = Path(directory).resolve()
    ensure_readable(directory)
    for ancestor in (directory, *directory.parents):
        catalog = ancestor / "catalog.json"
        if catalog.exists():
            for relative, entry in json.loads(catalog.read_text()).get("datasets", {}).items():
                if directory.is_relative_to((ancestor / relative).resolve()) and entry.get("archived"):
                    raise ValueError("Archived dataset is reportable only; select a new experiment")


def ensure_readable(directory):
    if any(part in {"quarantine", ".migration_backup", ".migration_staging"} or "contaminated" in part.lower()
           for part in Path(directory).resolve().parts):
        raise ValueError("Quarantined datasets and migration backups cannot be imported or resumed")


def new_experiment(kind):
    from datetime import datetime, timezone
    from uuid import uuid4
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return RESULTS_ROOT / "experiments" / f"{stamp}_{kind}_{uuid4().hex[:6]}"
