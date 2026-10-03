"""Capped hosted Jev launch across both suites. Never run as an offline check."""
import argparse
import csv
import json
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from jev_bench.suites.benchmark.cases import CASES
from jev_bench.run.benchmark import run_benchmark
from jev_bench.run.hard_case import run_hard_case_benchmark
from jev_bench.runtime.execution import atomic_write, fingerprint, now, output_lock, JEV_CACHE_EXCEPTION
from jev_bench.providers.jev import JEV_MODEL, JevBudget
from jev_bench.run.control import RunStopped
from jev_bench.run_evaluations.validation import update_validation
from jev_bench.storage.paths import PROJECT_ROOT, new_experiment, ensure_writable


def run_jev(output_dir=None, repetitions=30, max_new_calls=330,
            budget_usd='0.10', console=None, *, allow_unverified_server_cache=False):
    console = console or Console()
    root = Path(output_dir or new_experiment("jev-api")).resolve()
    ensure_writable(root)
    if type(repetitions) is not int or not 1 <= repetitions <= 30:
        raise ValueError('Jev repetitions must be between 1 and 30')
    budget = JevBudget(root, budget_usd, max_new_calls)
    from jev_bench.providers.jev import configuration
    plan = {'version': 2, 'kind': 'jev_api', 'models': [JEV_MODEL],
            'repetitions': repetitions, 'case_ids': [case.case_id for case in CASES],
            'suites': ['benchmark', 'hard_case'], 'warmups': 0,
            'budget_usd': str(budget.limit), 'max_new_calls': max_new_calls}
    plan['targets'] = {suite: {JEV_MODEL: {'case_ids': plan['case_ids'] if suite == 'benchmark' else [],
        'repetitions': repetitions, 'warmups': 0, 'configuration': configuration()}}
        for suite in plan['suites']}
    if allow_unverified_server_cache:
        plan['jev_cache_exception'] = JEV_CACHE_EXCEPTION
    if not (root / 'run_plan.json').exists() and any(
        (root / suite / 'metadata.json').exists() for suite in plan['suites']):
        raise RunStopped('Existing results have no Jev target manifest; select a new root')
    with output_lock(root / 'orchestration'):
        budget.check()
        for suite in plan['suites']:
            history = root / suite / 'attempt_history.csv'
            if history.exists():
                with history.open(newline='') as handle:
                    if any(row['validation_success'].lower() != 'true' for row in csv.DictReader(handle)):
                        raise RunStopped('Jev already has a rejected attempt; no automatic retry')
        target = root / 'run_plan.json'
        if target.exists():
            if json.loads(target.read_text()) != plan:
                raise RunStopped('Jev target manifest changed; refusing resume')
        else:
            if any((root / suite / 'metadata.json').exists() for suite in plan['suites']):
                raise RunStopped('Existing results have no Jev target manifest; select a new root')
            atomic_write(target, lambda handle: json.dump(plan, handle, indent=2))
        # Initialize the persistent ledger before providers are created.
        with budget.locked() as data:
            budget.save(data)
        outcome = {'started_at_utc': now(), 'run_plan_sha256': fingerprint(plan), 'status': 'incomplete'}
        try:
            settings = dict(repetitions=repetitions, warmups=0, output_dir=root,
                            max_new_calls=max_new_calls, console=console)
            if run_benchmark([JEV_MODEL], list(CASES), **settings):
                raise RunStopped('Jev benchmark rejected; no further calls')
            if run_hard_case_benchmark([JEV_MODEL], **settings):
                raise RunStopped('Jev hard case rejected; no further calls')
            outcome['status'] = 'passed' if update_validation(root)['complete'] else 'incomplete'
        except Exception as error:
            # Provider exceptions may carry sensitive response bodies. Store only type.
            outcome.update(status='stopped', reason=type(error).__name__)
            budget.block('Jev launcher stopped after a failed attempt')
            console.print('Jev stopped. Review saved audit evidence; no retry was attempted.')
        except KeyboardInterrupt:
            outcome['status'] = 'interrupted'
            budget.block('Jev launch interrupted; automatic retry forbidden')
            raise
        finally:
            outcome['finished_at_utc'] = now()
            outcome['validation'] = update_validation(root)
            atomic_write(root / 'jev_run.json', lambda handle: json.dump(outcome, handle, indent=2))
        return 0 if outcome['status'] == 'passed' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path,
                        default=None)
    parser.add_argument('--repetitions', type=int, default=30)
    parser.add_argument('--max-new-calls', type=int, default=330)
    parser.add_argument('--budget-usd', default='0.10')
    parser.add_argument('--allow-unverified-server-cache', action='store_true',
                        help='Accept valid Jev probabilities while marking server caching unverified')
    args = parser.parse_args(argv)
    if args.output_dir is None:
        args.output_dir = new_experiment('jev-api')
        import shlex, sys
        arguments = list(sys.argv[1:] if argv is None else argv)
        print('Resume: ' + shlex.join(['python', '-m', 'jev_bench.run', 'jev-api', *arguments, '--output-dir', str(args.output_dir)]))
    load_dotenv(PROJECT_ROOT / '.env', override=False)
    try:
        return run_jev(args.output_dir, args.repetitions, args.max_new_calls, args.budget_usd,
                       allow_unverified_server_cache=args.allow_unverified_server_cache)
    except KeyboardInterrupt:
        return 130
    except (ValueError, OSError, RunStopped):
        print('Jev launch refused; check target manifest, credentials, and spending ledger.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
