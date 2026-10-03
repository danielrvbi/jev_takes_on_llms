"""Read-only comparison of original sources, with independent audit provenance."""
from contextlib import ExitStack
import csv
import json
import math
from pathlib import Path

import pandas as pd

from execution import file_hash, fingerprint, revalidate_audit, revalidate_row

SOURCE_FILES = ('raw.csv', 'attempt_history.csv', 'metadata.json')
DEFAULT_ROOTS = ('results/jev_30', 'results/gemma_30', 'results/jev_api_30')


def load_source(directory, suite):
    from benchmark.metrics import RAW_COLUMNS as benchmark_columns, results_frame as benchmark_frame
    from hard_case.metrics import RAW_COLUMNS as hard_columns, results_frame as hard_frame
    columns = benchmark_columns if suite == 'benchmark' else hard_columns
    frame_factory = benchmark_frame if suite == 'benchmark' else hard_frame
    metadata = json.loads((directory / 'metadata.json').read_text())
    definition = metadata['experiment']
    if metadata.get('kind') == 'reconciled_evaluation' or fingerprint(definition) != metadata.get('fingerprint'):
        raise ValueError('Select an original source with valid metadata: ' + str(directory))
    loaded = {}
    latest_history = {}
    cases = {c['case_id']: c['message'] for c in definition.get('cases', [])}
    for filename in ('attempt_history.csv', 'raw.csv'):
        with (directory / filename).open(newline='', encoding='utf-8') as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != columns:
                raise ValueError('Unexpected CSV columns: ' + str(directory / filename))
            rows = list(reader)
        seen = set()
        for row in rows:
            if None in row or any(v is None for v in row.values()):
                raise ValueError('Incomplete source row')
            key = (row['model'], int(row['repetition']))
            if suite == 'benchmark':
                key = (row['model'], int(row['case_id']), int(row['repetition']))
                if cases.get(key[1]) != row['message']:
                    raise ValueError('Source case message differs from metadata')
            if row['model'] not in definition.get('model_configuration', {}):
                raise ValueError('Source model absent from metadata')
            if int(row['repetition']) < 1 or int(row['attempt']) < 1:
                raise ValueError('Invalid source repetition/attempt')
            if row['validation_success'].lower() not in ('true', 'false'):
                raise ValueError('Invalid source validation status')
            successful = row['validation_success'].lower() == 'true'
            if successful == bool(row['error']):
                raise ValueError('Source error contradicts validation status')
            if filename == 'raw.csv':
                if key in seen:
                    raise ValueError('Duplicate source measurement key')
                seen.add(key)
            else:
                previous = latest_history.get(key)
                if int(row['attempt']) != (int(previous['attempt']) + 1 if previous else 1):
                    raise ValueError('Invalid source attempt sequence')
                if previous and previous['validation_success'].lower() == 'true':
                    raise ValueError('Successful source repetition was retried')
                latest_history[key] = row
            raw = json.loads(row['raw_response_json'])
            audit = revalidate_audit(raw['execution_audit'], raw, require_verified=False)
            if row['call_id'] != audit['call_id'] or row['audit_path'] != audit['audit_path']:
                raise ValueError('Source row differs from audit linkage')
            for field in ('request_sha256', 'runtime_sha256', 'model_sha256'):
                if (row[field] or '') != (audit.get(field) or ''):
                    raise ValueError('Source row differs from audit fingerprint')
            configuration = definition['model_configuration'][row['model']]
            if audit['model'] != configuration.get('inference_model', row['model']):
                raise ValueError('Audited source model differs from metadata')
            payload = audit['input']
            state = payload.get('state') if 'state' in payload else payload.get('messages', [[None, None]])[-1][1]
            if suite == 'benchmark' and state != row['message']:
                raise ValueError('Audited source input differs from case')
            if suite == 'hard_case':
                import hashlib
                if not isinstance(state, str) or hashlib.sha256(state.encode()).hexdigest() != definition['case_input']['sha256']:
                    raise ValueError('Audited source packet differs from metadata')
            if successful:
                revalidate_row(row, definition)
                if suite == 'benchmark':
                    from benchmark.schemas import DecisionOutput, ROUTES
                    DecisionOutput.model_validate({
                        'requires_web_probability': float(row['requires_web_probability']),
                        'is_safe_probability': float(row['is_safe_probability']),
                        'route_probabilities': {r: float(row[f'route_{r}_probability']) for r in ROUTES},
                        'freshness_probabilities': {str(i): float(row[f'freshness_{i}_probability']) for i in range(6)}})
                else:
                    from hard_case.schemas import HardCaseOutput, PROBABILITY_FIELDS
                    HardCaseOutput.model_validate({f: float(row[f]) for f in PROBABILITY_FIELDS})
            elif audit['provider'] == 'typesafe':
                # Rejected Jev rows still bind every recoverable probability to the
                # raw HTTP response; they never enter accepted aggregates.
                if suite == 'benchmark':
                    from benchmark.providers.ollama_systemone import map_response
                    from benchmark.metrics import flatten_values
                else:
                    from hard_case.providers.ollama_systemone import map_response
                    from hard_case.metrics import flatten_values
                mapped = map_response({**raw, 'usage': {}})
                values = flatten_values(mapped.values)
                for field, value in values.items():
                    if value is not None and math.isfinite(value) and (not row[field] or float(row[field]) != value):
                        raise ValueError('Rejected Jev probabilities differ from audited response')
        loaded[filename] = rows
    if len(loaded['raw.csv']) != len(latest_history) or any(
        row != latest_history.get((row['model'], int(row['case_id']), int(row['repetition']))
                                  if suite == 'benchmark' else (row['model'], int(row['repetition'])))
        for row in loaded['raw.csv']):
        raise ValueError('Source raw.csv and history disagree')
    return {'directory': directory, 'metadata': metadata, 'definition': definition,
            'frame': frame_factory(loaded['raw.csv']), 'history': frame_factory(loaded['attempt_history.csv']),
            'sha256': {name: file_hash(directory / name) for name in SOURCE_FILES}}


def semantic_identity(definition, suite):
    identity = {'schema': definition['schema'], 'prompts': definition['prompts']}
    if suite == 'benchmark':
        identity['cases'] = definition['cases']
    else:
        identity['case_input'] = definition['case_input']
        identity['input_format'] = definition.get('input_format')
    return identity


def build_combined_report(roots, suite='all', models=None, case_ids=None, include_raw=False,
                          output_directory=None):
    from show_evaluations import saved_snapshot, build_report, render_hard_case_report, table, fenced_json
    from benchmark.metrics import summarize as benchmark_summary
    from hard_case.metrics import summarize as hard_summary
    roots = [Path(root).resolve() for root in roots]
    if len(set(roots)) != len(roots):
        raise ValueError('Duplicate result sources')
    if any('contaminated' in part.lower() for root in roots for part in root.parts):
        raise ValueError('Quarantined results cannot be imported')
    directories = [(root / name, name) for root in roots for name in ('benchmark', 'hard_case')
                   if (suite == 'all' or suite == name) and (root / name / 'raw.csv').exists()]
    if not directories:
        raise ValueError('No saved measurements in selected sources')
    lines = ['# Combined benchmark comparison', '',
             'Sources remain independent. Probability aggregates exclude rejected rows; '
             'latency and usage retain the existing treatment of latest failed attempts.', '']
    groups = {}
    identities = {}
    keys = set()
    call_ids = set()
    with ExitStack() as stack:
        for directory, name in sorted(directories):
            stack.enter_context(saved_snapshot(directory))
        for directory, name in directories:
            source = load_source(directory, name)
            identity = semantic_identity(source['definition'], name)
            if name in identities and identity != identities[name]:
                raise ValueError('Source semantic prompts, schema, or inputs differ: ' + name)
            identities[name] = identity
            variant = 'prefix_pilot' if source['definition'].get('prefix_strategy') else 'original'
            variant_id = fingerprint(source['definition'].get('prefix_strategy'))
            frame, history = source['frame'], source['history']
            if models:
                frame = frame[frame.model.isin(models)]
                history = history[history.model.isin(models)]
            if case_ids and name == 'benchmark':
                frame = frame[frame.case_id.isin(case_ids)]
                history = history[history.case_id.isin(case_ids)]
            for row in frame.itertuples():
                key = (name, row.model, row.repetition, getattr(row, 'case_id', None))
                if key in keys:
                    raise ValueError('Duplicate measurement keys across sources; select one authoritative source')
                keys.add(key)
            for row in history.itertuples():
                if row.call_id in call_ids:
                    raise ValueError('Audit reused across source measurements')
                call_ids.add(row.call_id)
            if frame.empty:
                continue
            source.update(frame=frame, history=history)
            groups.setdefault((name, variant, variant_id), []).append(source)
        if not groups:
            raise ValueError('No measurements match selected filters')
        for (name, variant, _), sources in groups.items():
            frame = pd.concat([s['frame'] for s in sources], ignore_index=True)
            history = pd.concat([s['history'] for s in sources], ignore_index=True)
            summary = (benchmark_summary if name == 'benchmark' else hard_summary)(frame, history)
            lines.extend([f'## {name} / {variant}', ''])
            if variant != 'original':
                lines.extend(['Altered-prompt Mistral pilot; shown separately and never pooled with original-prompt measurements.', ''])
            table(lines, ['Source', 'Models', 'Latest rows', 'Historical attempts'],
                  [[str(s['directory']), ', '.join(s['frame'].model.unique()), len(s['frame']), len(s['history'])] for s in sources])
            provenance = {'kind': 'combined_report', 'sources': [
                {'path': str(s['directory']), 'file_sha256': s['sha256'], 'metadata': s['metadata']} for s in sources]}
            # All summary fields are rendered for every model, including tokens,
            # failure counts, standard deviations, ranges, and unavailable values.
            lines.extend(['### Complete suite summary', ''])
            table(lines, list(summary.columns), summary.fillna('NA').itertuples(index=False, name=None))
            target = None
            if output_directory is not None:
                target = Path(output_directory) / 'comparison' / name / variant
                target.mkdir(parents=True, exist_ok=True)
                plotter = __import__('benchmark.plots' if name == 'benchmark' else 'hard_case.plots', fromlist=['plot_results'])
                plotter.plot_results(frame, summary, target)
            if name == 'benchmark':
                lines.append(build_report(target or sources[0]['directory'], frame, history, provenance,
                                          include_raw, plot_directory=target / 'plots' if target else None))
            else:
                lines.append(render_hard_case_report(sources[0]['directory'], frame, history,
                                                     provenance, include_raw))
            if target:
                lines.extend(['### Combined plots', ''])
                lines.extend(f'- [{p.name}]({p.resolve()})' for p in sorted((target / 'plots').glob('*.png')))
            fenced_json(lines, provenance)
        for directory, name in directories:
            # The locks cover the complete read and report operation.
            source = next((s for items in groups.values() for s in items if s['directory'] == directory), None)
            if source and any(file_hash(directory / filename) != digest for filename, digest in source['sha256'].items()):
                raise ValueError('Source changed during comparison')
    return '\n'.join(lines) + '\n', [d for d, _ in directories]
