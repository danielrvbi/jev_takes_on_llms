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
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import Progress
from rich.table import Table

from hard_case.loader import HARD_CASE_DIR, PACKET_JSON_BUDGET, compact_records, load_claim_packet
from hard_case.metrics import RAW_COLUMNS, flatten_values, is_success, results_frame, summarize
from hard_case.prompts import SYSTEM_PROMPT, systemone_questions
from hard_case.providers import MODELS, create_provider, model_configuration
from hard_case.providers.base import ProviderResult, exception_result
from hard_case.providers.context import inference_model, validate_alias, validate_request_budget
from hard_case.schemas import HardCaseOutput


DEFAULT_OUTPUT_DIR = HARD_CASE_DIR / "results"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def fingerprint(definition):
    encoded = json.dumps(definition, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def local_model_information(models, systemone_context=None):
    """Bounded, read-only inspection; never pull, load, or reconfigure models."""
    local = [model for model in models if model.startswith(("tev1:", "gemma4:"))]
    if not local:
        return {}
    import httpx
    import ollama

    observed = {}
    client = ollama.Client(timeout=5)
    try:
        installed = {entry.model: entry.digest for entry in client.list().models}
    except (ollama.ResponseError, httpx.HTTPError, ConnectionError):
        return {model: {"inspection_status": "unavailable"} for model in local}
    version = None
    # The installed SDK has no version() method; use the documented read-only endpoint.
    try:
        with httpx.Client(base_url=str(client._client.base_url), timeout=5) as http:
            response = http.get("/api/version")
            response.raise_for_status()
            version = response.json().get("version")
    except (httpx.HTTPError, ValueError):
        pass
    for model in local:
        actual = inference_model(model, systemone_context)
        if actual not in installed:
            observed[model] = {"inspection_status": "not-installed", "server_version": version}
            continue
        try:
            info = client.show(actual)
            observed[model] = {
                "inspection_status": "available",
                "digest": installed[actual],
                "parameters": info.parameters,
                "template": info.template,
                "modelfile": info.modelfile,
                "context_limits": {key: value for key, value in (info.modelinfo or {}).items()
                                   if "context_length" in key},
                "server_version": version,
            }
        except (ollama.ResponseError, httpx.HTTPError, ConnectionError):
            observed[model] = {"inspection_status": "unavailable", "digest": installed[actual],
                               "server_version": version}
    return observed


def experiment_definition(packet, models=MODELS, local_information=None, systemone_context=None):
    dependencies = {}
    for package in ["ollama", "langchain-core", "langchain-ollama", "langchain-mistralai",
                    "pydantic", "pandas", "numpy", "rich", "python-dotenv", "httpx"]:
        try:
            dependencies[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = "not-installed"
    observed = (local_model_information(models, systemone_context)
                if local_information is None else local_information)
    return {
        "kind": "hard_case_benchmark",
        "format_version": 1,
        "case_input": packet.metadata(),
        "input_format": "reviewed compact evidence records with source line references; identical state for all backends",
        "schema": HardCaseOutput.model_json_schema(),
        "prompts": {"llm_system": SYSTEM_PROMPT, "systemone_questions": systemone_questions()},
        "model_configuration": {
            model: {**model_configuration(model, systemone_context), **({"local_model": observed[model]}
                                                    if model in observed else {})}
            for model in models
        },
        "reporting": {
            "probabilities": "successful latest repetitions only; independent, unnormalized",
            "std_ddof": 1,
            "latency_and_tokens": "latest attempts, including failures; warm-ups excluded",
            "percentiles": "linear interpolation",
            "raw_columns": RAW_COLUMNS,
        },
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "ollama_host": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
            "dependencies": dependencies,
        },
    }


def merged_definition(previous, current):
    """Allow model subsets and additions, but preserve every model's recorded settings."""
    old = dict(previous)
    new = dict(current)
    old_models = old.pop("model_configuration", {})
    new_models = new.pop("model_configuration", {})
    if old != new or any(model in old_models and old_models[model] != settings
                         for model, settings in new_models.items()):
        raise ValueError("Incompatible resume metadata; use another --output-dir")
    return {**old, "model_configuration": {**old_models, **new_models}}


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


def write_csv(handle, rows):
    writer = csv.DictWriter(handle, fieldnames=RAW_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)


def row_key(row):
    key = row["model"], int(row["repetition"])
    if not key[0] or key[1] < 1:
        raise ValueError("Invalid repetition key in history")
    return key


@contextmanager
def output_lock(directory):
    directory = Path(directory)
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
    """Append-only history is authoritative; raw.csv contains latest repetitions."""

    def __init__(self, directory, definition):
        if definition.get("case_input", {}).get("profile") != "compact":
            raise ValueError("Only compact experiment metadata is supported")
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        manifest = self.directory / "metadata.json"
        if manifest.exists():
            metadata = json.loads(manifest.read_text(encoding="utf-8"))
            previous = metadata.get("experiment")
            if (metadata.get("kind") != "hard_case_benchmark" or not isinstance(previous, dict)
                    or metadata.get("fingerprint") != fingerprint(previous)):
                raise ValueError("Invalid hard-case metadata or fingerprint")
            if previous.get("case_input", {}).get("profile") != "compact":
                raise ValueError("Incompatible resume: historical non-compact results cannot resume")
            definition = merged_definition(previous, definition)
            changed = metadata["fingerprint"] != fingerprint(definition)
        else:
            if any((self.directory / name).exists() for name in ["raw.csv", "attempt_history.csv"]):
                raise ValueError("Existing results have no metadata; use another --output-dir")
            metadata = {"kind": "hard_case_benchmark", "created_at_utc": utc_now()}
            changed = True

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
                        raise ValueError("Incomplete attempt history row; inspect CSV before resuming")
                    key = row_key(row)
                    prior = self.latest.get(key)
                    if (int(row["attempt"]) != (int(prior["attempt"]) + 1 if prior else 1)
                            or (prior and is_success(prior["validation_success"]))):
                        raise ValueError("Invalid attempt sequence in history")
                    if row["validation_success"].lower() not in {"true", "false"}:
                        raise ValueError("Invalid validation status in history")
                    if is_success(row["validation_success"]):
                        if row["error"]:
                            raise ValueError("Successful history row contains an error")
                        HardCaseOutput.model_validate({name: float(row[name])
                                                       for name in HardCaseOutput.model_fields})
                    self.history.append(row)
                    self.latest[key] = row
        elif (self.directory / "raw.csv").exists():
            raise ValueError("Attempt history is missing; refusing to overwrite existing raw.csv")

        if changed:
            metadata.update(fingerprint=fingerprint(definition), experiment=definition)
            atomic_write(manifest, lambda handle: json.dump(metadata, handle, indent=2, ensure_ascii=False))
        if not self.history_path.exists():
            atomic_write(self.history_path, lambda handle: write_csv(handle, []))
        self.save_raw()

    def save_raw(self):
        atomic_write(self.directory / "raw.csv", lambda handle: write_csv(handle, self.latest.values()))

    def pending(self, model, repetition):
        row = self.latest.get((model, repetition))
        return row is None or not is_success(row["validation_success"])

    def record(self, row):
        key = row_key(row)
        prior = self.latest.get(key)
        if prior and is_success(prior["validation_success"]):
            raise ValueError("Cannot duplicate a completed repetition")
        row = {column: row.get(column) for column in RAW_COLUMNS}
        row["attempt"] = int(prior["attempt"]) + 1 if prior else 1
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


def measured_row(model, repetition, result, latency_ms):
    return {
        "model": model,
        "repetition": repetition,
        "timestamp_utc": utc_now(),
        "latency_ms": latency_ms,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "validation_success": result.output is not None and not result.error,
        "error": result.error,
        "raw_response_json": json.dumps(result.raw_response, ensure_ascii=False, default=str),
        **flatten_values(result.output.model_dump() if result.output is not None else result.values),
    }


def report(store, console):
    frame = results_frame(list(store.latest.values()))
    if frame.empty:
        return None
    summary = summarize(frame, results_frame(store.history))
    atomic_write(store.directory / "summary.csv", lambda handle: summary.to_csv(handle, index=False))
    table = Table(title="Hard-case insurance judgment repeatability")
    for name in ["Model", "Valid", "Latest failures", "Historical failures", "p50 ms", "p95 ms"]:
        table.add_column(name)
    for row in summary.itertuples():
        table.add_row(row.model, f"{row.successful_repetitions}/{row.attempted_repetitions}",
                      str(row.validation_failures), str(row.historical_failures),
                      f"{row.latency_ms_p50:.1f}", f"{row.latency_ms_p95:.1f}")
    console.print(table)
    console.print(f"Six independent probability distributions saved in summary.csv. "
                  f"Results: {store.directory.resolve()}", markup=False)
    return summary


def run_hard_case_benchmark(models, repetitions=30, warmups=2, output_dir=DEFAULT_OUTPUT_DIR,
                            provider_factory=create_provider, console=None, packet=None, definition=None,
                            input_profile="compact", systemone_context=None):
    console = console or Console()
    if input_profile != "compact":
        raise ValueError("Only the compact input profile is supported")
    packet = load_claim_packet(profile=input_profile) if packet is None else packet
    if packet.profile != "compact":
        raise ValueError("Only compact claim packets may be used for inference")
    compact_records(packet.text)  # Reject original narrative even if mislabeled as compact.
    if len(json.dumps(packet.text, ensure_ascii=False).encode("utf-8")) > PACKET_JSON_BUDGET:
        raise ValueError("Compact packet exceeds 50 KiB JSON-encoded budget")
    models = list(dict.fromkeys(models))
    if not models or repetitions < 1 or warmups < 0:
        raise ValueError("Select models, at least one repetition, and nonnegative warm-ups")
    # Validate the actual SDK payload and aliases before creating/overwriting results or inference.
    for model in models:
        actual = inference_model(model, systemone_context)
        if model.startswith("tev1:"):
            validate_request_budget(actual, packet.text)
            if systemone_context is not None:
                validate_alias(model, systemone_context)
    definition = (experiment_definition(packet, models, systemone_context=systemone_context)
                  if definition is None else definition)
    directory = Path(output_dir)
    with output_lock(directory):
        store = ResultStore(directory, definition)
        pending = [(model, repetition) for model in models for repetition in range(1, repetitions + 1)
                   if store.pending(model, repetition)]
        console.print(f"{len(pending)} pending measurements; successful repetitions are skipped.")
        try:
            with Progress(console=console) as progress:
                task = progress.add_task("Measuring", total=len(pending))
                for model in models:
                    work = [repetition for selected, repetition in pending if selected == model]
                    if not work:
                        continue
                    initialization_failure = None
                    try:
                        provider = (provider_factory(model) if systemone_context is None
                                    else provider_factory(model, systemone_context=systemone_context))
                    except Exception as exc:
                        initialization_failure = exception_result(exc)
                        initialization_failure.error = f"Provider initialization failed: {initialization_failure.error}"
                        console.print(initialization_failure.error, markup=False)
                    if initialization_failure is None:
                        progress.update(task, description=f"Warming up {model}")
                        for _ in range(warmups):
                            try:
                                result = provider.invoke(packet.text)
                                if result.error:
                                    console.print(f"Warm-up failed ({model}): {result.error}", markup=False)
                            except Exception as exc:
                                console.print(f"Warm-up failed ({model}): {type(exc).__name__}: {exc}", markup=False)
                    for repetition in work:
                        progress.update(task, description=f"{model} · repetition {repetition}")
                        started = time.perf_counter()
                        interrupted = False
                        try:
                            result = initialization_failure or provider.invoke(packet.text)
                        except KeyboardInterrupt:
                            interrupted = True
                            result = ProviderResult(error="KeyboardInterrupt: measurement interrupted")
                        except Exception as exc:
                            result = exception_result(exc)
                        latency = (time.perf_counter() - started) * 1000
                        store.record(measured_row(model, repetition, result, latency))
                        progress.advance(task)
                        if result.error:
                            console.print(f"Failed {model}, repetition {repetition}: {result.error}", markup=False)
                        if interrupted:
                            raise KeyboardInterrupt
        finally:
            report(store, console)
        return sum(not is_success(store.latest[(model, repetition)]["validation_success"])
                   for model in models for repetition in range(1, repetitions + 1))
