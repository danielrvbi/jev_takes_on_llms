"""Capped Azure prefix pilot and full experiments through the existing suites."""
import argparse
import csv
import json
from pathlib import Path
import shlex
import tempfile

from dotenv import load_dotenv
from rich.console import Console

from jev_bench.providers.azure_config import AZURE_MODELS, api_key, configuration
from jev_bench.run.control import CallGuard, PREFIX_STRATEGY, RunStopped
from jev_bench.runtime.azure import AZURE_POLICY
from jev_bench.runtime.execution import fingerprint
from jev_bench.storage.io import atomic_write, output_lock
from jev_bench.storage.paths import PROJECT_ROOT, ensure_writable


def definitions():
    from jev_bench.run.benchmark import experiment_definition as benchmark
    from jev_bench.run.hard_case import experiment_definition as hard_case
    from jev_bench.suites.hard_case.loader import load_claim_packet
    return {"benchmark": {**benchmark(AZURE_MODELS), "prefix_strategy": PREFIX_STRATEGY},
            "hard_case": {**hard_case(load_claim_packet(), AZURE_MODELS), "prefix_strategy": PREFIX_STRATEGY}}


def run_plan(phase, definitions):
    repetitions = 2 if phase == "pilot" else 30
    cases = [1] if phase == "pilot" else list(range(1, 11))
    return {"version": 2, "kind": f"azure_prefix_{phase}", "phase": phase,
            "execution_policy": AZURE_POLICY, "prefix_strategy": PREFIX_STRATEGY,
            "compatibility_sha256": fingerprint(definitions), "suites": ["benchmark", "hard_case"],
            "maximum_paid_calls": len(AZURE_MODELS) * repetitions * (len(cases) + 1),
            "targets": {suite: {model: {"case_ids": cases if suite == "benchmark" else [],
                "repetitions": repetitions, "warmups": 0, "configuration": definition["model_configuration"][model]}
                for model in AZURE_MODELS} for suite, definition in definitions.items()}}


def verify_pilot(pilot, compatibility):
    from jev_bench.run_evaluations.validation import validate_saved
    pilot = Path(pilot).resolve()
    saved = json.loads((pilot / "run_plan.json").read_text(encoding="utf-8"))
    if saved.get("kind") != "azure_prefix_pilot" or saved.get("compatibility_sha256") != compatibility:
        raise ValueError("A successful pilot with identical deployments, settings and implementation is required")
    observed = {}
    for suite in ('benchmark', 'hard_case'):
        metadata = json.loads((pilot / suite / 'metadata.json').read_text(encoding="utf-8"))
        observed[suite] = metadata['experiment']
        if metadata.get('fingerprint') != fingerprint(observed[suite]):
            raise ValueError('Pilot metadata fingerprint mismatch')
    if fingerprint(observed) != compatibility:
        raise ValueError('Pilot definitions differ from recorded compatibility settings')
    with tempfile.TemporaryDirectory(prefix="jev-azure-pilot-validation-") as directory:
        manifest = validate_saved(pilot, Path(directory))
    if not manifest["complete"] or manifest["expected_measurements"] != len(AZURE_MODELS) * 4:
        raise ValueError("Azure pilot is incomplete or contains rejected evidence; inspect its validation.json")


def check_history(root):
    """Interruptions can resume; provider/cache/schema rejections require review."""
    for suite in ("benchmark", "hard_case"):
        path = root / suite / "attempt_history.csv"
        if not path.exists():
            continue
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row['validation_success'].lower() != 'true':
                    from jev_bench.runtime.execution import revalidate_audit
                    raw = json.loads(row['raw_response_json'])
                    audit = revalidate_audit(raw['execution_audit'], require_verified=False, directory=root / suite)
                    if not audit.get('interrupted'):
                        raise ValueError("Saved Azure rejection blocks automatic resume; inspect evidence and use a new output directory")


def run_azure(phase, output_dir, *, pilot_dir=None, max_new_calls=None, provider_factory=None, console=None):
    if phase not in ('pilot', 'full'):
        raise ValueError('Azure phase must be pilot or full')
    console = console or Console()
    root = Path(output_dir).resolve()
    ensure_writable(root)
    for model in AZURE_MODELS:
        configuration(model)
        api_key(model)
    configs = definitions()
    for model, config in configs['benchmark']['model_configuration'].items():
        console.print(f"{model}: deployment={config['deployment']}", markup=False)
    plan = run_plan(phase, configs)
    cap = plan["maximum_paid_calls"] if max_new_calls is None else max_new_calls
    if type(cap) is not int or not 1 <= cap <= plan["maximum_paid_calls"]:
        raise ValueError(f"Call cap must be between 1 and {plan['maximum_paid_calls']}")
    if phase == "full":
        if pilot_dir is None or Path(pilot_dir).resolve() == root:
            raise ValueError("Full execution requires a separate --pilot-dir")
        verify_pilot(pilot_dir, plan["compatibility_sha256"])
    from jev_bench.run.benchmark import ResultStore as BenchmarkStore, run_benchmark
    from jev_bench.run.hard_case import ResultStore as HardStore, run_hard_case_benchmark
    from jev_bench.suites.benchmark.cases import CASES
    from jev_bench.run_evaluations.validation import update_validation
    guard = CallGuard(cap, prefix_experiment=True)
    # An outer orchestration lock coordinates both suites and all invocations.
    with output_lock(root / "orchestration"):
        path = root / "run_plan.json"
        if path.exists():
            if json.loads(path.read_text(encoding="utf-8")) != plan:
                raise ValueError("Azure plan/configuration changed; use a new --output-dir")
        else:
            if any((root / suite).exists() for suite in configs):
                raise ValueError("Existing suite data has no Azure plan; use a new --output-dir")
            atomic_write(path, lambda handle: json.dump(plan, handle, indent=2))
        check_history(root)
        for suite, factory in (("benchmark", BenchmarkStore), ("hard_case", HardStore)):
            with output_lock(root / suite):
                factory(root / suite, configs[suite])
        update_validation(root)
        settings = dict(models=AZURE_MODELS, repetitions=2 if phase == "pilot" else 30,
                        warmups=0, output_dir=root, prefix_experiment=True,
                        max_new_calls=cap, call_guard=guard, console=console)
        if provider_factory is not None:
            settings["provider_factory"] = provider_factory
        try:
            run_benchmark(cases=CASES[:1] if phase == "pilot" else CASES,
                          definition=configs["benchmark"], **settings)
            run_hard_case_benchmark(definition=configs["hard_case"], **settings)
        finally:
            manifest = update_validation(root)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("pilot", "full"), required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--pilot-dir", type=Path, default=PROJECT_ROOT / "results/experiments/azure_prefix_pilot")
    parser.add_argument("--max-new-calls", type=int, help="cap sends across both suites for this invocation")
    parser.add_argument("--check-config", action="store_true", help="validate configuration offline; make no calls or result writes")
    args = parser.parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    console = Console()
    try:
        if args.check_config:
            settings = {model: configuration(model) for model in AZURE_MODELS}
            for model in AZURE_MODELS:
                api_key(model)
            console.print(json.dumps(settings, indent=2), markup=False)
            return 0
        root = args.output_dir or PROJECT_ROOT / f"results/experiments/azure_prefix_{'pilot' if args.phase == 'pilot' else '30'}"
        command = ['python', '-m', 'jev_bench.run', 'azure', '--phase', args.phase, '--output-dir', str(root)]
        if args.phase == 'full':
            command.extend(['--pilot-dir', str(args.pilot_dir)])
        if args.max_new_calls is not None:
            command.extend(['--max-new-calls', str(args.max_new_calls)])
        import os
        if os.name == 'nt':
            resume = ' '.join("'" + argument.replace("'", "''") + "'" for argument in command)
        else:
            resume = shlex.join(command)
        console.print('Resume: ' + resume, markup=False)
        manifest = run_azure(args.phase, root, pilot_dir=args.pilot_dir, max_new_calls=args.max_new_calls, console=console)
        return 0 if manifest['complete'] else 1
    except KeyboardInterrupt:
        console.print('Interrupted. Verified successes will be skipped on resume.')
        return 130
    except (ValueError, OSError, RunStopped) as exc:
        console.print(f'Azure run stopped: {exc}', markup=False)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
