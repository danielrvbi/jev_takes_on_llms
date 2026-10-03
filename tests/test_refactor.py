from jev_bench.storage.io import output_lock
import csv
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from rich.console import Console
from jev_bench.providers.base import Request, InvocationContext
from jev_bench.run.benchmark import run_benchmark
from jev_bench.run_evaluations.validation import update_validation, validate_saved
from jev_bench.storage.io import output_lock
from jev_bench.storage.migrate import migrate, inventory
from jev_bench.storage.paths import ensure_writable, resolve_evidence, dataset_context
from jev_bench.storage.plan import record_selection, expected_keys
from jev_bench.suites.benchmark.cases import CASES
from jev_bench.runtime.execution import POLICY, save_audit, new_audit, revalidate_audit
from tests.fixtures.benchmark import result, measured_row


class RefactorTests(unittest.TestCase):
    def test_modules_and_resources_work_outside_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            for module, arguments in [('jev_bench.run', ['--help']),
                                      ('jev_bench.run', ['benchmark', '--help']),
                                      ('jev_bench.run', ['hard-case', '--help']),
                                      ('jev_bench.run', ['jev-api', '--help']),
                                      ('jev_bench.run', ['prefix-pilot', '--help']),
                                      ('jev_bench.run_evaluations', ['--help'])]:
                process = subprocess.run([sys.executable, '-m', module, *arguments],
                    cwd=directory, capture_output=True, text=True)
                self.assertEqual(process.returncode, 0, process.stderr)
            process = subprocess.run([sys.executable, '-c',
                'from importlib.resources import files; '
                'from jev_bench.suites.hard_case.loader import load_claim_packet; '
                'assert load_claim_packet().profile == "compact"; '
                'assert files("jev_bench.runtime").joinpath("no-cache.patch").is_file()'],
                cwd=directory, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)

    def test_one_request_boundary_and_partial_target_validation(self):
        class Backend:
            supports_requests = True
            calls = []
            def invoke(self, request, context):
                self.calls.append((request, context))
                return result()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backend = Backend()
            definition = {'execution_policy': POLICY, 'model_configuration': {'fixture': {}}}
            with patch('jev_bench.run.benchmark.report'), patch('jev_bench.run.benchmark.measured_row', side_effect=measured_row):
                self.assertEqual(run_benchmark(['fixture'], [CASES[0]], 2, 0, root,
                    provider_factory=Mock(return_value=backend), definition=definition,
                    console=Console(file=io.StringIO())), 0)
            self.assertEqual(len(backend.calls), 2)
            self.assertIsInstance(backend.calls[0][0], Request)
            self.assertIsInstance(backend.calls[0][1], InvocationContext)
            manifest = update_validation(root)
            self.assertTrue(manifest['complete'])
            self.assertEqual(manifest['expected_measurements'], 2)
            before = inventory(root)
            output = root.parent / (root.name + '-reports')
            try:
                self.assertTrue(validate_saved(root, output)['complete'])
                self.assertEqual(inventory(root), before)
            finally:
                shutil.rmtree(output, ignore_errors=True)

    def test_plan_extensions_preserve_exact_case_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = {'model_configuration': {'fixture': {}}}
            record_selection(root, 'benchmark', ['fixture'], [1], 30, 0, definition)
            record_selection(root, 'benchmark', ['fixture'], [2], 5, 0, definition)
            plan = json.loads((root / 'run_plan.json').read_text())
            self.assertEqual(len(plan['revisions']), 2)
            self.assertEqual(len(expected_keys(plan)['benchmark']), 35)

    def test_new_evidence_links_are_relative_and_detect_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_selection(root, 'benchmark', ['fixture'], [1], 1, 0, {})
            suite = root / 'benchmark'
            audit = new_audit('fixture', {'state': 'input'}, suite / 'execution_audit', 'not_executed')
            linked = save_audit(audit)
            self.assertFalse(Path(linked['audit_path']).is_absolute())
            self.assertEqual(revalidate_audit(linked, require_verified=False, directory=suite)['input'], {'state': 'input'})
            (suite / linked['audit_path']).write_text('{}')
            with self.assertRaisesRegex(ValueError, 'fingerprint mismatch'):
                revalidate_audit(linked, require_verified=False, directory=suite)
            with dataset_context(suite), self.assertRaisesRegex(ValueError, 'escapes'):
                resolve_evidence('../other.json')

    def test_offline_report_generates_artifacts_without_changing_sources(self):
        from dataclasses import asdict
        from jev_bench.run.benchmark import ResultStore
        from jev_bench.run_evaluations.artifacts import render
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'experiment'
            definition = {'execution_policy': POLICY, 'cases': [asdict(CASES[0])],
                          'model_configuration': {'fixture': {}}}
            store = ResultStore(source / 'benchmark', definition)
            store.record(measured_row('fixture', CASES[0], 1, result(), 10))
            before = inventory(source)
            with patch('jev_bench.providers.registry.create_provider', side_effect=AssertionError('No inference')):
                report = render(source, root / 'reports')
            self.assertTrue(report.exists())
            self.assertTrue((root / 'reports/benchmark/summary.csv').exists())
            self.assertTrue(list((root / 'reports/benchmark/plots').glob('*.png')))
            self.assertEqual(inventory(source), before)

    def test_comparison_separates_overlapping_original_and_prefix_keys(self):
        from dataclasses import asdict
        from jev_bench.run.control import PREFIX_STRATEGY
        from jev_bench.run_evaluations.comparison import build_combined_report
        from jev_bench.suites.benchmark.metrics import results_frame
        with tempfile.TemporaryDirectory() as directory:
            roots = [Path(directory) / name for name in ('original', 'pilot')]
            sources = []
            for index, root in enumerate(roots):
                suite = root / 'benchmark'
                suite.mkdir(parents=True)
                (suite / 'raw.csv').touch()
                row = measured_row('mistral-small-latest', CASES[0], 1, result(), 10)
                row['attempt'] = 1
                frame = results_frame([row])
                definition = {'schema': {}, 'prompts': {}, 'cases': [asdict(CASES[0])]}
                if index:
                    definition['prefix_strategy'] = PREFIX_STRATEGY
                sources.append({'directory': suite, 'definition': definition, 'frame': frame,
                                'history': frame.copy(), 'metadata': {}, 'sha256': {}})
            with patch('jev_bench.run_evaluations.comparison.load_source', side_effect=sources):
                report, _ = build_combined_report(roots)
            self.assertIn('benchmark / original', report)
            self.assertIn('benchmark / prefix_pilot', report)

    def test_quarantine_and_backups_are_excluded_from_discovery(self):
        from jev_bench.run_evaluations.reporting import find_results
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for sub in ('quarantine', '.migration_backup', '.migration_staging'):
                folder = root / sub / 'benchmark'
                folder.mkdir(parents=True)
                (folder / 'raw.csv').touch()
                with self.assertRaisesRegex(ValueError, 'Quarantined'):
                    find_results(folder)
            with self.assertRaisesRegex(ValueError, 'No raw.csv'):
                find_results(root)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / 'results/jev_api_10'
        self.source.mkdir(parents=True)
        (self.source / 'metadata.json').write_bytes(b'{"preserve": "\\u00e9"}\r\n')

    def test_dry_run_preservation_relocation_and_archive_guard(self):
        before = inventory(self.root)
        plan = migrate(self.root)
        self.assertEqual(inventory(self.root), before)
        self.assertEqual(plan['status'], 'planned')
        manifest = migrate(self.root, apply=True)
        dest = self.root / 'results/experiments/jev_api_30'
        self.assertEqual(inventory(dest), {'metadata.json': before['results/jev_api_10/metadata.json']})
        self.assertTrue((self.root / 'results/.migration_backup/original/results/jev_api_10/metadata.json').exists())
        with dataset_context(dest):
            self.assertEqual(resolve_evidence(self.source / 'metadata.json'), dest / 'metadata.json')
        with self.assertRaisesRegex(ValueError, 'Archived'):
            ensure_writable(dest / 'benchmark')
        self.assertEqual(migrate(self.root, apply=True)['status'], 'complete')
        moved = self.root.parent / (self.root.name + '-moved')
        try:
            shutil.copytree(self.root, moved)
            with dataset_context(moved / 'results/experiments/jev_api_30'):
                self.assertEqual(resolve_evidence(self.source / 'metadata.json'), moved / 'results/experiments/jev_api_30/metadata.json')
        finally:
            shutil.rmtree(moved, ignore_errors=True)

    def test_conflicts_and_active_writer_leave_sources_untouched(self):
        dest = self.root / 'results/experiments/jev_api_30'
        dest.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'Conflicting'):
            migrate(self.root, apply=True)
        self.assertTrue(self.source.exists())
        shutil.rmtree(dest)
        with output_lock(self.source), self.assertRaisesRegex(ValueError, 'writer is active'):
            migrate(self.root, apply=True)
        self.assertTrue(self.source.exists())

    def test_completed_migration_checks_published_data_without_local_only_files(self):
        (self.source / '.benchmark.lock').touch()
        (self.root / 'results/.benchmark.lock').touch()
        quarantine = self.root / 'results_contaminated_dont_use'
        quarantine.mkdir()
        (quarantine / 'excluded.csv').write_text('local-only measurements\n')
        migrate(self.root, apply=True)
        destination = self.root / 'results/experiments/jev_api_30'
        (destination / '.benchmark.lock').unlink()
        (self.root / 'results/experiments/baseline/.benchmark.lock').unlink()
        shutil.rmtree(self.root / 'results/quarantine')
        before = inventory(self.root)
        self.assertEqual(migrate(self.root)['status'], 'complete')
        self.assertEqual(migrate(self.root, apply=True)['status'], 'complete')
        self.assertEqual(inventory(self.root), before)
        (destination / 'metadata.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Migrated destination changed'):
            migrate(self.root)

    def test_interrupted_publication_resumes_from_original_backup(self):
        rename = Path.rename
        def interrupt(path, target):
            if '.migration_staging' in path.parts:
                raise KeyboardInterrupt()
            return rename(path, target)
        with patch.object(Path, 'rename', interrupt), self.assertRaises(KeyboardInterrupt):
            migrate(self.root, apply=True)
        self.assertEqual(migrate(self.root, apply=True)['status'], 'complete')
        self.assertTrue((self.root / 'results/experiments/jev_api_30/metadata.json').exists())

    def test_archived_hosted_launchers_refuse_before_creating_providers(self):
        migrate(self.root, apply=True)
        from jev_bench.run.jev_api import run_jev
        from jev_bench.run.prefix_pilot import run_pilot
        dest = self.root / 'results/experiments/jev_api_30'
        before = inventory(dest)
        with patch('jev_bench.run.jev_api.run_benchmark') as launch, self.assertRaisesRegex(ValueError, 'Archived'):
            run_jev(dest)
        launch.assert_not_called()
        with self.assertRaisesRegex(ValueError, 'Archived'):
            run_pilot(dest, direct=True)
        self.assertEqual(inventory(dest), before)
