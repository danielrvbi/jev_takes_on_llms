import copy
from dataclasses import replace
import hashlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import ollama
from langchain_core.messages import AIMessage
from rich.console import Console

from hard_case.loader import (COMPACT_DIR, DATA_DIR, SOURCE_FILENAMES, ClaimPacket,
                              compact_records, load_claim_packet)
from hard_case.main import parse_args
from hard_case.providers import create_provider, model_configuration
from hard_case.providers.base import StructuredChatProvider
from hard_case.providers.context import (SYSTEMONE_REQUEST_BUDGET, inference_model,
                                         serialized_request, validate_alias, validate_request_budget)
from hard_case.providers.ollama_systemone import SystemOneProvider
from hard_case.runner import ResultStore, experiment_definition, fingerprint, merged_definition, run_hard_case_benchmark
from hard_case.schemas import HardCaseOutput, PROBABILITY_FIELDS


def values():
    # Mapping fixtures, never expected claim probabilities.
    return {field: 0.5 for field in PROBABILITY_FIELDS}


def model_info(blob='a' * 64, context=2050):
    return ollama.ShowResponse(parameters=f'num_ctx {context}', template='original template',
                              modelfile=f'FROM /cache/sha256-{blob}\nPARAMETER num_ctx {context}\nTEMPLATE original template',
                              capabilities=['systemone'], model_info={'qwen35.context_length': 262144})


class CompactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.data = self.directory / 'data'
        self.compact = self.directory / 'compact'
        shutil.copytree(DATA_DIR, self.data)
        shutil.copytree(COMPACT_DIR, self.compact)

    def tearDown(self):
        self.temp.cleanup()

    def load(self):
        return load_claim_packet(self.data, profile='compact', compact_directory=self.compact)

    def rewrite_map(self):
        packet = (self.compact / 'packet.md').read_text()
        mapping = json.loads((self.compact / 'source_map.json').read_text())
        mapping['compact_packet_sha256'] = hashlib.sha256(packet.encode()).hexdigest()
        mapping['records'] = compact_records(packet)
        (self.compact / 'source_map.json').write_text(json.dumps(mapping))

    def test_packet_order_and_provenance_cover_all_nine_sources(self):
        packet = self.load()
        records = compact_records(packet.text)
        self.assertEqual(list(dict.fromkeys(r['source'] for r in records.values())), list(SOURCE_FILENAMES))
        self.assertEqual(packet, self.load())
        self.assertGreater(len(records), 60)
        self.assertNotIn('README.md', packet.text)
        original = '\n\n'.join(f"===== {name} =====\n{(self.data / name).read_bytes().decode('utf-8')}"
                               for name in SOURCE_FILENAMES)
        self.assertEqual(packet.original_source_sha256, hashlib.sha256(original.encode('utf-8')).hexdigest())
        self.assertEqual(packet.profile, 'compact')

    def test_designer_readme_is_never_opened_for_compact_profile(self):
        original = Path.read_bytes
        def allowed(path):
            self.assertNotEqual(path.name, 'README.md')
            return original(path)
        with patch.object(Path, 'read_bytes', new=allowed):
            self.load()

    def test_source_change_invalidates_compact_before_inference(self):
        with (self.data / 'timeline.md').open('a') as handle:
            handle.write('\nChanged fixture source\n')
        with self.assertRaisesRegex(ValueError, 'source content changed'):
            self.load()

    def test_loader_has_no_full_packet_mode_and_defaults_to_compact(self):
        self.assertEqual(load_claim_packet(self.data, compact_directory=self.compact), self.load())
        for profile in ['full', 'unknown']:
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, 'Only the compact'):
                load_claim_packet(self.data, profile=profile, compact_directory=self.compact)

    def test_runner_rejects_noncompact_inputs_before_calls_or_result_writes(self):
        compact = self.load()
        originals = '\n\n'.join(f"===== {name} =====\n{(self.data / name).read_text()}"
                                  for name in SOURCE_FILENAMES)
        invalid_packets = [replace(compact, profile='full'), replace(compact, text=originals),
                           replace(compact, text=compact.text.replace('PW01 | L1-24 | ',
                                                                    'PW01 | L1-24 | ' + 'x' * 51200))]
        output = self.directory / 'rejected'
        for packet in invalid_packets:
            factory = Mock()
            with self.subTest(packet_profile=packet.profile), self.assertRaises(ValueError):
                run_hard_case_benchmark(['mistral-small-latest'], repetitions=1, warmups=0,
                                       packet=packet, provider_factory=factory, output_dir=output,
                                       console=Console(file=io.StringIO()))
            factory.assert_not_called()
            self.assertFalse(output.exists())
        with self.assertRaisesRegex(ValueError, 'Only the compact'):
            run_hard_case_benchmark(['mistral-small-latest'], packet=compact, input_profile='full',
                                   output_dir=output)
        self.assertFalse(output.exists())

    def test_historical_full_metadata_cannot_resume_and_files_stay_unchanged(self):
        packet = self.load()
        definition = experiment_definition(packet, ['mistral-small-latest'], {})
        output = self.directory / 'historical'
        output.mkdir()
        # Legacy full runs omitted the profile field entirely; also reject explicitly labeled full.
        for profile in [None, 'full']:
            previous = copy.deepcopy(definition)
            if profile is None:
                previous['case_input'].pop('profile')
            else:
                previous['case_input']['profile'] = profile
            (output / 'metadata.json').write_text(json.dumps({
                'kind': 'hard_case_benchmark', 'experiment': previous, 'fingerprint': fingerprint(previous)}))
            (output / 'raw.csv').write_text('historical raw sentinel')
            (output / 'attempt_history.csv').write_text('historical history sentinel')
            snapshot = {p.name: p.read_bytes() for p in output.iterdir() if p.name != '.benchmark.lock'}
            factory = Mock()
            with self.assertRaisesRegex(ValueError, 'historical non-compact'):
                run_hard_case_benchmark(['mistral-small-latest'], repetitions=1, warmups=0,
                                       packet=packet, definition=definition, provider_factory=factory,
                                       output_dir=output, console=Console(file=io.StringIO()))
            factory.assert_not_called()
            self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()
                              if p.name != '.benchmark.lock'}, snapshot)

    def test_current_definition_retains_compact_metadata_layout_for_resume(self):
        packet = self.load()
        definition = experiment_definition(packet, ['mistral-small-latest'], {})
        self.assertEqual(definition['format_version'], 1)
        self.assertEqual(definition['input_format'],
                         'reviewed compact evidence records with source line references; identical state for all backends')
        self.assertEqual(set(definition['case_input']), {
            'source_filenames', 'sha256', 'character_count', 'word_count', 'utf8_byte_count',
            'profile', 'original_source_sha256', 'compact_packet_sha256', 'source_map_sha256',
            'json_encoded_bytes'})
        self.assertEqual(merged_definition(copy.deepcopy(definition), definition), definition)

    def test_packet_change_requires_reviewed_map_update(self):
        with (self.compact / 'packet.md').open('a') as handle:
            handle.write('altered')
        with self.assertRaisesRegex(ValueError, 'packet hash'):
            self.load()

    def test_malformed_provenance_and_omitted_source_lines_rejected(self):
        path = self.compact / 'packet.md'
        text = path.read_text()
        path.write_text(text.replace('PW01 | L1-24', 'PW01 | L1-500'))
        self.rewrite_map()
        with self.assertRaisesRegex(ValueError, 'range exceeds'):
            self.load()
        path.write_text(text.replace('PW01 | L1-24', 'PW01 | L1-22'))
        self.rewrite_map()
        with self.assertRaisesRegex(ValueError, 'omits source lines'):
            self.load()
        path.write_text(text.replace('===== fnol.md =====', '===== README.md ====='))
        with self.assertRaisesRegex(ValueError, 'nine ordered'):
            self.rewrite_map()

    def test_exact_serialization_matches_sdk_transport_including_utf8_and_escaping(self):
        packet = self.load()
        actual = inference_model('tev1:4b', 262144)
        captured = []
        def respond(request):
            captured.append(request.content)
            return httpx.Response(200, json={'model': actual,
                                            'usage': {'input_tokens': 100, 'output_tokens': 6},
                                            'answers': {f: {'type': 'noul', 'noul': p}
                                                       for f, p in values().items()}})
        client = ollama.Client(transport=httpx.MockTransport(respond))
        from hard_case.prompts import systemone_questions
        state = packet.text + '\n"quotes"\\slash\t€\ré'
        client.systemone(model=actual, state=state, questions=systemone_questions())
        self.assertEqual(captured[0], serialized_request(actual, state))
        self.assertLessEqual(packet.metadata()['json_encoded_bytes'], 50 * 1024)
        self.assertLessEqual(validate_request_budget(actual, packet.text), 60 * 1024)

    def test_complete_request_and_packet_budgets_fail_without_truncation(self):
        # Find the exact transport boundary using a single-byte character.
        name = inference_model('tev1:4b', 262144)
        overhead = len(serialized_request(name, ''))
        state = 'x' * (SYSTEMONE_REQUEST_BUDGET - overhead)
        self.assertEqual(validate_request_budget(name, state), SYSTEMONE_REQUEST_BUDGET)
        with self.assertRaisesRegex(ValueError, '60 KiB'):
            validate_request_budget(name, state + 'x')
        with patch('hard_case.providers.ollama_systemone.ollama.systemone') as native:
            with self.assertRaises(ValueError):
                SystemOneProvider('tev1:4b').invoke(state + 'x' * 200)
            native.assert_not_called()
        path = self.compact / 'packet.md'
        path.write_text(path.read_text().replace('PW01 | L1-24 | ', 'PW01 | L1-24 | ' + '€' * 18000))
        self.rewrite_map()
        with self.assertRaisesRegex(ValueError, '50 KiB'):
            self.load()

    def test_map_profile_context_changes_fingerprint_and_reject_resume(self):
        compact = experiment_definition(self.load(), ['tev1:4b'], {}, systemone_context=262144)
        full = copy.deepcopy(compact)
        full['case_input']['profile'] = 'full'  # Historical metadata fixture, never an inference input.
        with self.assertRaisesRegex(ValueError, 'Incompatible resume'):
            merged_definition(compact, full)
        context_change = experiment_definition(self.load(), ['tev1:4b'], {})
        self.assertNotEqual(fingerprint(compact), fingerprint(context_change))
        with self.assertRaises(ValueError):
            merged_definition(compact, context_change)
        mapping = self.compact / 'source_map.json'
        mapping.write_text(mapping.read_text() + '\n')
        changed = experiment_definition(self.load(), ['tev1:4b'], {}, systemone_context=262144)
        self.assertNotEqual(fingerprint(compact), fingerprint(changed))
        with self.assertRaises(ValueError):
            merged_definition(compact, changed)

    def test_compact_identical_delivery_to_native_and_structured_chat(self):
        packet = self.load()
        native_response = {'answers': {f: {'noul': p} for f, p in values().items()}}
        structured = Mock()
        structured.invoke.return_value = {'parsed': HardCaseOutput(**values()),
                                         'raw': AIMessage(content='fixture'), 'parsing_error': None}
        chat = Mock()
        chat.with_structured_output.return_value = structured
        with patch('hard_case.providers.ollama_systemone.ollama.systemone', return_value=native_response) as native:
            native_result = SystemOneProvider('tev1:4b').invoke(packet.text)
        chat_result = StructuredChatProvider(chat).invoke(packet.text)
        self.assertEqual(native.call_args.kwargs['state'], structured.invoke.call_args.args[0][1][1])
        self.assertEqual(native_result.output, chat_result.output)
        chat.with_structured_output.assert_called_once_with(HardCaseOutput, method='json_schema', include_raw=True)

    def test_compact_runner_reuses_first_success_to_extend_ten_without_changing_logical_labels(self):
        packet = self.load()
        models = ['tev1:0.8b', 'tev1:4b']
        definition = experiment_definition(packet, models, {}, systemone_context=262144)
        provider = Mock()
        from hard_case.providers.base import validate_result
        provider.invoke.return_value = validate_result(values(), {'fixture': True}, 1, 6)
        factory = Mock(return_value=provider)
        kwargs = dict(output_dir=self.directory / 'results', packet=packet, definition=definition,
                      input_profile='compact', systemone_context=262144, warmups=0,
                      provider_factory=factory, console=Console(file=io.StringIO()))
        with patch('hard_case.runner.validate_alias'):
            self.assertEqual(run_hard_case_benchmark(models, repetitions=1, **kwargs), 0)
            self.assertEqual(run_hard_case_benchmark(models, repetitions=10, **kwargs), 0)
        self.assertEqual(provider.invoke.call_count, 20)
        self.assertEqual({call.args[0] for call in factory.call_args_list}, set(models))
        for call in factory.call_args_list:
            self.assertEqual(call.kwargs, {'systemone_context': 262144})
        for call in provider.invoke.call_args_list:
            self.assertEqual(call.args[0], packet.text)


class AliasTests(unittest.TestCase):
    def test_alias_resolution_and_default_original_behavior(self):
        for model in ['tev1:0.8b', 'tev1:4b']:
            self.assertEqual(inference_model(model), model)
            actual = inference_model(model, 262144)
            self.assertEqual(actual, f"tev1-hard:{model.split(':')[1]}-ctx262144")
            self.assertEqual(model_configuration(model, 262144)['inference_model'], actual)
            self.assertEqual(model_configuration(model)['context_override'], None)
        self.assertEqual(inference_model('gemma4:e4b', 262144), 'gemma4:e4b')
        with self.assertRaises(ValueError):
            inference_model('tev1:4b', 32768)

    def test_alias_verifies_weight_parameters_template_and_maximum(self):
        original = model_info()
        alias = model_info(context=262144)
        client = Mock()
        client.show.side_effect = [original, alias]
        proof = validate_alias('tev1:4b', 262144, client)
        self.assertEqual(proof['num_ctx'], 262144)
        self.assertEqual(proof['weight_blob'], 'sha256-' + 'a' * 64)
        for broken in [model_info(blob='b' * 64, context=262144), model_info(context=2050),
                       alias.model_copy(update={'template': 'changed'}),
                       alias.model_copy(update={'parameters': 'num_ctx 262144\ntemperature 1'})]:
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                client.show.side_effect = [original, broken]
                validate_alias('tev1:4b', 262144, client)
        client.show.side_effect = ollama.ResponseError('missing', 404)
        with self.assertRaisesRegex(ValueError, 'create it'):
            validate_alias('tev1:4b', 262144, client)

    def test_provider_uses_verified_alias_and_unchanged_questions(self):
        with patch('hard_case.providers.ollama_systemone.validate_alias') as verify, \
             patch('hard_case.providers.ollama_systemone.ollama.systemone', return_value={}) as native:
            create_provider('tev1:0.8b', systemone_context=262144).invoke('fixture packet')
        verify.assert_called_once_with('tev1:0.8b', 262144)
        self.assertEqual(native.call_args.kwargs['model'], 'tev1-hard:0.8b-ctx262144')
        self.assertEqual(native.call_args.kwargs['state'], 'fixture packet')
        self.assertEqual(set(native.call_args.kwargs['questions']), set(PROBABILITY_FIELDS))

    def test_cli_defaults_and_explicit_experiment(self):
        default = parse_args(['--models', 'tev1:4b'])
        self.assertEqual((default.input_profile, default.systemone_context), ('compact', None))
        explicit = parse_args(['--models', 'tev1:4b', '--input-profile', 'compact',
                               '--systemone-context', '262144', '--repetitions', '1', '--warmups', '0'])
        self.assertEqual((explicit.input_profile, explicit.systemone_context), ('compact', 262144))
        for flags in [['--input-profile', 'unknown'], ['--input-profile', 'full'],
                      ['--systemone-context', '32768'], ['--case', '1']]:
            with self.assertRaises(SystemExit), patch('sys.stderr', new=io.StringIO()):
                parse_args(['--models', 'tev1:4b', *flags])
