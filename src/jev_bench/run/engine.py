"""One sequential scheduling loop for every suite."""
import time
from dataclasses import dataclass
from rich.progress import Progress
from jev_bench.providers.base import ProviderResult, InvocationContext
from jev_bench.runtime.execution import enforce_result, failed_audit
from jev_bench.storage.io import output_lock
from jev_bench.storage.store import is_success
from jev_bench.run_evaluations.validation import validation_on_exit
from jev_bench.storage.paths import dataset_context
from jev_bench.storage.plan import record_selection
from jev_bench.suites import get_suite


@dataclass(frozen=True)
class RunHooks:
    store_factory: object
    measure: object
    report: object
    exception_result: object = None


def execute(hooks, suite, models, cases, repetitions, warmups, root, directory,
            definition, provider_factory, console, guard, prefix_experiment=False,
            systemone_context=None, cache_exception=None):
    spec = get_suite(suite)
    def state(case):
        return case.message if suite == "benchmark" else case
    def key(model, case, repetition):
        return (model, case.case_id, repetition) if suite == "benchmark" else (model, repetition)
    def measured(model, case, repetition, result, latency):
        return (hooks.measure(model, case, repetition, result, latency) if suite == "benchmark"
                else hooks.measure(model, repetition, result, latency))
    def invoke(provider, case, purpose):
        provider.purpose = purpose
        if getattr(provider, "supports_requests", False) is True:
            return provider.invoke(spec.request(state(case)), InvocationContext(
                directory / "execution_audit", purpose, prefix_experiment))
        return provider.invoke(state(case))

    with validation_on_exit(root), output_lock(directory), dataset_context(directory):
        store = hooks.store_factory(directory, definition)
        record_selection(root, suite, models, [c.case_id for c in cases] if suite == "benchmark" else [],
                         repetitions, warmups, definition, guard.maximum, prefix_experiment)
        pending = [(model, case, rep) for model in models for case in cases
                   for rep in range(1, repetitions + 1) if store.pending(*key(model, case, rep))]
        console.print(f"{len(pending)} pending measurements; successful repetitions are skipped.")
        try:
            with Progress(console=console) as progress:
                task = progress.add_task("Measuring", total=len(pending))
                for model in models:
                    work = [(case, rep) for selected, case, rep in pending if selected == model]
                    if not work:
                        continue
                    initialization_failure = None
                    settings = {"audit_directory": directory / "execution_audit"}
                    if suite == "hard_case" and systemone_context is not None:
                        settings["systemone_context"] = systemone_context
                    try:
                        provider = provider_factory(model, **settings)
                        provider.prefix_experiment = prefix_experiment
                    except Exception as exc:
                        if suite == "hard_case":
                            initialization_failure = hooks.exception_result(exc)
                            initialization_failure.error = "Provider initialization failed: " + initialization_failure.error
                        else:
                            initialization_failure = ProviderResult(error=f"Provider initialization failed: {type(exc).__name__}: {exc}")
                        console.print(initialization_failure.error, markup=False)
                    if initialization_failure is None:
                        progress.update(task, description=f"Warming up {model}")
                        for _ in range(warmups):
                            guard.before_call()
                            try:
                                result = enforce_result(invoke(provider, cases[0], "warmup"), model,
                                    {"state": state(cases[0])}, directory / "execution_audit",
                                    allow_unverified_jev=bool(cache_exception))
                            except Exception as exc:
                                failed_audit(model, {"state": state(cases[0])}, directory / "execution_audit", exc, "warmup")
                                result = ProviderResult(error=f"{type(exc).__name__}: {exc}")
                            if result.error:
                                console.print(f"Warm-up failed ({model}): {result.error}", markup=False)
                            guard.after_result(model, result, console)
                    for case, repetition in work:
                        progress.update(task, description=f"{model} · repetition {repetition}")
                        guard.before_call()
                        started = time.perf_counter()
                        interrupted = False
                        try:
                            result = initialization_failure or invoke(provider, case, "measurement")
                        except KeyboardInterrupt as exc:
                            interrupted = True
                            result = ProviderResult(raw_response={"execution_audit": exc.audit} if hasattr(exc, "audit") else None,
                                                    error="KeyboardInterrupt: measurement interrupted")
                        except Exception as exc:
                            result = hooks.exception_result(exc) if suite == "hard_case" else ProviderResult(error=f"{type(exc).__name__}: {exc}")
                        latency = (time.perf_counter() - started) * 1000
                        result = enforce_result(result, model, {"state": state(case)}, directory / "execution_audit",
                                                allow_unverified_jev=bool(cache_exception))
                        store.record(measured(model, case, repetition, result, latency))
                        progress.advance(task)
                        if result.error:
                            console.print(f"Failed {model}, repetition {repetition}: {result.error}", markup=False)
                        if interrupted:
                            raise KeyboardInterrupt
                        guard.after_result(model, result, console)
        finally:
            hooks.report(store, console)
        return sum(not is_success(store.latest[key(model, case, rep)]["validation_success"])
                   for model in models for case in cases for rep in range(1, repetitions + 1))
