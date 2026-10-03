"""One capped, sequential Mistral prefix pilot; never launches a bulk run."""
import argparse
import csv
import json
from pathlib import Path

from dotenv import load_dotenv
from rich.console import Console

from jev_bench.suites.benchmark.cases import CASES
from jev_bench.run.benchmark import run_benchmark
from jev_bench.run.hard_case import run_hard_case_benchmark
from jev_bench.runtime.execution import atomic_write, output_lock, now
from jev_bench.run.control import RunStopped
from jev_bench.run_evaluations.validation import update_validation
from jev_bench.storage.paths import PROJECT_ROOT, new_experiment, ensure_writable


def run_pilot(output_dir=None, console=None, *, direct=False):
    console = console or Console()
    if output_dir is None:
        output_dir, direct = new_experiment('prefix-pilot'), True
    root = Path(output_dir).resolve()
    pilot = root if direct else root / 'prefix_pilot'
    ensure_writable(pilot)
    with output_lock(pilot / 'orchestration'):
        if direct and not (pilot / 'run_plan.json').exists():
            targets = {suite: {model: {'case_ids': [1] if suite == 'benchmark' else [],
                'repetitions': 2, 'warmups': 0, 'configuration': None}
                for model in ('mistral-small-latest', 'mistral-large-latest')}
                for suite in ('benchmark', 'hard_case')}
            atomic_write(pilot / 'run_plan.json', lambda h: json.dump(
                {'version': 2, 'kind': 'prefix_pilot', 'targets': targets, 'maximum_paid_calls': 8}, h, indent=2))
        prior = []
        for suite in ('benchmark', 'hard_case'):
            path = pilot / suite / 'attempt_history.csv'
            if path.exists():
                with path.open(newline='') as handle:
                    prior.extend(csv.DictReader(handle))
        if any(row['validation_success'].lower() != 'true' for row in prior):
            raise RunStopped('Pilot already has a rejected attempt; no retry or replacement calls allowed')
        if len(prior) > 8:
            raise RunStopped('Pilot already exceeds its eight-call budget')
        outcome = {'started_at_utc': now(), 'maximum_paid_calls': 8, 'status': 'incomplete'}
        try:
            for suite, model in [('benchmark', 'mistral-small-latest'), ('benchmark', 'mistral-large-latest'),
                                 ('hard_case', 'mistral-small-latest'), ('hard_case', 'mistral-large-latest')]:
                console.print(f'Pilot: {suite} / {model}; two repetitions, zero warm-ups', markup=False)
                settings = dict(repetitions=2, warmups=0, output_dir=root, console=console,
                                prefix_experiment=True, max_new_calls=2)
                if suite == 'benchmark':
                    failures = run_benchmark([model], [CASES[0]], **settings)
                else:
                    failures = run_hard_case_benchmark([model], systemone_context=262144, **settings)
                if failures:
                    raise RunStopped('Pilot measurements failed; no further calls')
            manifest = update_validation(pilot)
            if not manifest['complete']:
                raise RunStopped('Pilot audit validation incomplete')
            outcome['status'] = 'passed'
        except RunStopped as exc:
            outcome['reason'] = str(exc)
            console.print(str(exc), markup=False)
        except KeyboardInterrupt:
            outcome['reason'] = 'Interrupted; no further calls'
            raise
        except Exception as exc:
            outcome['reason'] = f'{type(exc).__name__}: {exc}'
            console.print(outcome['reason'], markup=False)
        finally:
            outcome['finished_at_utc'] = now()
            outcome['validation'] = update_validation(pilot)
            if outcome['status'] != 'passed':
                support = ('# Mistral prompt-cache control question\n\n'
                    'We need independent repeated inference with unchanged prompts. Fresh prompt_cache_key values did not prevent cache hits. '
                    'A separately identified pilot prepended a unique UUID to the first system message and still required explicit numeric cached_tokens=0.\n\n'
                    'Does the chat endpoint support a documented request- or organization-level cache-disable control? '
                    'Can response-schema content or an internal system prefix be cached before our first system message? '
                    'What is the cache lifetime, and how should cached-token telemetry be interpreted for these models?\n\n'
                    'Pilot outcome: ' + outcome.get('reason', 'incomplete') + '\n\n'
                    'Local evidence: pilot.json, validation.json, and linked execution_audit sidecars. '
                    'Review and redact prompts before sharing any sidecars. This draft has not been sent.\n')
                atomic_write(pilot / 'provider_support_question.md', lambda f: f.write(support))
            atomic_write(pilot / 'pilot.json', lambda f: json.dump(outcome, f, indent=2))
        return 0 if outcome['status'] == 'passed' else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=None)
    args = parser.parse_args(argv)
    if args.output_dir is None:
        args.output_dir = new_experiment('prefix-pilot')
        import shlex, sys
        print('Resume: ' + shlex.join(['python', '-m', 'jev_bench.run', 'prefix-pilot', '--output-dir', str(args.output_dir)]))
    load_dotenv(PROJECT_ROOT / '.env', override=False)
    try:
        return run_pilot(args.output_dir, direct=True)
    except RunStopped as exc:
        print(exc)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
