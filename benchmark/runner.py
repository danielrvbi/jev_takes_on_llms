import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import time
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
from benchmark.providers import MODELS, create_provider, model_configuration
from benchmark.providers.base import ProviderResult
from benchmark.schemas import DecisionOutput
from execution import (POLICY, atomic_write, output_lock, suite_directory, experiment_execution,
                       enforce_result, audit_fields, revalidate_row, model_identity, failed_audit)
from validation import validation_on_exit
from run_control import PREFIX_STRATEGY, experiment_settings


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def experiment_definition(models=MODELS):
    dependencies = {}
    for package in ["ollama", "langchain-core", "langchain-ollama", "langchain-mistralai",
                    "pydantic", "pandas", "numpy", "matplotlib", "rich", "python-dotenv", "httpx",
                    *(['langchain-typesafe', 'httpx2'] if 'jev-1.13.0' in models else [])]:
        try:
            dependencies[package] = importlib.metadata.version(package) or 'version-unavailable'
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = "not-installed"
    return {
        **experiment_execution(models), "suite_source_sha256": hashlib.sha256("".join(
            str(p.relative_to(Path(__file__).resolve().parent)) + hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(__file__).resolve().parent.rglob("*.py")) if "tests" not in p.parts).encode()).hexdigest(),
        "format_version": 2, "cases": [asdict(case) for case in CASES],
        "schema": DecisionOutput.model_json_schema(),
        "prompts": {"llm_system": SYSTEM_PROMPT, "systemone_questions": systemone_questions()},
        "model_configuration": {
            model: {**model_configuration(model),
                    **({"local_model": model_identity(model)} if model.startswith(("tev1:", "gemma4:")) else {})} for model in models
        },
        "derivation": {"binary_threshold": 0.5, "entropy_base": 2,
                       "distribution_normalization": "derived calculations only", "std_ddof": 1},
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "ollama_host": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                        "dependencies": dependencies},
    }


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


class ResultStore:
    """History is authoritative; raw.csv is the latest row for each repetition."""

    def __init__(self, directory, definition):
        if definition.get("execution_policy") != POLICY:
            raise ValueError("Incompatible resume: mandatory cache-free metadata required")
        self.definition = definition
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        manifest = self.directory / "metadata.json"
        encoded = json.dumps(definition, sort_keys=True, ensure_ascii=False)
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        if manifest.exists():
            previous = json.loads(manifest.read_text())
            if previous.get("fingerprint") != hashlib.sha256(json.dumps(previous.get("experiment"), sort_keys=True, ensure_ascii=False).encode()).hexdigest():
                raise ValueError("Invalid benchmark metadata fingerprint")
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
                    if is_success(row["validation_success"]):
                        revalidate_row(row, self.definition)
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
        if is_success(row["validation_success"]):
            revalidate_row(row, self.definition)
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
        **audit_fields(result),
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
                  provider_factory=create_provider, console=None, definition=None, *, prefix_experiment=False, max_new_calls=None):
    console = console or Console()
    models = list(dict.fromkeys(models))
    if not models or not cases or repetitions < 1 or warmups < 0:
        raise ValueError("Select models/cases, positive repetitions and nonnegative warm-ups")
    output_dir, guard = experiment_settings(output_dir, models, prefix_experiment, max_new_calls)
    if 'jev-1.13.0' in models:
        from jev_execution import validate_selection
        validate_selection(output_dir, models, warmups, max_new_calls)
    directory = suite_directory(output_dir, "benchmark")
    definition = {**(definition if definition is not None else experiment_definition(models)), "execution_policy": POLICY}
    from jev_execution import cache_exception_for_root
    cache_exception = cache_exception_for_root(output_dir) if 'jev-1.13.0' in models else None
    if cache_exception:
        definition = {**definition, "jev_cache_exception": cache_exception}
    if prefix_experiment:
        definition = {**definition, "prefix_strategy": PREFIX_STRATEGY}
    console.print("Jev: server caching unverified; valid responses accepted by explicit exception."
                  if cache_exception else "Mandatory cache-free calls. Local latency includes private startup and teardown.")
    with validation_on_exit(Path(output_dir)), output_lock(directory):
        store = ResultStore(directory, definition)
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
                        provider = provider_factory(model, audit_directory=directory / "execution_audit")
                    except Exception as exc:
                        initialization_error = f"Provider initialization failed: {type(exc).__name__}: {exc}"
                        console.print(initialization_error, markup=False)
                    if not initialization_error:
                        progress.update(task, description=f"Warming up {model}")
                        provider.prefix_experiment = prefix_experiment
                        provider.purpose = "warmup"
                        for _ in range(warmups):
                            guard.before_call()
                            try:
                                result = enforce_result(provider.invoke(cases[0].message), model, {"state": cases[0].message}, directory / "execution_audit", allow_unverified_jev=bool(cache_exception))
                                if result.error:
                                    console.print(f"Warm-up failed ({model}): {result.error}", markup=False)
                            except Exception as exc:
                                failed_audit(model, {"state": cases[0].message}, directory / "execution_audit", exc, "warmup")
                                result = ProviderResult(error=f"{type(exc).__name__}: {exc}")
                                console.print(f"Warm-up failed ({model}): {type(exc).__name__}: {exc}", markup=False)
                            guard.after_result(model, result, console)
                    if not initialization_error:
                        provider.purpose = "measurement"
                    for case, repetition in work:
                        progress.update(task, description=f"{model} · case {case.case_id} · repetition {repetition}")
                        guard.before_call()
                        started = time.perf_counter()
                        interrupted = False
                        try:
                            result = (ProviderResult(error=initialization_error) if initialization_error
                                      else provider.invoke(case.message))
                        except KeyboardInterrupt as exc:
                            interrupted = True
                            result = ProviderResult(raw_response={"execution_audit": exc.audit} if hasattr(exc, "audit") else None,
                                                    error="KeyboardInterrupt: measurement interrupted")
                        except Exception as exc:
                            result = ProviderResult(error=f"{type(exc).__name__}: {exc}")
                        latency = (time.perf_counter() - started) * 1000
                        result = enforce_result(result, model, {"state": case.message}, directory / "execution_audit", allow_unverified_jev=bool(cache_exception))
                        store.record(measured_row(model, case, repetition, result, latency))
                        progress.advance(task)
                        if result.error:
                            console.print(f"Failed {model}, case {case.case_id}, repetition {repetition}: {result.error}", markup=False)
                        if interrupted:
                            raise KeyboardInterrupt
                        guard.after_result(model, result, console)
        finally:
            report(store, console)
        return sum(not is_success(store.latest[(model, case.case_id, repetition)]["validation_success"])
                   for model in models for case in cases for repetition in range(1, repetitions + 1))
