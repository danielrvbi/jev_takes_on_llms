import copy
import csv
from decimal import Decimal
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from rich.console import Console

from benchmark.cases import CASES
from benchmark.prompts import SYSTEM_PROMPT, systemone_questions
from benchmark.providers.jev import JevProvider
from benchmark.runner import ResultStore, measured_row, run_benchmark
from benchmark.schemas import DecisionOutput
from combined_reporting import build_combined_report
from execution import (POLICY, JEV_CACHE_EXCEPTION, file_hash, fingerprint,
                       revalidate_audit, revalidate_row, enforce_result)
from hard_case.loader import load_claim_packet
from hard_case.providers.jev import JevProvider as HardJevProvider
from hard_case.runner import ResultStore as HardStore, measured_row as hard_row, run_hard_case_benchmark
from hard_case.schemas import HardCaseOutput, PROBABILITY_FIELDS
from jev_execution import (CACHE_REJECTION, JEV_MODEL, INPUT_PRICE, MAX_REQUEST_CHARGE,
                           JevBudget, typed_questions)
from run_control import RunStopped
from run_jev_api import run_jev
from runtime.testing import audited_result
from tests.test_benchmark import result as successful_result, values
from validation import update_validation


def response_body(hard=False, usage=True):
    answers = {name: {'type': 'noul', 'noul': (index + 1) / 10}
               for index, name in enumerate(PROBABILITY_FIELDS)} if hard else {
        'requires_web': {'type': 'noul', 'noul': .5},
        'is_safe': {'type': 'noul', 'noul': .9},
        'route': {'type': 'choice', 'choice': 'refuse', 'confidence': .1,
                  'probabilities': values()['route_probabilities']},
        'freshness': {'type': 'score', 'score': 5, 'confidence': .1,
                     'legend': {str(i): str(i) for i in range(6)},
                     'probabilities': values()['freshness_probabilities']}}
    body = {'model': JEV_MODEL, 'answers': answers, 'extra_provider_metadata': {'untouched': True}}
    if usage:
        body['usage'] = {'input_tokens': 281, 'output_tokens': 20}
    return body


def definition(suite='benchmark'):
    if suite == 'benchmark':
        return {'execution_policy': POLICY, 'cases': [{'case_id': case.case_id, 'message': case.message} for case in CASES],
                'schema': DecisionOutput.model_json_schema(),
                'prompts': {'llm_system': SYSTEM_PROMPT, 'systemone_questions': systemone_questions()},
                'model_configuration': {JEV_MODEL: {}}}
    from hard_case.prompts import SYSTEM_PROMPT as hard_prompt, systemone_questions as hard_questions
    packet = load_claim_packet()
    return {'execution_policy': POLICY, 'case_input': packet.metadata(),
            'schema': HardCaseOutput.model_json_schema(),
            'prompts': {'llm_system': hard_prompt, 'systemone_questions': hard_questions()},
            'model_configuration': {JEV_MODEL: {}}}


class TrackingTransport(httpx2.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)
        self.closed = False

    def close(self):
        self.closed = True
        super().close()


class JevFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.console = Console(file=io.StringIO())
        patcher = patch.dict('os.environ', {'TYPESAFE_API': 'fixture-secret-key'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def provider(self, hard=False, body=None, status=200, fail=None):
        self.requests = []
        self.transports = []
        def handler(request):
            self.requests.append(json.loads(request.content))
            self.assertEqual(str(request.url), 'https://api.typesafe.ai/v1/systemone')
            if fail:
                raise fail
            return httpx2.Response(status, json=response_body(hard) if body is None else body,
                                   headers={'x-typesafe-request-id': 'fixture-request-id'})
        def factory():
            transport = TrackingTransport(handler)
            self.transports.append(transport)
            return transport
        suite = 'hard_case' if hard else 'benchmark'
        cls = HardJevProvider if hard else JevProvider
        return cls(JEV_MODEL, audit_directory=self.root / suite / 'execution_audit', transport_factory=factory)


class JevTests(JevFixture):
    def test_complete_payloads_values_usage_and_cleanup_in_both_suites(self):
        for hard in (False, True):
            with self.subTest(hard=hard), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                provider = self.provider(hard)
                state = load_claim_packet().text if hard else CASES[0].message
                from hard_case.prompts import systemone_questions as hard_questions
                questions = hard_questions() if hard else systemone_questions()
                typed = typed_questions(questions)
                self.assertEqual({k: q.model_dump(mode='json', exclude_none=True) for k, q in typed.items()}, questions)
                classifiers = []
                from langchain_typesafe import TypeSafeClassifier
                def capture(**kwargs):
                    classifier = TypeSafeClassifier(**kwargs)
                    classifiers.append(classifier)
                    return classifier
                with patch('jev_execution.TypeSafeClassifier', side_effect=capture):
                    measured = provider.invoke(state)
                self.assertEqual(self.requests, [{'model': JEV_MODEL, 'state': state, 'questions': questions}])
                expected = {f: (i + 1) / 10 for i, f in enumerate(PROBABILITY_FIELDS)} if hard else values()
                self.assertEqual(measured.values, expected)
                self.assertIsNotNone(measured.output)
                self.assertEqual((measured.input_tokens, measured.output_tokens), (281, 20))
                self.assertTrue(measured.error.startswith('Cache verification failed:'))
                raw = measured.raw_response
                self.assertEqual(raw['extra_provider_metadata'], {'untouched': True})
                audit = revalidate_audit(raw['execution_audit'], raw, require_verified=False)
                self.assertFalse(audit['verified'])
                self.assertEqual(audit['request_id'], 'fixture-request-id')
                self.assertTrue(all(c.client.is_closed and c.async_client.is_closed for c in classifiers))
                self.assertTrue(all(t.closed for t in self.transports))
                evidence = Path(audit['audit_path']).read_text()
                self.assertNotIn('fixture-secret-key', evidence)
                self.assertNotIn('Authorization', evidence)
                with self.assertRaisesRegex(ValueError, 'Cache verification failed'):
                    revalidate_audit(raw['execution_audit'])
                with self.assertRaises(RunStopped):
                    provider.invoke(state)
                self.assertEqual(len(self.requests), 1)

    def test_rejected_rows_keep_same_columns_and_no_derived_decisions(self):
        from benchmark.metrics import RAW_COLUMNS, results_frame, summarize
        from hard_case.metrics import RAW_COLUMNS as hard_columns, results_frame as hard_frame, summarize as hard_summary
        for hard in (False, True):
            with self.subTest(hard=hard), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                response = self.provider(hard).invoke(load_claim_packet().text if hard else CASES[0].message)
                row = hard_row(JEV_MODEL, 1, response, 20) if hard else measured_row(JEV_MODEL, CASES[0], 1, response, 20)
                store = HardStore(self.root / 'hard_case', definition('hard_case')) if hard else ResultStore(self.root / 'benchmark', definition())
                store.record(row)
                columns = hard_columns if hard else RAW_COLUMNS
                with (store.directory / 'raw.csv').open() as handle:
                    self.assertEqual(next(csv.reader(handle)), columns)
                row = next(iter(store.latest.values()))
                self.assertFalse(row['validation_success'])
                self.assertFalse(row['cache_verified'])
                self.assertEqual(row['failure_kind'], 'cache_verification')
                self.assertEqual(row['input_tokens'], 281)
                if not hard:
                    self.assertIsNone(row['derived_route'])
                    self.assertEqual(row['route_refuse_probability'], .1)
                summary = (hard_summary(hard_frame([row])) if hard else summarize(results_frame([row]))).iloc[0]
                self.assertEqual(summary.successful_repetitions, 0)
                self.assertEqual(summary.cache_verification_failures, 1)
                field = PROBABILITY_FIELDS[0] if hard else 'requires_web_probability'
                self.assertTrue(math.isnan(summary[field + '_mean']))

    def test_errors_redirect_missing_usage_and_malformed_output_do_not_retry(self):
        for status, body in [(401, {'error': 'fixture-secret-key'}), (429, {'error': 'limited'}),
                             (529, {'error': 'overloaded'}), (302, {}),
                             (200, response_body(usage=False)), (200, {'answers': {}}),
                             (200, {'model': JEV_MODEL, 'usage': ['bad'], 'answers': {'urgent': {'noul': True}}})]:
            with self.subTest(status=status, body=body), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                measured = self.provider(body=body, status=status).invoke(CASES[0].message)
                self.assertEqual(len(self.requests), 1)
                self.assertTrue(measured.error)
                self.assertNotIn('fixture-secret-key', json.dumps(measured.raw_response))
                ledger = json.loads((self.root / 'jev_budget.json').read_text())
                if 'usage' not in body or not isinstance(body['usage'], dict):
                    self.assertIsNone(measured.input_tokens)
                    self.assertEqual(Decimal(ledger['reservations'][0]['charge_usd']), MAX_REQUEST_CHARGE)

    def test_timeout_and_interrupt_retain_reservation_and_close_clients(self):
        for error in (httpx2.ReadTimeout('fixture-secret-key'), KeyboardInterrupt()):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                provider = self.provider(fail=error)
                if isinstance(error, KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        provider.invoke(CASES[0].message)
                else:
                    provider.invoke(CASES[0].message)
                self.assertTrue(self.transports[0].closed)
                ledger = json.loads((self.root / 'jev_budget.json').read_text())
                self.assertEqual(Decimal(ledger['reservations'][0]['charge_usd']), MAX_REQUEST_CHARGE)
                self.assertTrue(ledger['blocked_reason'])

    def test_missing_key_and_oversized_payload_send_nothing(self):
        with patch.dict('os.environ', {'TYPESAFE_API': ''}), self.assertRaises(ValueError):
            self.provider()
        provider = self.provider()
        with self.assertRaises(ValueError):
            provider.invoke('x' * (61 * 1024))
        self.assertEqual(self.requests, [])

    def test_audit_tampering_and_fake_cache_claim_rejected(self):
        measured = self.provider().invoke(CASES[0].message)
        raw = copy.deepcopy(measured.raw_response)
        raw['answers']['is_safe']['noul'] = .1
        with self.assertRaisesRegex(ValueError, 'Saved response differs'):
            revalidate_audit(raw['execution_audit'], raw, require_verified=False)
        link = measured.raw_response['execution_audit']
        audit_path = Path(link['audit_path'])
        audit = json.loads(audit_path.read_text())
        audit['verified'] = True
        audit_path.write_text(json.dumps(audit))
        link['audit_sha256'] = file_hash(audit_path)
        with self.assertRaisesRegex(ValueError, 'cannot claim verified'):
            revalidate_audit(link, require_verified=False)

    def test_each_runner_persists_first_failure_and_stops_and_blocks_resume(self):
        for hard in (False, True):
            with self.subTest(hard=hard), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                provider = self.provider(hard)
                settings = dict(repetitions=2, warmups=0, output_dir=self.root,
                    max_new_calls=2, provider_factory=lambda *a, **k: provider,
                    definition=definition('hard_case' if hard else 'benchmark'), console=self.console)
                runner = run_hard_case_benchmark if hard else run_benchmark
                args = ([JEV_MODEL],) if hard else ([JEV_MODEL], [CASES[0]])
                with patch('benchmark.runner.report'), patch('hard_case.runner.report'):
                    with self.assertRaises(RunStopped):
                        runner(*args, **settings)
                    with self.assertRaises(RunStopped):
                        runner(*args, **settings)
                self.assertEqual(len(self.requests), 1)
                history = self.root / ('hard_case' if hard else 'benchmark') / 'attempt_history.csv'
                with history.open() as stream:
                    self.assertEqual(len(list(csv.DictReader(stream))), 1)

    def test_launcher_target_is_330_and_stops_before_second_suite(self):
        def fail(*args, **kwargs):
            raise RunStopped('fixture failure')
        with patch('run_jev_api.run_benchmark', side_effect=fail) as bench, \
             patch('run_jev_api.run_hard_case_benchmark') as hard:
            self.assertEqual(run_jev(self.root, console=self.console), 1)
            bench.assert_called_once()
            hard.assert_not_called()
            self.assertEqual(update_validation(self.root)['expected_measurements'], 330)
            with self.assertRaises(RunStopped):
                run_jev(self.root, console=self.console)
            self.assertEqual(bench.call_count, 1)

    def test_budget_reservations_settlement_unknown_usage_and_shared_limits(self):
        budget = JevBudget(self.root, budget_usd='0.01', max_calls=2)
        budget.reserve('first')
        with self.assertRaisesRegex(RunStopped, 'reservation'):
            JevBudget.for_root(self.root).reserve('second')
        budget.finish('first', 100, None)
        budget.reserve('second')
        budget.finish('second', None, None)
        with self.assertRaisesRegex(RunStopped, 'call budget'):
            JevBudget.for_root(self.root).reserve('third')
        ledger = json.loads(budget.path.read_text())
        self.assertEqual(Decimal(ledger['reservations'][0]['charge_usd']), 100 * INPUT_PRICE)
        self.assertEqual(Decimal(ledger['reservations'][1]['charge_usd']), MAX_REQUEST_CHARGE)
        with tempfile.TemporaryDirectory() as root:
            budget = JevBudget(root, budget_usd='0.000001')
            with self.assertRaisesRegex(RunStopped, 'exceed budget'):
                budget.reserve('too-expensive')


class JevCacheExceptionTests(JevFixture):
    def enable(self):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / 'run_plan.json').write_text(json.dumps({
            'kind': 'jev_api', 'models': [JEV_MODEL], 'jev_cache_exception': JEV_CACHE_EXCEPTION}))

    def test_ten_repetitions_both_suites_resume_reports_and_exact_columns(self):
        requests = []
        transports = []
        def factory(**kwargs):
            self.assertEqual(kwargs, {'retries': 0, 'trust_env': False})
            def handle(request):
                body = json.loads(request.content)
                requests.append(body)
                return httpx2.Response(200, json=response_body('route' not in body['questions']))
            transport = TrackingTransport(handle)
            transports.append(transport)
            return transport
        with patch('jev_execution.httpx2.HTTPTransport', side_effect=factory), \
             patch('benchmark.runner.experiment_definition', return_value=definition()), \
             patch('hard_case.runner.experiment_definition', return_value=definition('hard_case')), \
             patch('benchmark.runner.report'), patch('hard_case.runner.report'):
            self.assertEqual(run_jev(self.root, 10, 110, console=self.console,
                                    allow_unverified_server_cache=True), 0)
            self.assertEqual(len(requests), 110)
            self.assertEqual(run_jev(self.root, 10, 110, console=self.console,
                                    allow_unverified_server_cache=True), 0)
            self.assertEqual(len(requests), 110)
            with self.assertRaisesRegex(RunStopped, 'manifest changed'):
                run_jev(self.root, 10, 110, console=self.console)
        self.assertTrue(all(t.closed for t in transports))
        for hard in (False, True):
            suite = 'hard_case' if hard else 'benchmark'
            columns = __import__('hard_case.metrics' if hard else 'benchmark.metrics',
                                 fromlist=['RAW_COLUMNS']).RAW_COLUMNS
            data = json.loads((self.root / suite / 'metadata.json').read_text())['experiment']
            self.assertEqual(data['jev_cache_exception'], JEV_CACHE_EXCEPTION)
            with (self.root / suite / 'raw.csv').open() as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, columns)
                rows = list(reader)
            self.assertEqual(len(rows), 10 if hard else 100)
            for row in rows:
                self.assertEqual(row['validation_success'], 'True')
                self.assertEqual(row['cache_verified'], 'False')
                self.assertEqual(row['failure_kind'], '')
                revalidate_row(row, data)
            raw = json.loads(rows[0]['raw_response_json'])
            with self.assertRaisesRegex(ValueError, 'Cache verification failed'):
                revalidate_audit(raw['execution_audit'], raw)
            audit = revalidate_audit(raw['execution_audit'], raw, allow_unverified_jev=True)
            self.assertFalse(audit['verified'])
            self.assertEqual(audit['cache_status'], 'unverified')
            self.assertEqual(requests[100 if hard else 0]['questions'],
                             __import__('hard_case.prompts' if hard else 'benchmark.prompts',
                                        fromlist=['systemone_questions']).systemone_questions())
            if hard:
                self.assertEqual(requests[100]['state'], load_claim_packet().text)
            else:
                self.assertEqual(requests[0]['state'], CASES[0].message)
            bad_row = {**rows[0], 'cache_verified': 'True'}
            with self.assertRaisesRegex(ValueError, 'cache status'):
                revalidate_row(bad_row, data)
            with self.assertRaisesRegex(ValueError, 'Cache verification failed'):
                revalidate_row(rows[0], {k: v for k, v in data.items() if k != 'jev_cache_exception'})
            for field, value in [('input_tokens', '1'),
                                 (PROBABILITY_FIELDS[0] if hard else 'requires_web_probability', '.01')]:
                with self.assertRaisesRegex(ValueError, 'differ'):
                    revalidate_row({**rows[0], field: value}, data)
        manifest = update_validation(self.root)
        self.assertTrue(manifest['complete'])
        self.assertEqual(manifest['valid_measurements'], 110)
        self.assertFalse(manifest['cache_verified'])
        ledger = json.loads((self.root / 'jev_budget.json').read_text())
        self.assertIsNone(ledger['blocked_reason'])
        self.assertEqual(len(ledger['reservations']), 110)
        self.assertTrue(all(r['state'] == 'completed' for r in ledger['reservations']))
        report, _ = build_combined_report([self.root])
        self.assertIn('server caching unverified', report)
        self.assertIn('requires_web_probability_mean', report)
        self.assertIn('requires_human_review_mean', report)
        self.assertIn('cache_verified remains false', report)

    def test_exception_still_stops_after_provider_schema_and_model_errors(self):
        invalid = response_body()
        invalid['answers']['is_safe']['noul'] = 2
        wrong_model = {**response_body(), 'model': 'jev-other'}
        for status, body in [(401, {'error': 'fixture-secret-key'}), (302, {}),
                             (200, {'answers': {}}), (200, invalid), (200, wrong_model)]:
            with self.subTest(status=status, body=body), tempfile.TemporaryDirectory() as root:
                self.root = Path(root)
                self.enable()
                provider = self.provider(body=body, status=status)
                result = provider.invoke(CASES[0].message)
                self.assertTrue(result.error)
                self.assertFalse(result.raw_response['execution_audit']['accepted'])
                self.assertNotIn('fixture-secret-key', json.dumps(result.raw_response))
                with self.assertRaises(RunStopped):
                    provider.invoke(CASES[0].message)
                self.assertEqual(len(self.requests), 1)

    def test_missing_usage_retains_maximum_charge_and_acceptance_needs_opt_in(self):
        self.enable()
        result = self.provider(body=response_body(usage=False)).invoke(CASES[0].message)
        self.assertEqual(result.error, '')
        self.assertIsNone(result.input_tokens)
        ledger = json.loads((self.root / 'jev_budget.json').read_text())
        self.assertEqual(Decimal(ledger['reservations'][0]['charge_usd']), MAX_REQUEST_CHARGE)
        allowed = enforce_result(copy.deepcopy(result), JEV_MODEL, {}, self.root,
                                 allow_unverified_jev=True)
        self.assertEqual(allowed.error, '')
        strict = enforce_result(copy.deepcopy(result), JEV_MODEL, {}, self.root)
        self.assertTrue(strict.error.startswith('Cache verification failed:'))

    def test_saved_strict_rejection_cannot_be_retried_with_exception(self):
        self.provider().invoke(CASES[0].message)
        with self.assertRaisesRegex(RunStopped, 'rejected attempt'):
            run_jev(self.root, console=self.console, allow_unverified_server_cache=True)
        self.assertEqual(len(self.requests), 1)


class CombinedReportTests(JevFixture):
    def create_source(self, root, model='tev1:0.8b', hard=False, rejected=False, prefix=False):
        suite = 'hard_case' if hard else 'benchmark'
        data = definition(suite)
        data['model_configuration'] = {model: {}}
        if prefix:
            from run_control import PREFIX_STRATEGY
            data['prefix_strategy'] = PREFIX_STRATEGY
        store = HardStore(root / suite, data) if hard else ResultStore(root / suite, data)
        if rejected:
            self.root = root
            measured = self.provider(hard).invoke(load_claim_packet().text if hard else CASES[0].message)
        elif prefix:
            import httpx
            from langchain_mistralai import ChatMistralAI
            from benchmark.providers.mistral import MistralProvider
            def handle(request):
                return httpx.Response(200, json={'id': 'prefix-fixture', 'model': model,
                    'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {
                        'role': 'assistant', 'content': json.dumps(values())}}],
                    'usage': {'prompt_tokens': 100, 'completion_tokens': 30,
                              'prompt_tokens_details': {'cached_tokens': 0}}})
            def factory(**settings):
                return ChatMistralAI(**settings, client=httpx.Client(
                    base_url='https://fixture/v1', transport=httpx.MockTransport(handle)))
            with patch.dict('os.environ', {'MISTRAL_API_KEY': 'fixture'}), \
                 patch('benchmark.providers.mistral.ChatMistralAI', side_effect=factory):
                provider = MistralProvider(model, audit_directory=root / suite / 'execution_audit')
                provider.prefix_experiment = True
                measured = provider.invoke(CASES[0].message)
        elif hard:
            from hard_case.providers.base import validate_result
            measured = audited_result(validate_result({f: .5 for f in PROBABILITY_FIELDS}, {}), model, load_claim_packet().text)
        else:
            measured = audited_result(successful_result(), model, CASES[0].message)
        store.record(hard_row(model, 1, measured, 10) if hard else measured_row(model, CASES[0], 1, measured, 10))
        return store

    def test_mistral_prefix_pilot_is_reported_separately(self):
        original, prefix = self.root / 'original', self.root / 'prefix'
        self.create_source(original)
        self.create_source(prefix, model='mistral-small-latest', prefix=True)
        report, _ = build_combined_report([original, prefix])
        self.assertIn('## benchmark / original', report)
        self.assertIn('## benchmark / prefix_pilot', report)
        self.assertIn('never pooled', report)

    def test_rejected_values_and_usage_cannot_be_changed_in_csv(self):
        for field in ('requires_web_probability', 'input_tokens'):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                root = Path(root)
                self.create_source(root, model=JEV_MODEL, rejected=True)
                for name in ('raw.csv', 'attempt_history.csv'):
                    path = root / 'benchmark' / name
                    with path.open() as stream:
                        reader = csv.DictReader(stream)
                        columns = reader.fieldnames
                        rows = list(reader)
                    rows[0][field] = '0.1' if field.endswith('probability') else '1'
                    with path.open('w') as stream:
                        writer = csv.DictWriter(stream, fieldnames=columns)
                        writer.writeheader()
                        writer.writerows(rows)
                with self.assertRaisesRegex(ValueError, 'differ'):
                    build_combined_report([root])

    def test_combined_report_parity_rejections_plots_and_sources_unchanged(self):
        sources = [self.root / 'local', self.root / 'hosted']
        for hard in (False, True):
            self.create_source(sources[0], hard=hard)
            self.create_source(sources[1], model=JEV_MODEL, hard=hard, rejected=True)
            # Each suite fixture is independently constructed. Clear only fixture
            # spending evidence between suites; never done by production code.
            (sources[1] / 'jev_budget.json').unlink()
        before = {p: p.read_bytes() for root in sources for p in root.rglob('*') if p.is_file()}
        report, _ = build_combined_report(sources, output_directory=self.root / 'report')
        for field in ('input_tokens_mean', 'output_tokens_mean', 'cache_verification_failures',
                      'route_consistency', 'requires_human_review_std', 'Every latest repetition'):
            self.assertIn(field, report)
        self.assertIn(JEV_MODEL, report)
        self.assertIn('NA', report)
        self.assertTrue(list((self.root / 'report').rglob('*.png')))
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_duplicates_semantic_changes_and_audit_corruption_are_rejected(self):
        first, second = self.root / 'one', self.root / 'two'
        one = self.create_source(first)
        two = self.create_source(second)
        with self.assertRaisesRegex(ValueError, 'Duplicate measurement'):
            build_combined_report([first, second])
        metadata = second / 'benchmark/metadata.json'
        saved = json.loads(metadata.read_text())
        saved['experiment']['prompts']['llm_system'] += ' changed'
        saved['fingerprint'] = fingerprint(saved['experiment'])
        metadata.write_text(json.dumps(saved))
        with self.assertRaisesRegex(ValueError, 'semantic'):
            build_combined_report([first, second])
        link = json.loads(next(iter(one.latest.values()))['raw_response_json'])['execution_audit']
        Path(link['audit_path']).write_text('{}')
        with self.assertRaisesRegex(ValueError, 'fingerprint mismatch'):
            build_combined_report([first])

    def test_source_lock_and_protected_output_are_respected(self):
        import fcntl
        root = self.root / 'source'
        self.create_source(root)
        directory = root / 'benchmark'
        (directory / '.benchmark.lock').touch()
        with (directory / '.benchmark.lock').open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            with self.assertRaisesRegex(ValueError, 'currently writing'):
                build_combined_report([root])
        from show_evaluations import main
        before = (directory / 'raw.csv').read_bytes()
        with patch('sys.stderr', new=io.StringIO()):
            self.assertEqual(main(['--input-dirs', str(root), '--output', str(directory / 'raw.csv')]), 1)
        self.assertEqual(before, (directory / 'raw.csv').read_bytes())


if __name__ == '__main__':
    unittest.main()
