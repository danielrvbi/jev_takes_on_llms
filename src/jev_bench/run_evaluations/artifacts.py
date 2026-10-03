"""Regenerate reports, summaries and plots outside immutable source datasets."""
import csv
from importlib import import_module
import json
from pathlib import Path

from jev_bench.runtime.execution import POLICY, supported_policy
from jev_bench.run_evaluations.comparison import load_source
from jev_bench.run_evaluations.reporting import (saved_snapshot, build_report,
                                                render_hard_case_report)
from jev_bench.storage.paths import ensure_readable
from jev_bench.suites import get_suite


def render(root, output, *, suite='all', models=None, case_ids=None, include_raw=False):
    root, output = Path(root).resolve(), Path(output).resolve()
    ensure_readable(root)
    if output.is_relative_to(root) or root.is_relative_to(output):
        raise ValueError('Report output must be separate from source datasets')
    directories = [(root / name, name) for name in ('benchmark', 'hard_case')
                   if (root / name / 'raw.csv').exists() and suite in ('all', name)]
    if (root / 'raw.csv').exists():
        with (root / 'raw.csv').open(newline='') as stream:
            columns = csv.DictReader(stream).fieldnames
        name = 'benchmark' if 'case_id' in columns else 'hard_case'
        directories = [(root, name)] if suite in ('all', name) else []
    if not directories:
        raise ValueError('Selected source/suite has no saved measurements')
    reports = []
    for directory, name in directories:
        spec = get_suite(name)
        with saved_snapshot(directory):
            metadata_path = directory / 'metadata.json'
            metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
            with (directory / 'raw.csv').open(newline='') as stream:
                reader = csv.DictReader(stream)
                columns, rows = reader.fieldnames, list(reader)
            definition = metadata.get('experiment', {})
            audited = (columns == spec.columns and supported_policy(definition.get('execution_policy'))
                       and metadata.get('kind') != 'reconciled_evaluation')
            if audited:
                source = load_source(directory, name)
                frame, history = source['frame'], source['history']
            else:
                frame = spec.metrics.results_frame(rows)
                history_path = directory / 'attempt_history.csv'
                if history_path.exists():
                    with history_path.open(newline='') as stream:
                        history = spec.metrics.results_frame(list(csv.DictReader(stream)))
                else:
                    history = frame.copy()
            if models:
                frame = frame[frame.model.isin(models)]
                history = history[history.model.isin(models)]
            if case_ids and name == 'benchmark':
                frame = frame[frame.case_id.isin(case_ids)]
                history = history[history.case_id.isin(case_ids)]
            if frame.empty:
                raise ValueError('No saved measurements match selected filters')
            summary = spec.metrics.summarize(frame, history)
            target = output / name
            target.mkdir(parents=True, exist_ok=True)
            summary.to_csv(target / 'summary.csv', index=False)
            import_module(f'jev_bench.run_evaluations.plots.{name}').plot_results(frame, summary, target)
            report = (build_report(target, frame, history, metadata, include_raw,
                                   plot_directory=target / 'plots') if name == 'benchmark' else
                      render_hard_case_report(target, frame, history, metadata, include_raw))
            if not audited:
                report = ('> Historical or evaluation-only source: original success flags are preserved. '
                          'This report does not establish current execution validity; consult original provenance.\n\n' + report)
            report = f'Source dataset: {directory}\n\n' + report
            if definition.get('prefix_strategy'):
                report = ('> Altered-prompt prefix experiment; kept separate from original-prompt results. '
                          'Azure latency is hosted wall time, not local cold-start latency.\n\n' + report)
            if name == 'hard_case':
                report += '\n## Plots\n\n' + '\n'.join(
                    f'- [{p.name}]({p})' for p in sorted((target / 'plots').glob('*.png'))) + '\n'
            (target / 'evaluation.md').write_text(report, encoding='utf-8')
            reports.append(report)
    (output / 'evaluation.md').write_text('\n'.join(reports), encoding='utf-8')
    return output / 'evaluation.md'
