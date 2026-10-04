from jev_bench.storage.paths import PACKAGE_ROOT
from jev_bench.storage.store import ResultStore as SharedResultStore
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from jev_bench.suites.benchmark.cases import CASES
from jev_bench.suites.benchmark.metrics import RAW_COLUMNS, derive, flatten_values
from jev_bench.suites.benchmark.prompts import SYSTEM_PROMPT, systemone_questions
from jev_bench.providers.suites.benchmark import MODELS, create_provider, model_configuration
from jev_bench.suites.benchmark.schemas import DecisionOutput
from jev_bench.runtime.execution import POLICY, output_lock, suite_directory, experiment_execution, audit_fields, model_identity, execution_policy
from jev_bench.run.control import PREFIX_STRATEGY, experiment_settings


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def experiment_definition(models=MODELS):
    dependencies = {}
    for package in ["ollama", "langchain-core", "langchain-ollama", "langchain-mistralai",
                    "pydantic", "pandas", "numpy", "matplotlib", "rich", "python-dotenv", "httpx", "portalocker",
                    *(['langchain-typesafe', 'httpx2'] if 'jev-1.13.0' in models else []),
                    *(['langchain-openai', 'langchain-anthropic', 'openai', 'anthropic', 'httpx2'] if any(m.startswith('azure-') for m in models) else [])]:
        try:
            dependencies[package] = importlib.metadata.version(package) or 'version-unavailable'
        except importlib.metadata.PackageNotFoundError:
            dependencies[package] = "not-installed"
    return {
        **experiment_execution(models), "suite_source_sha256": hashlib.sha256("".join(
            str(p.relative_to(PACKAGE_ROOT / "suites/benchmark")) + hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((PACKAGE_ROOT / "suites/benchmark").rglob("*.py")) if "tests" not in p.parts).encode()).hexdigest(),
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


class ResultStore(SharedResultStore):
    def __init__(self, directory, definition):
        def merge(previous, current):
            if not compatible_definition(previous, current):
                raise ValueError("Incompatible resume metadata; use another --output-dir")
            return previous
        super().__init__(directory, definition, "benchmark", merge)


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


from jev_bench.run_evaluations.benchmark_summary import report


def run_benchmark(models, cases, repetitions=30, warmups=2, output_dir=None,
                  provider_factory=create_provider, console=None, definition=None, *, prefix_experiment=False, max_new_calls=None, call_guard=None):
    console = console or Console()
    if output_dir is None:
        from jev_bench.storage.paths import new_experiment
        output_dir = new_experiment('benchmark')
    models = list(dict.fromkeys(models))
    if not models or not cases or repetitions < 1 or warmups < 0:
        raise ValueError("Select models/cases, positive repetitions and nonnegative warm-ups")
    output_dir, guard = experiment_settings(output_dir, models, prefix_experiment, max_new_calls)
    guard = call_guard if call_guard is not None else guard
    if 'jev-1.13.0' in models:
        from jev_bench.providers.jev import validate_selection
        validate_selection(output_dir, models, warmups, max_new_calls)
    directory = suite_directory(output_dir, "benchmark")
    definition = {**(definition if definition is not None else experiment_definition(models)), "execution_policy": execution_policy(models)}
    cache_exception = None
    if 'jev-1.13.0' in models:
        from jev_bench.providers.jev import cache_exception_for_root
        cache_exception = cache_exception_for_root(output_dir)
    if cache_exception:
        definition = {**definition, "jev_cache_exception": cache_exception}
    if prefix_experiment:
        definition = {**definition, "prefix_strategy": PREFIX_STRATEGY}
    console.print("Jev: server caching unverified; valid responses accepted by explicit exception."
                  if cache_exception else "Azure prefix experiment: verified zero cache-read telemetry; hosted wall-time latency."
                  if any(m.startswith("azure-") for m in models) else "Mandatory cache-free calls. Local latency includes private startup and teardown.")
    from jev_bench.run.engine import execute, RunHooks
    return execute(RunHooks(ResultStore, measured_row, report), 'benchmark', models, cases, repetitions, warmups,
                   Path(output_dir), directory, definition, provider_factory, console, guard,
                   prefix_experiment=prefix_experiment, cache_exception=cache_exception)
