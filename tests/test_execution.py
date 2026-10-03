import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx
from langchain_core.caches import InMemoryCache
from langchain_core.globals import get_llm_cache, set_llm_cache
from langchain_mistralai import ChatMistralAI
from rich.console import Console

from execution import (AuditError, POLICY, fingerprint, hosted_call, local_call, revalidate_audit,
                       stop_process_tree, verify_cold_log, verify_mistral_cache)
from benchmark.cases import CASES
from benchmark.providers.mistral import MistralProvider
from benchmark.runner import ResultStore, measured_row, run_benchmark
from hard_case.main import parse_args as hard_args
from main import parse_args
from runtime.testing import audited_result
from tests.test_benchmark import result, values


def task_log(task, tokens=123):
    return f'''msg="experiment prompt request" kind=score prompt_tokens={tokens} cache_prompt=false
slot operator(): id 0 | task {task} | new prompt, n_ctx_slot = 262144, task.n_tokens = {tokens}
slot operator(): id 0 | task {task} | cached n_tokens = 0, memory_seq_rm [0, end)
slot operator(): id 0 | task {task} | cached n_tokens = 111
slot print_timing: id 0 | task {task} | prompt eval time = 500.00 ms / {tokens} tokens
'''


COLD_LOG = task_log(0) + task_log(4) + '''[GIN] POST "/v1/systemone"
msg="llama-server stopped" pid=456
'''


class PromptVerificationTests(unittest.TestCase):
    def test_every_prompt_and_retry_is_verified(self):
        self.assertEqual(len(verify_cold_log(COLD_LOG, minimum_tasks=2)['prompt_tasks']), 2)
        retry = task_log(8) + COLD_LOG
        self.assertEqual(len(verify_cold_log(retry, minimum_tasks=2)['prompt_tasks']), 3)

    def test_later_cached_partial_restored_truncated_missing_or_hidden_task_is_rejected(self):
        for log in [COLD_LOG.replace('task 4 | cached n_tokens = 0', 'task 4 | cached n_tokens = 120'),
                    COLD_LOG.replace('task 4 | prompt eval time = 500.00 ms / 123', 'task 4 | prompt eval time = 500.00 ms / 3'),
                    COLD_LOG + 'restored context checkpoint', COLD_LOG + 'truncated = 1',
                    COLD_LOG.replace('task 4 | prompt eval time', 'task 4 | unknown time'),
                    COLD_LOG + 'msg="experiment prompt request" kind=score prompt_tokens=123 cache_prompt=false',
                    COLD_LOG.replace('cache_prompt=false', 'cache_prompt=true'),
                    COLD_LOG.replace('llama-server stopped', 'unknown worker state'),
                    COLD_LOG + '[GIN] POST "/v1/systemone"']:
            with self.subTest(log=log), self.assertRaises(ValueError):
                verify_cold_log(log, minimum_tasks=2)

    def test_empty_schema_conversion_does_no_inference(self):
        empty = """slot operator(): id 0 | task 99 | new prompt, task.n_tokens = 0
slot print_timing: id 0 | task 99 | prompt eval time = 0.00 ms / 0 tokens
"""
        log = COLD_LOG.replace('/v1/systemone', '/api/chat')
        evidence = verify_cold_log(empty + log, minimum_tasks=2, endpoint='/api/chat')
        self.assertTrue(evidence['prompt_tasks'][0]['empty_schema_conversion'])
        with self.assertRaises(ValueError):
            verify_cold_log(empty.replace('0.00 ms / 0 tokens', '0.00 ms / 1 tokens') + log, endpoint='/api/chat')

    def test_explicit_zero_only_for_hosted_telemetry(self):
        def raw(value):
            return {'response_metadata': {'token_usage': {'prompt_tokens_details': {'cached_tokens': value}}}}
        for value in [0, 0.0]:
            self.assertEqual(verify_mistral_cache(raw(value)), 0)
        for value in [1, None, '0', False, float('nan'), -1, {}]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                verify_mistral_cache(raw(value))
        with self.assertRaisesRegex(ValueError, 'missing'):
            verify_mistral_cache({})


class ProcessLifecycleTests(unittest.TestCase):
    def test_fresh_server_client_each_call_and_cleanup_on_error_or_interrupt(self):
        with tempfile.TemporaryDirectory() as directory:
            http = Mock()
            http.get.side_effect = [Mock(json=lambda: {'version': '0.35.0'}), Mock(json=lambda: {'models': []})] * 4
            client = Mock()
            client.ps.return_value.model_dump.return_value = {'models': [{'model': 'tev1:0.8b'}]}
            processes, logs = [], []
            def launch(command, **settings):
                process = Mock(pid=1000 + len(processes), returncode=0)
                process.poll.return_value = None
                settings['stdout'].write(('Listening on ' + settings['env']['OLLAMA_HOST'][7:] + ' (version 0.35.0)\n').encode())
                settings['stdout'].flush()
                processes.append(process)
                logs.append(settings['stdout'])
                self.assertTrue(settings['start_new_session'])
                self.assertEqual(settings['env']['OLLAMA_NUM_PARALLEL'], '1')
                return process
            def cleanup(process):
                logs[processes.index(process)].write(COLD_LOG.encode())
                logs[processes.index(process)].flush()
                return True
            def invoke(host, audit):
                audit['requests'] = [{'model': 'tev1:0.8b', 'state': 'unchanged'}]
                audit['request_sha256'] = fingerprint(audit['requests'][0])
                return {'model': 'tev1:0.8b'}
            with patch('execution.runtime_identity', return_value={'binary': 'bin/ollama'}), \
                 patch('execution.model_identity', return_value={'model': 'fixture'}), \
                 patch('execution.subprocess.Popen', side_effect=launch), \
                 patch('execution.stop_process_tree', side_effect=cleanup) as stop, \
                 patch('execution.httpx.Client') as http_factory, patch('execution.ollama.Client') as clients:
                http_factory.return_value.__enter__.return_value = http
                clients.return_value.__enter__.return_value = client
                args = ('tev1:0.8b', {'model': 'tev1:0.8b', 'state': 'unchanged'}, directory)
                kwargs = dict(minimum_tasks=2, endpoint='/v1/systemone')
                audits = [local_call(*args, invoke, **kwargs)[1] for _ in range(2)]
                for failure in [RuntimeError('backend failed'), KeyboardInterrupt()]:
                    def fail(host, audit): raise failure
                    with self.assertRaises(KeyboardInterrupt if isinstance(failure, KeyboardInterrupt) else AuditError):
                        local_call(*args, fail, **kwargs)
            self.assertEqual(len(processes), 4)
            self.assertEqual(stop.call_count, 4)
            self.assertEqual(len({a['call_id'] for a in audits}), 2)
            self.assertEqual(len({a['server_pid'] for a in audits}), 2)
            self.assertTrue(all(a['verified'] and a['process_tree_stopped'] for a in audits))
            self.assertEqual(len(list(Path(directory).glob('*.json'))), 4)

    def test_hung_server_is_killed_reaped_and_group_absence_checked(self):
        process = Mock(pid=1234)
        process.wait.side_effect = [subprocess.TimeoutExpired('serve', 10), 0]
        with patch('execution.os.killpg', side_effect=[None, None, None, ProcessLookupError()]) as kill:
            self.assertTrue(stop_process_tree(process))
        self.assertEqual(kill.call_args_list[-1].args, (1234, 0))
        self.assertEqual(process.wait.call_count, 2)
        self.assertEqual(kill.call_args_list[1].args, (1234, signal.SIGKILL))

    def test_server_early_exit_is_audited_and_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            process = Mock(pid=99999, returncode=1)
            process.poll.return_value = 1
            with patch('execution.runtime_identity', return_value={'binary':'bin/ollama'}), patch('execution.model_identity', return_value={}), \
                 patch('execution.subprocess.Popen', return_value=process), patch('execution.stop_process_tree', return_value=True) as stop:
                with self.assertRaises(AuditError):
                    local_call('tev1:0.8b', {}, directory, Mock(), minimum_tasks=1, endpoint='/v1/systemone')
            stop.assert_called_once_with(process)
            self.assertFalse(json.loads(next(Path(directory).glob('*.json')).read_text())['verified'])


class HostedClientTests(unittest.TestCase):
    def exercise(self, cached=0, count=2, prefix=False):
        requests, clients = [], []
        with tempfile.TemporaryDirectory() as directory:
            def handle(request):
                requests.append(json.loads(request.content))
                usage = {'prompt_tokens': 10, 'completion_tokens': 20, 'total_tokens': 30}
                if cached != 'missing': usage['prompt_tokens_details'] = {'cached_tokens': cached}
                return httpx.Response(200, json={'id': 'fixture', 'model': 'fixture-model', 'choices': [{'index':0, 'finish_reason':'stop', 'message':{'role':'assistant', 'content':json.dumps(values())}}], 'usage':usage})
            def factory(**settings):
                self.assertIs(settings['cache'], False)
                self.assertEqual(settings['max_retries'], 0)
                self.assertEqual(settings['temperature'], 0)
                self.assertNotIn('seed', settings)
                llm = ChatMistralAI(**settings, client=httpx.Client(base_url='https://fixture/v1', transport=httpx.MockTransport(handle)))
                clients.append(llm)
                return llm
            set_llm_cache(InMemoryCache())
            with patch.dict('os.environ', {'MISTRAL_API_KEY': 'fixture'}), patch('benchmark.providers.mistral.ChatMistralAI', side_effect=factory):
                provider = MistralProvider('mistral-small-latest', audit_directory=directory)
                provider.prefix_experiment = prefix
                results = [provider.invoke(CASES[0].message) for _ in range(count)]
            self.assertIsNone(get_llm_cache())
            self.assertEqual(len(clients), count)
            self.assertTrue(all(c.client.is_closed for c in clients))
            self.assertEqual(len({r['prompt_cache_key'] for r in requests}), count)
            if prefix:
                from benchmark.prompts import SYSTEM_PROMPT
                identifiers = [r['messages'][0]['content'].split('\n\n', 1)[0] for r in requests]
                self.assertEqual(len(set(identifiers)), count)
                for r in requests:
                    self.assertRegex(r['messages'][0]['content'], r'^Request identifier: [0-9a-f]{32}\n\n')
                    self.assertEqual(r['messages'][0]['content'].split('\n\n', 1)[1], SYSTEM_PROMPT)
                    self.assertEqual(r['response_format'], requests[0]['response_format'])
            else:
                self.assertTrue(all(r['messages'] == requests[0]['messages'] for r in requests))
            self.assertEqual(requests[0]['messages'][1]['content'], CASES[0].message)
            for r in results:
                revalidate_audit(r.raw_response['execution_audit'], require_verified=False)
            return results

    def test_fresh_keys_clients_disabled_cache_and_unchanged_messages(self):
        results = self.exercise()
        self.assertTrue(all(r.output is not None and not r.error for r in results))

    def test_prefix_requests_preserve_schema_settings_and_original_text(self):
        results = self.exercise(prefix=True)
        self.assertTrue(all(not r.error for r in results))

    def test_cached_and_missing_telemetry_preserved_and_not_retried(self):
        for cached in [10, 'missing']:
            results = self.exercise(cached, count=1)
            self.assertIsNone(results[0].output)
            self.assertIn('Cache verification failed', results[0].error)
            self.assertIn('content', results[0].raw_response)
            self.assertEqual(results[0].input_tokens, 10)
            self.assertEqual(results[0].output_tokens, 20)


class ResumeAndLayoutTests(unittest.TestCase):
    def test_only_one_policy_and_common_root(self):
        self.assertEqual(parse_args(['--all']).output_dir, hard_args(['--all']).output_dir)
        for parse in [parse_args, hard_args]:
            with self.assertRaises(SystemExit), patch('sys.stderr', new=io.StringIO()):
                parse(['--all', '--systemone-execution', 'shared'])
        with tempfile.TemporaryDirectory() as directory, patch('benchmark.runner.report'):
            provider = Mock()
            provider.invoke.side_effect = lambda message: result()
            kwargs = dict(models=['fixture'], cases=[CASES[0]], repetitions=1, warmups=2, output_dir=directory,
                          provider_factory=Mock(return_value=provider), definition={'fixture':True}, console=Console(file=io.StringIO()))
            self.assertEqual(run_benchmark(**kwargs), 0)
            self.assertEqual(provider.invoke.call_count, 3)
            self.assertEqual(run_benchmark(**kwargs), 0)
            self.assertEqual(provider.invoke.call_count, 3)
            self.assertTrue((Path(directory) / 'benchmark/raw.csv').exists())
            self.assertTrue((Path(directory) / 'validation.json').exists())

    def test_audit_response_tampering_or_old_metadata_prevents_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            definition = {'execution_policy': POLICY}
            store = ResultStore(directory, definition)
            row = measured_row('fixture', CASES[0], 1, result(), 10)
            store.record(row)
            self.assertFalse(ResultStore(directory, definition).pending('fixture',1,1))
            raw = json.loads(row['raw_response_json'])
            Path(raw['execution_audit']['audit_path']).write_text('{}')
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                ResultStore(directory, definition)
        with tempfile.TemporaryDirectory() as directory, self.assertRaisesRegex(ValueError, 'mandatory'):
            ResultStore(directory, {'old':True})

    def test_common_root_keeps_csv_schemas_separate(self):
        from hard_case.runner import run_hard_case_benchmark
        from hard_case.schemas import PROBABILITY_FIELDS
        from hard_case.providers.base import validate_result
        import csv
        with tempfile.TemporaryDirectory() as directory, patch('benchmark.runner.report'), patch('hard_case.runner.report'):
            original = Mock()
            original.invoke.side_effect = lambda message: result()
            hard = Mock()
            hard.invoke.side_effect = lambda message: audited_result(validate_result({f:.5 for f in PROBABILITY_FIELDS}, {}))
            console = Console(file=io.StringIO())
            self.assertEqual(run_benchmark(['fixture'], [CASES[0]], 1, 0, directory,
                provider_factory=Mock(return_value=original), definition={'fixture':True}, console=console), 0)
            self.assertEqual(run_hard_case_benchmark(['fixture'], 1, 0, directory,
                provider_factory=Mock(return_value=hard), definition={'case_input':{'profile':'compact'}}, console=console), 0)
            with (Path(directory)/'benchmark/raw.csv').open() as f: original_header=next(csv.reader(f))
            with (Path(directory)/'hard_case/raw.csv').open() as f: hard_header=next(csv.reader(f))
            self.assertIn('case_id',original_header)
            self.assertNotIn('case_id',hard_header)
            self.assertTrue(set(PROBABILITY_FIELDS) <= set(hard_header))

    def test_saved_probability_tampering_prevents_resume(self):
        import csv
        with tempfile.TemporaryDirectory() as directory:
            definition = {'execution_policy': POLICY}
            store = ResultStore(directory, definition)
            store.record(measured_row('fixture', CASES[0], 1, result(), 10))
            path = Path(directory)/'attempt_history.csv'
            with path.open() as f:
                reader=csv.DictReader(f); fields=reader.fieldnames; rows=list(reader)
            rows[0]['requires_web_probability'] = '0.75'
            with path.open('w') as f:
                writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows(rows)
            with self.assertRaisesRegex(ValueError,'probabilities differ'):
                ResultStore(directory,definition)

    def test_manifest_requires_all_55_independent_audited_keys(self):
        from validation import update_validation
        from benchmark.providers import MODELS
        from tests.test_benchmark import measured_row as fixture_original_row
        from hard_case.runner import ResultStore as HardStore, measured_row as hard_row
        from hard_case.providers.base import validate_result
        from hard_case.schemas import PROBABILITY_FIELDS
        import hashlib
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            definition={'execution_policy':POLICY,'cases':[{'case_id':case.case_id,'message':case.message} for case in CASES],
                        'model_configuration':{model:{} for model in MODELS}}
            original=ResultStore(root/'benchmark',definition)
            for model in MODELS:
                for case in CASES:
                    original.record(fixture_original_row(model,case,1,result(),10))
            hard_definition={'execution_policy':POLICY,'case_input':{'profile':'compact','sha256':hashlib.sha256(b'packet').hexdigest()},
                             'model_configuration':{model:{} for model in MODELS}}
            hard=HardStore(root/'hard_case',hard_definition)
            for model in MODELS[:-1]:
                r=audited_result(validate_result({f:.5 for f in PROBABILITY_FIELDS},{}),model,'packet')
                hard.record(hard_row(model,1,r,10))
            manifest=update_validation(root)
            self.assertFalse(manifest['complete'])
            self.assertEqual(manifest['valid_measurements'],54)
            model=MODELS[-1]
            r=audited_result(validate_result({f:.5 for f in PROBABILITY_FIELDS},{}),model,'packet')
            hard.record(hard_row(model,1,r,10))
            manifest=update_validation(root)
            self.assertTrue(manifest['complete'])
            self.assertEqual(manifest['valid_measurements'],55)
            from show_evaluations import main as report_main
            with patch('sys.stderr',new=io.StringIO()):
                self.assertEqual(report_main(['--input-dir',str(root),'--output',str(root/'evaluation.md')]),0)
            rendered=(root/'evaluation.md').read_text()
            self.assertIn('# Hard-case insurance benchmark',rendered)
            self.assertIn('Cache verification and cold timings',rendered)
            link=json.loads(hard.latest[(model,1)]['raw_response_json'])['execution_audit']
            Path(link['audit_path']).write_text('{}')
            self.assertFalse(update_validation(root)['complete'])

    def test_validation_does_not_read_a_partially_written_suite(self):
        from execution import output_lock
        from validation import update_validation
        with tempfile.TemporaryDirectory() as root:
            with output_lock(Path(root)/'benchmark'):
                manifest=update_validation(root)
                self.assertFalse(manifest['complete'])
                self.assertEqual(manifest['status'],'suite_writer_active')

    def test_quarantine_root_is_rejected_before_writing(self):
        from execution import suite_directory
        with self.assertRaisesRegex(ValueError, 'Quarantined'):
            suite_directory('results_contaminated_dont_use', 'benchmark')
