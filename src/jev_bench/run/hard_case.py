from jev_bench.storage.store import ResultStore as SharedResultStore
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from jev_bench.suites.hard_case.loader import PACKET_JSON_BUDGET, compact_records, load_claim_packet
from jev_bench.suites.hard_case.metrics import RAW_COLUMNS, flatten_values
from jev_bench.suites.hard_case.prompts import SYSTEM_PROMPT, systemone_questions
from jev_bench.providers.suites.hard_case import MODELS, create_provider, model_configuration
from jev_bench.providers.suites.hard_case.base import exception_result
from jev_bench.providers.suites.hard_case.context import inference_model, validate_alias, validate_request_budget
from jev_bench.suites.hard_case.schemas import HardCaseOutput
from jev_bench.runtime.execution import POLICY, suite_directory, experiment_execution, audit_fields, model_identity
from jev_bench.run.control import PREFIX_STRATEGY, experiment_settings


from jev_bench.storage.paths import RESULTS_ROOT, PACKAGE_ROOT
DEFAULT_OUTPUT_DIR = RESULTS_ROOT


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
                    "pydantic", "pandas", "numpy", "matplotlib", "rich", "python-dotenv", "httpx",
                    *(['langchain-typesafe', 'httpx2'] if 'jev-1.13.0' in models else [])]:
        try:
            dependencies[package] = importlib.metadata.version(package) or 'version-unavailable'
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = "not-installed"
    observed = (local_model_information(models, systemone_context)
                if local_information is None else local_information)
    if local_information is None:
        for model, info in observed.items():
            info["disk_identity"] = model_identity(inference_model(model, systemone_context))
    return {
        **experiment_execution([inference_model(m, systemone_context) for m in models]),
        "kind": "hard_case_benchmark",
        "suite_source_sha256": hashlib.sha256("".join(
            str(p.relative_to(PACKAGE_ROOT / "suites/hard_case")) + hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((PACKAGE_ROOT / "suites/hard_case").rglob("*.py")) if "tests" not in p.parts).encode()).hexdigest(),
        "format_version": 2,
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


def write_csv(handle, rows):
    writer = csv.DictWriter(handle, fieldnames=RAW_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)


def row_key(row):
    key = row["model"], int(row["repetition"])
    if not key[0] or key[1] < 1:
        raise ValueError("Invalid repetition key in history")
    return key


class ResultStore(SharedResultStore):
    def __init__(self, directory, definition):
        super().__init__(directory, definition, "hard_case", merged_definition)


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
        **audit_fields(result),
        **flatten_values(result.output.model_dump() if result.output is not None else result.values),
    }


from jev_bench.run_evaluations.hard_case_summary import report


def run_hard_case_benchmark(models, repetitions=30, warmups=2, output_dir=None,
                            provider_factory=create_provider, console=None, packet=None, definition=None,
                            input_profile="compact", systemone_context=None, prefix_experiment=False, max_new_calls=None):
    console = console or Console()
    if output_dir is None:
        from jev_bench.storage.paths import new_experiment
        output_dir = new_experiment('hard-case')
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
    output_dir, guard = experiment_settings(output_dir, models, prefix_experiment, max_new_calls)
    if 'jev-1.13.0' in models:
        from jev_bench.providers.jev import validate_selection
        validate_selection(output_dir, models, warmups, max_new_calls)
    # Validate the actual SDK payload and aliases before creating/overwriting results or inference.
    for model in models:
        actual = inference_model(model, systemone_context)
        if model.startswith("tev1:"):
            validate_request_budget(actual, packet.text)
            if systemone_context is not None:
                validate_alias(model, systemone_context)
    definition = (experiment_definition(packet, models, systemone_context=systemone_context)
                  if definition is None else definition)
    definition = {**definition, "execution_policy": POLICY}
    from jev_bench.providers.jev import cache_exception_for_root
    cache_exception = cache_exception_for_root(output_dir) if 'jev-1.13.0' in models else None
    if cache_exception:
        definition = {**definition, "jev_cache_exception": cache_exception}
    if prefix_experiment:
        definition = {**definition, "prefix_strategy": PREFIX_STRATEGY}
    console.print("Jev: server caching unverified; valid responses accepted by explicit exception."
                  if cache_exception else "Mandatory cache-free calls. Local latency includes private startup and teardown.")
    directory = suite_directory(output_dir, "hard_case")
    from jev_bench.run.engine import execute, RunHooks
    return execute(RunHooks(ResultStore, measured_row, report, exception_result), 'hard_case', models, [packet.text], repetitions, warmups,
                   Path(output_dir), directory, definition, provider_factory, console, guard,
                   prefix_experiment=prefix_experiment, cache_exception=cache_exception, systemone_context=systemone_context)
