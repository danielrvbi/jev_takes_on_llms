"""Root manifest: complete only when all 55 requested repetition-one keys pass."""
import csv
from jev_bench.storage.locking import fcntl
from contextlib import ExitStack, contextmanager
import json
from pathlib import Path
from jev_bench.storage.paths import dataset_context, ensure_writable, ensure_readable

from jev_bench.runtime.execution import POLICY, atomic_write, now, output_lock, revalidate_audit, revalidate_row


def _build_validation(root, output_directory=None):
    root = Path(root).resolve()
    ensure_readable(root)
    from jev_bench.providers.suites.benchmark import LEGACY_MODELS as MODELS
    from jev_bench.suites.benchmark.schemas import DecisionOutput
    from jev_bench.suites.benchmark.metrics import PROBABILITY_COLUMNS, ROUTE_COLUMNS, FRESHNESS_COLUMNS
    from jev_bench.suites.hard_case.schemas import HardCaseOutput
    expected = {
        'benchmark': {(m, c, 1) for m in MODELS for c in range(1, 11)},
        'hard_case': {(m, 1) for m in MODELS},
    }
    run_plan_path = root / 'run_plan.json'
    run_plan = json.loads(run_plan_path.read_text()) if run_plan_path.exists() else None
    if run_plan is not None and run_plan.get('version') == 2:
        from jev_bench.storage.plan import expected_keys
        expected = expected_keys(run_plan)
        if run_plan.get('kind') == 'jev_api':
            from jev_bench.providers.jev_contract import cache_exception_for_root
            cache_exception_for_root(root)
    elif run_plan is not None:
        from jev_bench.providers.jev_contract import JEV_MODEL
        if (run_plan.get('version') != 1 or run_plan.get('kind') != 'jev_api'
                or run_plan.get('models') != [JEV_MODEL]
                or type(run_plan.get('repetitions')) is not int
                or not 1 <= run_plan['repetitions'] <= 30
                or run_plan.get('case_ids') != list(range(1, 11))
                or run_plan.get('suites') != ['benchmark', 'hard_case']):
            raise ValueError('Invalid Jev target manifest')
        repetitions = range(1, run_plan['repetitions'] + 1)
        expected = {'benchmark': {(JEV_MODEL, c, r) for c in run_plan['case_ids'] for r in repetitions},
                    'hard_case': {(JEV_MODEL, r) for r in repetitions}}
        from jev_bench.providers.jev_contract import cache_exception_for_root
        cache_exception_for_root(root)
    prefix = any(json.loads((root / suite / 'metadata.json').read_text()).get('experiment', {}).get('prefix_strategy')
                 for suite in expected if (root / suite / 'metadata.json').exists())
    if prefix and not (run_plan or {}).get('version') == 2:
        mistral = [m for m in MODELS if m.startswith('mistral')]
        expected = {'benchmark': {(m, 1, r) for m in mistral for r in (1, 2)},
                    'hard_case': {(m, r) for m in mistral for r in (1, 2)}}
    target = sum(map(len, expected.values()))
    manifest = {'experiment': run_plan.get('kind', 'original') if run_plan else 'prefix_pilot' if prefix else 'original', 'policy': (run_plan or {}).get('execution_policy', POLICY), 'updated_at_utc': now(), 'complete': False,
                'expected_measurements': target, 'valid_measurements': 0, 'suites': {}}
    if run_plan and run_plan.get('jev_cache_exception'):
        manifest['jev_cache_exception'] = run_plan['jev_cache_exception']
        manifest['cache_verified'] = False
    call_ids, cache_keys, prefixes = set(), set(), set()
    for suite in expected:
        directory = root / suite
        info = {'expected_keys': len(expected[suite]), 'measurement_keys': 0,
                'passed': 0, 'failures': [], 'missing': [], 'attempts': 0, 'linked_attempts': 0,
                'token_usage': []}
        path = directory / 'attempt_history.csv'
        latest = {}
        metadata_path = directory / "metadata.json"
        metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
        definition = metadata.get("experiment")
        if definition is not None and (definition.get('jev_cache_exception') !=
                (run_plan or {}).get('jev_cache_exception')):
            raise ValueError('Jev cache exception differs between target and suite metadata')
        if path.exists():
            with path.open(newline='', encoding='utf-8') as f:
                rows = list(csv.DictReader(f))
            for row in rows:
                key = ((row['model'], int(row['case_id']), int(row['repetition'])) if suite == 'benchmark'
                       else (row['model'], int(row['repetition'])))
                latest[key] = row
                info['attempts'] += 1
                try:
                    raw = json.loads(row['raw_response_json'])
                    link = raw['execution_audit']
                    with dataset_context(directory):
                        audit = revalidate_audit(link, raw, require_verified=False)
                    if audit['call_id'] in call_ids:
                        raise ValueError('Execution audit reused for multiple measurements')
                    call_ids.add(audit['call_id'])
                    if audit.get('prompt_cache_key'):
                        if audit['prompt_cache_key'] in cache_keys:
                            raise ValueError('Mistral cache key reused')
                        cache_keys.add(audit['prompt_cache_key'])
                    if prefix:
                        if not audit['input'].get('prefix_strategy'):
                            raise ValueError('Pilot audit has no unique prefix strategy')
                        identifier = audit['input']['messages'][0][1].split('\n', 1)[0]
                        if identifier in prefixes:
                            raise ValueError('Request prefix reused')
                        prefixes.add(identifier)
                    response = audit.get('response') or {}
                    usage = audit.get('usage') or response.get('usage') or response.get('response_metadata', {}).get('token_usage', {})
                    usage = usage if isinstance(usage, dict) else {}
                    info['token_usage'].append({'call_id': audit['call_id'], 'input_tokens': usage.get('input_tokens', usage.get('prompt_tokens')),
                        'output_tokens': usage.get('output_tokens', usage.get('completion_tokens')),
                        'cached_tokens': usage.get('cached_tokens', usage.get('prompt_tokens_details', {}).get('cached_tokens')),
                        'cache_creation_input_tokens': usage.get('cache_creation_input_tokens')})
                    info['linked_attempts'] += 1
                except (ValueError, OSError, KeyError) as exc:
                    info['failures'].append({'key': key, 'attempt': row['attempt'], 'kind': 'audit_integrity', 'reason': str(exc)})
            for key in sorted(expected[suite]):
                row = latest.get(key)
                if row is None:
                    info['missing'].append(key)
                    continue
                try:
                    if row['validation_success'].lower() != 'true':
                        raise ValueError(row['error'] or 'Measurement rejected')
                    with dataset_context(directory):
                        revalidate_row(row, definition)
                    if suite == 'benchmark':
                        DecisionOutput.model_validate({
                            'requires_web_probability': float(row['requires_web_probability']),
                            'is_safe_probability': float(row['is_safe_probability']),
                            'route_probabilities': {name.removeprefix('route_').removesuffix('_probability'): float(row[name]) for name in ROUTE_COLUMNS},
                            'freshness_probabilities': {str(i): float(row[f'freshness_{i}_probability']) for i in range(6)}})
                    else:
                        HardCaseOutput.model_validate({name: float(row[name]) for name in HardCaseOutput.model_fields})
                    info['passed'] += 1
                except (ValueError, OSError, KeyError) as exc:
                    info['failures'].append({'key': key, 'kind': row.get('failure_kind') or 'integrity', 'reason': str(exc)})
        info['measurement_keys'] = len(set(latest) & expected[suite])
        manifest['valid_measurements'] += info['passed']
        manifest['suites'][suite] = info
    manifest['complete'] = manifest['valid_measurements'] == target and all(
        info['linked_attempts'] == info['attempts'] and not info['failures'] for info in manifest['suites'].values())
    if prefix and not (run_plan or {}).get('kind', '').startswith('azure_prefix_') and (sum(info['attempts'] for info in manifest['suites'].values()) > 8
                   or any(info['attempts'] != info['expected_keys'] for info in manifest['suites'].values())):
        manifest['complete'] = False
    destination = Path(output_directory) if output_directory else root
    destination.mkdir(parents=True, exist_ok=True)
    atomic_write(destination / 'validation.json', lambda f: json.dump(manifest, f, indent=2))
    return manifest


def update_validation(root, output_directory=None):
    root = Path(root).resolve()
    if output_directory is not None:
        return validate_saved(root, output_directory)
    ensure_writable(root)
    with output_lock(root), ExitStack() as stack:
        for suite in ['benchmark', 'hard_case']:
            path = root / suite / '.benchmark.lock'
            if path.exists():
                handle = stack.enter_context(path.open('r'))
                try:
                    fcntl.flock(handle, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    plan_path = root / 'run_plan.json'
                    plan = json.loads(plan_path.read_text()) if plan_path.exists() else None
                    if plan and plan.get('version') == 2:
                        from jev_bench.storage.plan import expected_keys
                        expected = sum(len(keys) for keys in expected_keys(plan).values())
                    else:
                        expected = 11 * plan['repetitions'] if plan else 55
                    manifest = {'policy': POLICY, 'updated_at_utc': now(), 'complete': False,
                                'expected_measurements': expected,
                                'status': 'suite_writer_active'}
                    atomic_write(root / 'validation.json', lambda f: json.dump(manifest, f, indent=2))
                    return manifest
        return _build_validation(root, output_directory)


def validate_saved(root, output_directory):
    """Inspect immutable datasets using existing locks; write only to report output."""
    from jev_bench.run_evaluations.reporting import saved_snapshot
    root = Path(root).resolve()
    output_directory = Path(output_directory).resolve()
    if output_directory.is_relative_to(root) or root.is_relative_to(output_directory):
        raise ValueError('Validation output must be separate from its source')
    with ExitStack() as stack:
        stack.enter_context(saved_snapshot(root))
        for suite in ('benchmark', 'hard_case'):
            stack.enter_context(saved_snapshot(root / suite))
        return _build_validation(root, output_directory)


@contextmanager
def validation_on_exit(root):
    try:
        yield
    finally:
        update_validation(root)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent / 'results')
    print(json.dumps(update_validation(parser.parse_args().output_dir), indent=2))
