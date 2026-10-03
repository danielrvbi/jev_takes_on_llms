import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.console import Console

from benchmark.cases import CASES
from benchmark.providers.base import ProviderResult
from benchmark.runner import run_benchmark
from hard_case.runner import run_hard_case_benchmark
from run_control import CallGuard, RunStopped, experiment_settings
from run_prefix_pilot import run_pilot
from tests.test_benchmark import result
from runtime.testing import audited_result


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.console = Console(file=io.StringIO())

    def test_budget_includes_warmups_and_does_not_dispatch_extra_call(self):
        with tempfile.TemporaryDirectory() as directory, patch('benchmark.runner.report'):
            provider = Mock()
            provider.invoke.side_effect = lambda text: result()
            with self.assertRaisesRegex(RunStopped, 'budget exhausted'):
                run_benchmark(['fixture'], [CASES[0]], repetitions=3, warmups=1,
                    output_dir=directory, provider_factory=Mock(return_value=provider),
                    definition={'fixture':True}, console=self.console, max_new_calls=2)
            self.assertEqual(provider.invoke.call_count, 2)
            with (Path(directory)/'benchmark/attempt_history.csv').open() as f:
                self.assertEqual(len(list(csv.DictReader(f))), 1)

    def test_both_suites_persist_cache_failure_before_stopping(self):
        for suite in ('benchmark', 'hard_case'):
            with self.subTest(suite=suite), tempfile.TemporaryDirectory() as directory, \
                    patch('benchmark.runner.report'), patch('hard_case.runner.report'):
                provider = Mock()
                provider.invoke.side_effect = lambda text: ProviderResult(error='backend failed')
                kwargs=dict(repetitions=2, warmups=0, output_dir=directory,
                    provider_factory=Mock(return_value=provider), console=self.console)
                with self.assertRaisesRegex(RunStopped, 'saved failure'):
                    if suite == 'benchmark':
                        run_benchmark(['mistral-small-latest'], [CASES[0]], definition={'fixture':True}, **kwargs)
                    else:
                        run_hard_case_benchmark(['mistral-small-latest'], definition={'case_input':{'profile':'compact'}}, **kwargs)
                self.assertEqual(provider.invoke.call_count, 1)
                with (Path(directory)/suite/'attempt_history.csv').open() as f:
                    rows=list(csv.DictReader(f))
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]['failure_kind'], 'cache_verification')
                self.assertTrue(Path(rows[0]['audit_path']).exists())

    def test_prefix_requires_mistral_budget_and_distinct_root(self):
        root, guard=experiment_settings('results', ['mistral-small-latest'], True, 8)
        self.assertEqual(root, Path('results/prefix_pilot'))
        for models, budget in [(['gemma4:e4b'],8), (['mistral-small-latest'],None)]:
            with self.assertRaises(ValueError): experiment_settings('results',models,True,budget)
        for budget in (0,-1,True,1.5):
            with self.assertRaises(ValueError): CallGuard(budget)

    def test_pilot_stops_globally_and_refuses_retry(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('run_prefix_pilot.run_benchmark', side_effect=RunStopped('cached')) as bench, \
                patch('run_prefix_pilot.run_hard_case_benchmark') as hard:
            self.assertEqual(run_pilot(directory,self.console),1)
            self.assertEqual(bench.call_count,1)
            hard.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'prefix_pilot/benchmark/attempt_history.csv'
            path.parent.mkdir(parents=True)
            path.write_text('validation_success\nFalse\n')
            with patch('run_prefix_pilot.run_benchmark') as bench, self.assertRaisesRegex(RunStopped,'rejected attempt'):
                run_pilot(directory,self.console)
            bench.assert_not_called()

    def test_pilot_order_and_eight_call_limit(self):
        calls=[]
        def bench(models,cases,**settings):
            self.assertEqual(cases,[CASES[0]])
            calls.append(('benchmark',models[0],settings));return 0
        def hard(models,**settings):
            calls.append(('hard_case',models[0],settings));return 0
        with tempfile.TemporaryDirectory() as directory, patch('run_prefix_pilot.run_benchmark',side_effect=bench), \
                patch('run_prefix_pilot.run_hard_case_benchmark',side_effect=hard), \
                patch('run_prefix_pilot.update_validation',return_value={'complete':True}):
            self.assertEqual(run_pilot(directory,self.console),0)
        self.assertEqual([(s,m) for s,m,k in calls],[(s,m) for s in ('benchmark','hard_case')
            for m in ('mistral-small-latest','mistral-large-latest')])
        for _,_,settings in calls:
            self.assertEqual(settings['repetitions'],2)
            self.assertEqual(settings['warmups'],0)
            self.assertEqual(settings['max_new_calls'],2)
            self.assertTrue(settings['prefix_experiment'])

    def test_schema_failure_stops_prefix_experiment(self):
        guard=CallGuard(8,True)
        with self.assertRaises(RunStopped):
            guard.after_result('mistral-small-latest',ProviderResult(error='invalid schema'),self.console)

    def test_complete_pilot_revalidates_all_eight_mock_http_measurements(self):
        import hashlib
        import json
        import httpx
        from langchain_mistralai import ChatMistralAI
        from benchmark.prompts import SYSTEM_PROMPT as original_prompt
        from hard_case.prompts import SYSTEM_PROMPT as hard_prompt
        from hard_case.schemas import PROBABILITY_FIELDS
        from tests.test_benchmark import values
        requests=[]
        def handle(request):
            body=json.loads(request.content);requests.append(body)
            content=values() if len(body['messages'][1]['content']) < 1000 else {f:.5 for f in PROBABILITY_FIELDS}
            return httpx.Response(200,json={'id':str(len(requests)), 'model':body['model'],
                'choices':[{'index':0,'finish_reason':'stop','message':{'role':'assistant','content':json.dumps(content)}}],
                'usage':{'prompt_tokens':100,'completion_tokens':30,'prompt_tokens_details':{'cached_tokens':0}}})
        def factory(**settings):
            return ChatMistralAI(**settings,client=httpx.Client(base_url='https://fixture/v1',transport=httpx.MockTransport(handle)))
        def hard_definition(packet,models,**settings):
            return {'case_input':{'profile':'compact','sha256':hashlib.sha256(packet.text.encode()).hexdigest()},
                'prompts':{'llm_system':hard_prompt},'model_configuration':{m:{} for m in models}}
        definition={'prompts':{'llm_system':original_prompt},
            'cases':[{'case_id':1,'message':CASES[0].message}]}
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ',{'MISTRAL_API_KEY':'fixture'}), \
                patch('benchmark.providers.mistral.ChatMistralAI',side_effect=factory), \
                patch('hard_case.providers.mistral.ChatMistralAI',side_effect=factory), \
                patch('benchmark.runner.experiment_definition',return_value=definition), \
                patch('hard_case.runner.experiment_definition',side_effect=hard_definition), \
                patch('benchmark.runner.report'), patch('hard_case.runner.report'):
            self.assertEqual(run_pilot(directory,self.console),0)
            manifest=json.loads((Path(directory)/'prefix_pilot/validation.json').read_text())
            self.assertTrue(manifest['complete'])
            self.assertEqual(manifest['expected_measurements'],8)
            self.assertEqual(manifest['valid_measurements'],8)
            self.assertEqual(run_pilot(directory,self.console),0)
            self.assertEqual(len(requests),8)
            from benchmark.runner import ResultStore
            with self.assertRaisesRegex(ValueError,'Incompatible'):
                ResultStore(Path(directory)/'prefix_pilot/benchmark',{'execution_policy':__import__('execution').POLICY,**definition})
        self.assertEqual(len({r['messages'][0]['content'] for r in requests}),8)
