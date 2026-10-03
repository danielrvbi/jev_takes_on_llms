"""Real LangChain/SDK serialization over offline HTTP transports."""
import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx
from rich.console import Console

from jev_bench.providers.azure import AzureProvider
from jev_bench.providers.azure_config import AZURE_MODELS, configuration
from jev_bench.providers.base import InvocationContext
from jev_bench.run.azure import run_azure
from jev_bench.run.control import RunStopped
from jev_bench.runtime.execution import revalidate_audit, fingerprint
from jev_bench.suites import get_suite
from tests.fixtures.benchmark import values


ENV = {
    'AZURE_GPT_API_KEY': 'offline-key',
    'AZURE_GPT_BASE_URL': 'https://offline.services.ai.azure.com/openai/v1/',
    'AZURE_GPT_LUNA_MODEL': 'gpt-luna-deployment',
    'AZURE_GPT_SOL_MODEL': 'gpt-sol-deployment',
    'AZURE_CLAUDE_API_KEY': 'offline-key',
    'AZURE_CLAUDE_BASE_URL': 'https://offline.services.ai.azure.com/anthropic/',
    'AZURE_CLAUDE_OPUS_MODEL': 'claude-opus-deployment',
    'AZURE_CLAUDE_SONNET_MODEL': 'claude-sonnet-deployment',
}


def response_values(suite):
    return values() if suite == 'benchmark' else {name: 0.5 for name in get_suite(suite).schema.model_fields}


class OfflineAzure:
    def __init__(self, *, cached=0, omit_cache=False, malformed=False, status=200):
        self.cached, self.omit_cache, self.malformed, self.status = cached, omit_cache, malformed, status
        self.requests = []

    def transport(self, suite, model):
        if configuration(model)['provider'] == 'azure-claude':
            import httpx2 as http_transport
        else:
            http_transport = httpx
        def respond(request):
            payload = json.loads(request.content)
            self.requests.append(payload)
            if self.status != 200:
                return http_transport.Response(self.status, json={'error': {'message': 'offline rejection', 'type': 'invalid_request_error'}})
            content = 'not json' if self.malformed else json.dumps(response_values(suite))
            if request.url.path.endswith('/chat/completions'):
                usage = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120}
                if not self.omit_cache:
                    usage['prompt_tokens_details'] = {'cached_tokens': self.cached}
                body = {'id': 'chatcmpl-offline', 'object': 'chat.completion', 'created': 1, 'model': 'gpt-test',
                        'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': content}}],
                        'usage': usage}
            else:
                usage = {'input_tokens': 100, 'output_tokens': 20, 'cache_creation_input_tokens': 0}
                if not self.omit_cache:
                    usage['cache_read_input_tokens'] = self.cached
                body = {'id': 'msg-offline', 'type': 'message', 'role': 'assistant', 'model': 'claude-test',
                        'content': [{'type': 'text', 'text': content}], 'stop_reason': 'end_turn',
                        'stop_sequence': None, 'usage': usage}
            return http_transport.Response(200, json=body, headers={'request-id': 'offline-request', 'api-key': 'offline-key'})
        return http_transport.MockTransport(respond)

    def factory(self, suite):
        return lambda model, **kwargs: AzureProvider(model, suite=suite, transport=self.transport(suite, model), **kwargs)


class AzureProviderTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict('os.environ', ENV)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def invoke(self, backend, model='azure-gpt-luna', suite='benchmark'):
        directory = self.root / suite / 'execution_audit'
        provider = backend.factory(suite)(model, audit_directory=directory)
        return provider.invoke(get_suite(suite).request('offline input'),
                               InvocationContext(directory, prefix_experiment=True))

    def test_both_langchain_adapters_and_suites_use_audited_native_json(self):
        backend = OfflineAzure()
        for model in AZURE_MODELS:
            for suite in ('benchmark', 'hard_case'):
                with self.subTest(model=model, suite=suite):
                    result = self.invoke(backend, model, suite)
                    self.assertEqual(result.error, '')
                    self.assertEqual(result.values, response_values(suite))
                    self.assertEqual((result.input_tokens, result.output_tokens), (100, 20))
                    audit = revalidate_audit(result.raw_response['execution_audit'], result.raw_response)
                    self.assertTrue(audit['verified'])
                    self.assertEqual(audit['model'], model)
                    self.assertEqual(audit['requests'][0]['model'], configuration(model)['deployment'])
                    self.assertEqual(audit['http_response']['request_ids']['request-id'], 'offline-request')
                    self.assertNotIn('offline-key', json.dumps(audit))
        self.assertEqual(len(backend.requests), 8)

    def test_hits_missing_and_non_numeric_cache_evidence_are_rejected_once(self):
        for model in AZURE_MODELS:
            for cached, missing in [(1, False), (0, True), ('0', False), (False, False), (-1, False)]:
                with self.subTest(model=model, cached=cached, missing=missing):
                    backend = OfflineAzure(cached=cached, omit_cache=missing)
                    result = self.invoke(backend, model)
                    self.assertTrue(result.error)
                    self.assertFalse(result.raw_response['execution_audit']['verified'])
                    self.assertEqual(len(backend.requests), 1)
                    with self.assertRaises(ValueError):
                        revalidate_audit(result.raw_response['execution_audit'], result.raw_response)

    def test_http_errors_are_not_retried_and_remain_reportable(self):
        for model in AZURE_MODELS:
            backend = OfflineAzure(status=429)
            result = self.invoke(backend, model)
            self.assertTrue(result.error)
            audit = revalidate_audit(result.raw_response['execution_audit'], require_verified=False)
            self.assertEqual(audit['http_response']['status_code'], 429)
            self.assertEqual(len(backend.requests), 1)

    def test_parse_errors_are_saved_without_repair(self):
        for model in AZURE_MODELS:
            backend = OfflineAzure(malformed=True)
            result = self.invoke(backend, model)
            self.assertTrue(result.error)
            self.assertIsNone(result.output)
            self.assertEqual(len(backend.requests), 1)

    def test_wire_binding_rejects_message_schema_and_settings_tampering(self):
        from jev_bench.runtime.azure import verify_request
        result = self.invoke(OfflineAzure())
        audit = revalidate_audit(result.raw_response['execution_audit'], result.raw_response)
        for change in ['message', 'schema', 'temperature', 'deployment', 'retry']:
            modified = copy.deepcopy(audit)
            sent = modified['requests'][0]
            if change == 'message':
                sent['messages'][1]['content'] = 'wrong input'
            elif change == 'schema':
                sent['response_format']['json_schema']['schema'] = {}
            elif change == 'temperature':
                sent['temperature'] = 1
            elif change == 'deployment':
                sent['model'] = 'wrong deployment'
            else:
                modified['requests'].append(sent)
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_request(modified)

    def test_configuration_rejects_unsafe_urls_and_does_not_expose_keys(self):
        self.assertNotIn('offline-key', json.dumps(configuration('azure-gpt-luna')))
        for url in ['https://api.openai.com/openai/v1/', 'http://offline.openai.azure.com/openai/v1/',
                    'https://offline.openai.azure.com/openai/v1/?api-key=secret',
                    'https://user:secret@offline.openai.azure.com/openai/v1/']:
            with patch.dict('os.environ', {'AZURE_GPT_BASE_URL': url}), self.assertRaises(ValueError):
                configuration('azure-gpt-luna')

    def test_only_eight_environment_entries_configure_all_four_models(self):
        template = Path(__file__).resolve().parents[1] / '.env.example'
        names = {line.split('=', 1)[0] for line in template.read_text().splitlines() if line.strip()}
        self.assertEqual(names, set(ENV))
        with patch.dict('os.environ', ENV, clear=True):
            for model in AZURE_MODELS:
                self.assertTrue(configuration(model)['deployment'])


class AzureRunTests(unittest.TestCase):
    setUp = AzureProviderTests.setUp
    def run_backend(self, backend, phase, root, **kwargs):
        def factory(model, **settings):
            suite = 'benchmark' if Path(settings['audit_directory']).parent.name == 'benchmark' else 'hard_case'
            return backend.factory(suite)(model, **settings)
        with patch('jev_bench.run.benchmark.report'), patch('jev_bench.run.hard_case.report'):
            return run_azure(phase, root, provider_factory=factory, console=Console(file=io.StringIO()), **kwargs)

    def test_pilot_full_targets_resume_and_relocated_offline_reporting(self):
        backend = OfflineAzure()
        pilot = self.root / 'pilot'
        manifest = self.run_backend(backend, 'pilot', pilot)
        self.assertTrue(manifest['complete'])
        self.assertEqual(manifest['expected_measurements'], 16)
        self.assertEqual(len(backend.requests), 16)
        self.assertTrue(self.run_backend(backend, 'pilot', pilot)['complete'])
        self.assertEqual(len(backend.requests), 16)
        full = self.root / 'full'
        with self.assertRaises(RunStopped):
            self.run_backend(backend, 'full', full, pilot_dir=pilot, max_new_calls=1)
        saved = json.loads((full / 'validation.json').read_text())
        self.assertEqual(saved['expected_measurements'], 1320)
        self.assertEqual(saved['valid_measurements'], 1)
        manifest = self.run_backend(backend, 'full', full, pilot_dir=pilot)
        self.assertTrue(manifest['complete'])
        self.assertEqual(manifest['suites']['benchmark']['passed'], 1200)
        self.assertEqual(manifest['suites']['hard_case']['passed'], 120)
        self.assertEqual(len(backend.requests), 1336)
        from jev_bench.runtime.azure import text_content
        prefixes = [text_content(r['messages'][0]['content']) if r.get('messages', [])[0].get('role') == 'system'
                    else text_content(r['system']) for r in backend.requests]
        self.assertEqual(len(set(prefixes)), 1336)
        moved = self.root / 'moved'
        shutil.copytree(pilot, moved)
        from jev_bench.run_evaluations.validation import validate_saved
        self.assertTrue(validate_saved(moved, self.root / 'validation-report')['complete'])
        from jev_bench.run_evaluations.artifacts import render
        with patch('jev_bench.providers.registry.create_provider', side_effect=AssertionError('No model calls')):
            report = render(moved, self.root / 'reports')
        self.assertIn('Altered-prompt prefix experiment', report.read_text())

    def test_rejected_pilot_stops_after_one_call_and_cannot_resume_or_enable_full(self):
        backend = OfflineAzure(cached=1)
        pilot = self.root / 'pilot'
        with self.assertRaises(RunStopped):
            self.run_backend(backend, 'pilot', pilot)
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(json.loads((pilot / 'validation.json').read_text())['expected_measurements'], 16)
        with self.assertRaisesRegex(ValueError, 'Saved Azure rejection'):
            self.run_backend(backend, 'pilot', pilot)
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            self.run_backend(backend, 'full', self.root / 'full', pilot_dir=pilot)
        self.assertEqual(len(backend.requests), 1)

    def test_configuration_change_blocks_resume_before_sending(self):
        backend = OfflineAzure()
        pilot = self.root / 'pilot'
        self.run_backend(backend, 'pilot', pilot)
        with patch.dict('os.environ', {'AZURE_GPT_LUNA_MODEL': 'changed'}):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.run_backend(backend, 'pilot', pilot)
        self.assertEqual(len(backend.requests), 16)

    def test_interrupted_call_is_saved_and_resume_skips_successes(self):
        backend = OfflineAzure()
        pilot = self.root / 'pilot'
        counter = 0
        def factory(model, **settings):
            suite = Path(settings['audit_directory']).parent.name
            provider = backend.factory(suite)(model, **settings)
            invoke = provider.invoke
            def interrupted(request, context):
                nonlocal counter
                counter += 1
                if counter == 2:
                    with patch('jev_bench.providers.azure.create_chat', side_effect=KeyboardInterrupt):
                        return invoke(request, context)
                return invoke(request, context)
            provider.invoke = interrupted
            return provider
        with patch('jev_bench.run.benchmark.report'), patch('jev_bench.run.hard_case.report'), self.assertRaises(KeyboardInterrupt):
            run_azure('pilot', pilot, provider_factory=factory, console=Console(file=io.StringIO()))
        self.assertEqual(len(backend.requests), 1)
        self.assertEqual(json.loads((pilot / 'validation.json').read_text())['valid_measurements'], 1)
        manifest = self.run_backend(backend, 'pilot', pilot)
        self.assertTrue(manifest['complete'])
        self.assertEqual(len(backend.requests), 16)
        self.assertEqual(manifest['suites']['benchmark']['attempts'], 9)

    def test_pilot_metadata_tampering_blocks_full(self):
        from jev_bench.run.azure import verify_pilot
        backend = OfflineAzure()
        pilot = self.root / 'pilot'
        self.run_backend(backend, 'pilot', pilot)
        path = pilot / 'benchmark' / 'metadata.json'
        metadata = json.loads(path.read_text())
        metadata['experiment']['model_configuration']['azure-gpt-luna']['deployment'] = 'tampered'
        metadata['fingerprint'] = fingerprint(metadata['experiment'])
        path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, 'definitions differ'):
            verify_pilot(pilot, json.loads((pilot / 'run_plan.json').read_text())['compatibility_sha256'])

    def test_offline_config_check_and_help_do_not_load_unrelated_sdks(self):
        script = '''import sys
from jev_bench.run.azure import main
assert main(['--phase', 'pilot', '--check-config']) == 0
assert not any(n.startswith(('langchain_typesafe', 'httpx2', 'langchain_ollama', 'langchain_mistralai')) for n in sys.modules)
'''
        process = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn('offline-key', process.stdout)
