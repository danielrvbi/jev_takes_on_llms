import csv
import fcntl
import hashlib
import importlib.metadata
import json
import os
import platform
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from benchmark.cases import CASES
from benchmark.metrics import RAW_COLUMNS, derive, flatten_values, is_success, results_frame, summarize
from benchmark.plots import plot_results
from benchmark.prompts import SYSTEM_PROMPT, systemone_questions
from benchmark.providers import MODELS, create_provider
from benchmark.providers.base import ProviderResult
from benchmark.schemas import DecisionOutput


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def experiment_definition():
    dependencies = {}
    for package in ["ollama", "langchain-core", "langchain-ollama", "langchain-mistralai",
                    "pydantic", "pandas", "numpy", "matplotlib", "rich", "python-dotenv"]:
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = "not-installed"
    return {
        "format_version": 1, "cases": [asdict(case) for case in CASES],
        "schema": DecisionOutput.model_json_schema(),
        "prompts": {"llm_system": SYSTEM_PROMPT, "systemone_questions": systemone_questions()},
        "model_configuration": {
            model: {"provider": "systemone" if model.startswith("tev1:") else "langchain",
                    "temperature": None if model.startswith("tev1:") else 0,
                    "structured_method": "native" if model.startswith("tev1:") else "json_schema",
                    "seed": None, "automatic_retries": 0} for model in MODELS
        },
        "derivation": {"binary_threshold": 0.5, "entropy_base": 2,
                       "distribution_normalization": "derived calculations only", "std_ddof": 1},
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "ollama_host": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                        "dependencies": dependencies},
    }


def atomic_write(path, write):
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", newline="", encoding="utf-8") as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def compatible_definition(previous, current):
    """Retiring models is compatible; changes to retained models or methodology are not.

    Keep original metadata as provenance. Removing an unused registry entry must
    not force users to discard successful measurements from the remaining models.
    """
    if not isinstance(previous, dict):
        return False
    old = dict(previous)
    new = dict(current)
    old_models = old.pop("model_configuration", {})
    new_models = new.pop("model_configuration", {})
    return old == new and all(model in old_models and configuration == old_models[model]
                              for model, configuration in new_models.items())


def write_csv(handle, rows):
    writer = csv.DictWriter(handle, fieldnames=RAW_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)


def row_key(row):
    return row["model"], int(row["case_id"]), int(row["repetition"])


@contextmanager
def output_lock(directory):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".benchmark.lock").open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("Another benchmark is writing to this output directory") from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class ResultStore:
    """History is authoritative; raw.csv is the latest row for each repetition."""

    def __init__(self, directory, definition):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        manifest = self.directory / "metadata.json"
        encoded = json.dumps(definition, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        if manifest.exists():
            previous = json.loads(manifest.read_text())
            if previous.get("kind") == "reconciled_evaluation":
                raise ValueError(
                    "This is an evaluation-only reconciled dataset. "
                    "Resume measurements in the original source directories listed in metadata.json."
                )
            if (previous.get("fingerprint") != fingerprint
                    and not compatible_definition(previous.get("experiment"), definition)):
                raise ValueError("Incompatible resume metadata; use another --output-dir")
        else:
            if any((self.directory / name).exists() for name in ["raw.csv", "attempt_history.csv"]):
                raise ValueError("Existing results have no metadata; use another --output-dir")
            atomic_write(manifest, lambda handle: json.dump(
                {"created_at_utc": utc_now(), "fingerprint": fingerprint, "experiment": definition},
                handle, indent=2, ensure_ascii=False))
        self.history = []
        self.latest = {}
        self.history_path = self.directory / "attempt_history.csv"
        if self.history_path.exists():
            with self.history_path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != RAW_COLUMNS:
                    raise ValueError("Incompatible attempt history CSV columns")
                for row in reader:
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError("Incomplete attempt history row; inspect the CSV before resuming")
                    key = row_key(row)
                    previous = self.latest.get(key)
                    if int(row["attempt"]) != (int(previous["attempt"]) + 1 if previous else 1):
                        raise ValueError("Invalid attempt sequence in history")
                    self.history.append(row)
                    self.latest[key] = row
        elif (self.directory / "raw.csv").exists():
            raise ValueError("Attempt history is missing; refusing to overwrite existing raw.csv")
        self.save_raw()

    def save_raw(self):
        atomic_write(self.directory / "raw.csv", lambda handle: write_csv(handle, self.latest.values()))

    def pending(self, model, case_id, repetition):
        row = self.latest.get((model, case_id, repetition))
        return row is None or not is_success(row["validation_success"])

    def record(self, row):
        key = row_key(row)
        previous = self.latest.get(key)
        if previous and is_success(previous["validation_success"]):
            raise ValueError("Cannot duplicate a completed repetition")
        row = {column: row.get(column) for column in RAW_COLUMNS}
        row["attempt"] = int(previous["attempt"]) + 1 if previous else 1
        needs_header = not self.history_path.exists() or self.history_path.stat().st_size == 0
        with self.history_path.open("a", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=RAW_COLUMNS)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
        self.latest[key] = row
        self.history.append(row)
        self.save_raw()


def measured_row(model, case, repetition, result, latency_ms):
    row = {
        "model": model, "case_id": case.case_id, "message": case.message,
        "repetition": repetition, "timestamp_utc": utc_now(), "latency_ms": latency_ms,
        "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
        "validation_success": result.output is not None and not result.error,
        "error": result.error,
        "raw_response_json": json.dumps(result.raw_response, ensure_ascii=False, default=str),
        **flatten_values(result.values),
    }
    if row["validation_success"]:
        row.update(derive(result.output))
    return row


def report(store, console):
    frame = results_frame(list(store.latest.values()))
    if frame.empty:
        return None
    summary = summarize(frame, results_frame(store.history))
    atomic_write(store.directory / "summary.csv", lambda handle: summary.to_csv(handle, index=False))
    plot_results(frame, summary, store.directory)
    table = Table(title="Structured decision repeatability / distribution")
    for column in ["Model", "Case", "Valid", "Web", "Safe", "Fresh", "Route", "p95 ms"]:
        table.add_column(column, justify="left" if column == "Model" else "right", no_wrap=True)
    def number(value, specification=".3f"):
        return "—" if value != value else format(value, specification)
    for row in summary.itertuples():
        table.add_row(row.model, str(row.case_id), f"{row.successful_repetitions}/{row.attempted_repetitions}",
                      number(row.requires_web_probability_mean), number(row.is_safe_probability_mean),
                      number(row.expected_freshness_mean), number(row.route_consistency, ".0%"),
                      number(row.latency_ms_p95, ".1f"))
    console.print(table)
    console.print("Web/Safe/Fresh: means; Route: consistency; Valid: valid/attempted.")
    console.print(f"Latest failures: {int(summary.validation_failures.sum())}; "
                  f"historical failures: {int(summary.historical_failures.sum())}. "
                  f"Results: {store.directory.resolve()}", markup=False)
    return summary


def run_benchmark(models, cases, repetitions=30, warmups=2, output_dir="results",
                  provider_factory=create_provider, console=None, definition=None):
    console = console or Console()
    directory = Path(output_dir)
    with output_lock(directory):
        store = ResultStore(directory, definition if definition is not None else experiment_definition())
        pending = [(model, case, repetition) for model in models for case in cases
                   for repetition in range(1, repetitions + 1)
                   if store.pending(model, case.case_id, repetition)]
        console.print(f"{len(pending)} pending measurements; successful repetitions are skipped.")
        try:
            with Progress(console=console) as progress:
                task = progress.add_task("Measuring", total=len(pending))
                for model in models:
                    work = [(case, repetition) for selected_model, case, repetition in pending if selected_model == model]
                    if not work:
                        continue
                    initialization_error = ""
                    try:
                        provider = provider_factory(model)
                    except Exception as exc:
                        initialization_error = f"Provider initialization failed: {type(exc).__name__}: {exc}"
                        console.print(initialization_error, markup=False)
                    if not initialization_error:
                        progress.update(task, description=f"Warming up {model}")
                        for _ in range(warmups):
                            try:
                                result = provider.invoke(cases[0].message)
                                if result.error:
                                    console.print(f"Warm-up failed ({model}): {result.error}", markup=False)
                            except Exception as exc:
                                console.print(f"Warm-up failed ({model}): {type(exc).__name__}: {exc}", markup=False)
                    for case, repetition in work:
                        progress.update(task, description=f"{model} · case {case.case_id} · repetition {repetition}")
                        started = time.perf_counter()
                        interrupted = False
                        try:
                            result = (ProviderResult(error=initialization_error) if initialization_error
                                      else provider.invoke(case.message))
                        except KeyboardInterrupt:
                            interrupted = True
                            result = ProviderResult(error="KeyboardInterrupt: measurement interrupted")
                        except Exception as exc:
                            result = ProviderResult(error=f"{type(exc).__name__}: {exc}")
                        latency = (time.perf_counter() - started) * 1000
                        store.record(measured_row(model, case, repetition, result, latency))
                        progress.advance(task)
                        if result.error:
                            console.print(f"Failed {model}, case {case.case_id}, repetition {repetition}: {result.error}", markup=False)
                        if interrupted:
                            raise KeyboardInterrupt
        finally:
            report(store, console)
        return sum(not is_success(store.latest[(model, case.case_id, repetition)]["validation_success"])
                   for model in models for case in cases for repetition in range(1, repetitions + 1))
