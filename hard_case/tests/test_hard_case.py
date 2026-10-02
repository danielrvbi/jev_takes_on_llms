import ast
import copy
import csv
import hashlib
import io
import json
import math
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import ollama
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from rich.console import Console

from hard_case.loader import HARD_CASE_DIR, SOURCE_FILENAMES, compact_records, load_claim_packet
from hard_case.main import main, parse_args
from hard_case.metrics import RAW_COLUMNS, results_frame, summarize
from hard_case.prompts import JUDGMENTS, SYSTEM_PROMPT, systemone_questions
from hard_case.providers import MODELS, create_provider
from hard_case.providers.base import StructuredChatProvider, structured_result, token_usage, validate_result
from hard_case.providers.ollama_chat import OllamaChatProvider, reject_truncation
from hard_case.providers.ollama_systemone import SystemOneProvider, map_response
from hard_case.runner import ResultStore, experiment_definition, fingerprint, local_model_information, measured_row, output_lock, run_hard_case_benchmark
from hard_case.schemas import HardCaseOutput, PROBABILITY_FIELDS


def fixture_values():
    # Response fixtures only: these are not target judgments for the source claim.
    return {name: (index + 1) / 10 for index, name in enumerate(PROBABILITY_FIELDS)}


def successful_result():
    return validate_result(fixture_values(), {"fixture": True}, 10, 20)


class PacketFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.data = self.directory / "data"
        self.data.mkdir()
        self.contents = {}
        for name in reversed(SOURCE_FILENAMES):
            content = f"Evidence from {name}: € / é.\r\n  trailing whitespace  \r\n"
            self.contents[name] = content
            (self.data / name).write_bytes(content.encode("utf-8"))
        (self.data / "README.md").write_text("DESIGNER_ONLY_SENTINEL", encoding="utf-8")
        self.compact = self.directory / "compact"
        self.compact.mkdir()
        self.write_compact_fixture()
        self.packet = self.load_fixture()

    def write_compact_fixture(self):
        """Artificial reviewed packet/map; never regenerate the actual claim artifact."""
        documents = {name: (self.data / name).read_bytes() for name in SOURCE_FILENAMES}
        original = "\n\n".join(f"===== {name} =====\n{data.decode('utf-8')}"
                                for name, data in documents.items())
        self.compact_text = "\n\n".join(
            f"===== {name} =====\nFX{index:02} | L1-{len(data.decode('utf-8').splitlines())} | "
            f"Artificial evidence record {index}, source fixture digest {hashlib.sha256(data).hexdigest()}"
            for index, (name, data) in enumerate(documents.items()))
        (self.compact / "packet.md").write_text(self.compact_text, encoding="utf-8")
        source_map = {
            "original_source_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "compact_packet_sha256": hashlib.sha256(self.compact_text.encode("utf-8")).hexdigest(),
            "records": compact_records(self.compact_text),
            "sources": {name: {"sha256": hashlib.sha256(data).hexdigest(),
                               "line_count": len(data.decode("utf-8").splitlines())}
                        for name, data in documents.items()},
        }
        (self.compact / "source_map.json").write_text(json.dumps(source_map), encoding="utf-8")

    def load_fixture(self):
        return load_claim_packet(self.data, compact_directory=self.compact)

    def tearDown(self):
        self.temp.cleanup()

    def definition(self, models=MODELS):
        return experiment_definition(self.packet, models, local_information={})


class LoaderAndSchemaTests(PacketFixture):
    def test_deterministic_explicit_document_order_and_preserved_bytes(self):
        self.assertEqual(SOURCE_FILENAMES, (
            "policy_wording.md", "fnol.md", "customer_emails.md", "adjuster_notes.md",
            "repair_invoices.md", "police_report.md", "previous_claims.md",
            "internal_guidelines.md", "timeline.md",
        ))
        expected = "\n\n".join(f"===== {name} =====\n{self.contents[name]}" for name in SOURCE_FILENAMES)
        self.assertEqual(self.packet.original_source_sha256, hashlib.sha256(expected.encode("utf-8")).hexdigest())
        self.assertEqual(self.packet.text, self.compact_text)
        self.assertEqual(self.packet, self.load_fixture())
        self.assertEqual(self.packet.metadata()["source_filenames"], list(SOURCE_FILENAMES))
        self.assertEqual(self.packet.metadata()["character_count"], len(self.compact_text))
        self.assertEqual(self.packet.metadata()["word_count"], len(self.compact_text.split()))

    def test_readme_and_unlisted_documents_are_never_read(self):
        (self.data / "extra.md").write_text("UNLISTED_SENTINEL")
        original = Path.read_bytes
        read_names = []

        def read_allowed(path):
            self.assertNotEqual(path.name, "README.md")
            read_names.append(path.name)
            return original(path)

        with patch.object(Path, "read_bytes", new=read_allowed):
            packet = self.load_fixture()
        self.assertEqual(read_names, [*SOURCE_FILENAMES, "packet.md", "source_map.json", *SOURCE_FILENAMES])
        self.assertNotIn("DESIGNER_ONLY_SENTINEL", packet.text)
        self.assertNotIn("UNLISTED_SENTINEL", packet.text)
        with patch("hard_case.loader.SOURCE_FILENAMES", ("README.md",)):
            with self.assertRaisesRegex(AssertionError, "README"):
                self.load_fixture()

    def test_missing_and_invalid_utf8_sources_fail(self):
        source = self.data / SOURCE_FILENAMES[0]
        source.unlink()
        with self.assertRaises(FileNotFoundError):
            self.load_fixture()
        source.write_bytes(b"\xff")
        with self.assertRaises(UnicodeDecodeError):
            self.load_fixture()

    def test_schema_bounds_and_strict_shape(self):
        for name in PROBABILITY_FIELDS:
            for bad in [-0.01, 1.01, float("nan"), float("inf"), "0.5", True, None]:
                candidate = {**fixture_values(), name: bad}
                with self.subTest(field=name, value=bad), self.assertRaises(ValidationError):
                    HardCaseOutput.model_validate(candidate)
        for name in PROBABILITY_FIELDS:
            candidate = fixture_values()
            candidate.pop(name)
            with self.assertRaises(ValidationError):
                HardCaseOutput.model_validate(candidate)
        with self.assertRaises(ValidationError):
            HardCaseOutput.model_validate({**fixture_values(), "extra": 0.5})

    def test_probabilities_are_independent_and_bounds_are_inclusive(self):
        for value in [0.0, 1.0, 0.8]:
            candidate = {name: value for name in PROBABILITY_FIELDS}
            self.assertEqual(HardCaseOutput.model_validate(candidate).model_dump(), candidate)

    def test_source_change_updates_packet_hash_and_experiment_fingerprint(self):
        before = self.definition()
        with (self.data / "timeline.md").open("ab") as handle:
            handle.write(b"additional fixture evidence")
        with self.assertRaisesRegex(ValueError, "source content changed"):
            self.load_fixture()
        self.write_compact_fixture()  # Re-review only the artificial fixture, not production evidence.
        changed = self.load_fixture()
        after = experiment_definition(changed, local_information={})
        self.assertNotEqual(before["case_input"]["sha256"], after["case_input"]["sha256"])
        self.assertNotEqual(fingerprint(before), fingerprint(after))

    def test_model_inspection_uses_installed_sdk_shape_and_records_runtime_configuration(self):
        client = Mock()
        client._client.base_url = "http://localhost:11434"
        client.list.return_value = ollama.ListResponse(models=[{"model": MODELS[0], "digest": "fixture-digest"}])
        client.show.return_value = ollama.ShowResponse(
            parameters="num_ctx 2050", template="fixture template", modelfile="fixture modelfile",
            model_info={"qwen35.context_length": 262144},
        )
        http = Mock()
        http.get.return_value.json.return_value = {"version": "fixture-version"}
        with patch("ollama.Client", return_value=client), patch("httpx.Client") as http_factory:
            http_factory.return_value.__enter__.return_value = http
            observed = local_model_information([MODELS[0], MODELS[1], MODELS[-1]])
        self.assertEqual(observed[MODELS[0]]["digest"], "fixture-digest")
        self.assertEqual(observed[MODELS[0]]["parameters"], "num_ctx 2050")
        self.assertEqual(observed[MODELS[0]]["modelfile"], "fixture modelfile")
        self.assertEqual(observed[MODELS[0]]["server_version"], "fixture-version")
        self.assertEqual(observed[MODELS[1]]["inspection_status"], "not-installed")
        self.assertNotIn(MODELS[-1], observed)
        client.show.assert_called_once_with(MODELS[0])


class ProviderTests(PacketFixture):
    def test_native_six_noul_answers_map_directly(self):
        response = {
            "answers": {name: {"type": "noul", "noul": value} for name, value in fixture_values().items()},
            "usage": {"input_tokens": 100, "output_tokens": 6},
        }
        with patch("hard_case.providers.ollama_systemone.ollama.systemone", return_value=response) as native:
            result = SystemOneProvider("tev1:4b").invoke(self.packet.text)
        self.assertEqual(result.output.model_dump(), fixture_values())
        self.assertEqual(result.raw_response, response)
        self.assertEqual((result.input_tokens, result.output_tokens), (100, 6))
        self.assertEqual(native.call_args.kwargs["state"], self.packet.text)
        self.assertEqual(tuple(native.call_args.kwargs["questions"]), PROBABILITY_FIELDS)
        self.assertEqual({q["type"] for q in native.call_args.kwargs["questions"].values()}, {"noul"})
        self.assertNotIn("temperature", native.call_args.kwargs)
        self.assertNotIn("seed", native.call_args.kwargs)

    def test_native_missing_invalid_answers_preserve_raw_available_values_and_usage(self):
        response = {"answers": {name: {"noul": value} for name, value in fixture_values().items()},
                    "usage": {"input_tokens": 30}}
        response["answers"].pop(PROBABILITY_FIELDS[-1])
        result = map_response(response)
        self.assertIsNone(result.output)
        self.assertEqual(len(result.values), 5)
        self.assertEqual(result.input_tokens, 30)
        self.assertEqual(result.raw_response, response)
        for malformed in [None, [], {"answers": []}, {"answers": {}, "usage": "bad"}]:
            with self.subTest(malformed=malformed):
                result = map_response(malformed)
                self.assertIsNone(result.output)
                self.assertTrue(result.error)
                self.assertEqual(result.raw_response, malformed)
        response["answers"][PROBABILITY_FIELDS[0]]["noul"] = "0.5"
        self.assertIsNone(map_response(response).output)

    def test_structured_response_mapping_and_parsing_failure_evidence(self):
        raw = AIMessage(content=json.dumps(fixture_values()),
                        usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120})
        for parsed in [fixture_values(), HardCaseOutput.model_validate(fixture_values())]:
            result = structured_result({"raw": raw, "parsed": parsed, "parsing_error": None})
            self.assertEqual(result.output.model_dump(), fixture_values())
            self.assertEqual((result.input_tokens, result.output_tokens), (100, 20))
        for error in [ValueError("invalid structured output"), None]:
            result = structured_result({"raw": raw, "parsed": None, "parsing_error": error})
            self.assertIsNone(result.output)
            self.assertTrue(result.error)
            self.assertEqual(result.values, fixture_values())
            self.assertEqual(result.raw_response["content"], raw.content)
            self.assertEqual(result.input_tokens, 100)
        bad = {**fixture_values(), PROBABILITY_FIELDS[0]: 2.0}
        self.assertIsNone(structured_result({"raw": raw, "parsed": bad}).output)

    def test_chat_and_systemone_receive_identical_complete_packet(self):
        llm = Mock()
        raw = AIMessage(content=json.dumps(fixture_values()))
        llm.with_structured_output.return_value.invoke.return_value = {
            "raw": raw, "parsed": fixture_values(), "parsing_error": None,
        }
        chat = StructuredChatProvider(llm)
        chat.invoke(self.packet.text)
        with patch("hard_case.providers.ollama_systemone.ollama.systemone", return_value={}) as native:
            SystemOneProvider("tev1:0.8b").invoke(self.packet.text)
        messages = llm.with_structured_output.return_value.invoke.call_args.args[0]
        self.assertEqual(messages, [("system", SYSTEM_PROMPT), ("human", self.packet.text)])
        self.assertEqual(messages[1][1], native.call_args.kwargs["state"])
        llm.with_structured_output.assert_called_once_with(HardCaseOutput, method="json_schema", include_raw=True)
        self.assertEqual(tuple(JUDGMENTS), PROBABILITY_FIELDS)
        for name, definition in JUDGMENTS.items():
            self.assertIn(definition, SYSTEM_PROMPT)
            self.assertIn(definition, systemone_questions()[name]["instructions"])

    def test_usage_fallbacks_tool_calls_and_missing_usage(self):
        for metadata in [{"token_usage": {"prompt_tokens": 2, "completion_tokens": 3}},
                         {"usage": {"prompt_tokens": 2, "completion_tokens": 3}},
                         {"prompt_eval_count": 2, "eval_count": 3}]:
            self.assertEqual(token_usage(AIMessage(content="", response_metadata=metadata)), (2, 3))
        self.assertEqual(token_usage(AIMessage(content="")), (None, None))
        raw = AIMessage(content="", tool_calls=[{"id": "fixture", "name": "HardCaseOutput", "args": fixture_values()}])
        result = structured_result({"raw": raw, "parsed": None})
        self.assertEqual(result.values, fixture_values())
        self.assertIsNone(result.output)

    def test_provider_configuration_and_registry(self):
        with patch("hard_case.providers.ollama_chat.ChatOllama") as llm:
            provider = create_provider("gemma4:e4b")
            self.assertIsInstance(provider, OllamaChatProvider)
            kwargs = llm.call_args.kwargs
            self.assertEqual(kwargs["temperature"], 0)
            self.assertEqual(kwargs["keep_alive"], "2h")
            self.assertEqual(kwargs["client_kwargs"]["timeout"], 120)
            self.assertNotIn("seed", kwargs)
            self.assertNotIn("num_ctx", kwargs)
            llm.return_value.with_structured_output.assert_called_once_with(
                HardCaseOutput, method="json_schema", include_raw=True)
        with patch.dict(os.environ, {"MISTRAL_API_KEY": "fixture"}), patch(
                "hard_case.providers.mistral.ChatMistralAI") as llm:
            create_provider("mistral-small-latest")
            self.assertEqual(llm.call_args.kwargs, {
                "model": "mistral-small-latest", "temperature": 0, "max_retries": 0, "timeout": 120,
            })
            llm.return_value.with_structured_output.assert_called_once_with(
                HardCaseOutput, method="json_schema", include_raw=True)
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "MISTRAL_API_KEY"):
                create_provider("mistral-large-latest")
        self.assertIsInstance(create_provider("tev1:4b"), SystemOneProvider)
        with self.assertRaises(ValueError):
            create_provider("unknown")

    def test_http_chat_request_rejects_truncation_without_changing_packet(self):
        payload = {"model": "gemma4:e4b", "messages": [{"role": "user", "content": self.packet.text}],
                   "format": HardCaseOutput.model_json_schema(), "options": {"temperature": 0}}
        inspected = []

        def handle(request):
            inspected.append(json.loads(request.content))
            self.assertEqual(int(request.headers["Content-Length"]), len(request.content))
            return httpx.Response(200, json={})

        with httpx.Client(transport=httpx.MockTransport(handle),
                          event_hooks={"request": [reject_truncation]}) as client:
            client.post("http://localhost:11434/api/chat", json=payload)
        self.assertEqual(inspected, [{**payload, "truncate": False}])


class PersistenceAndRunnerTests(PacketFixture):
    def setUp(self):
        super().setUp()
        self.output = self.directory / "results"
        self.console = Console(file=io.StringIO(), force_terminal=False)

    def run_fixture(self, models, repetitions, provider_factory, warmups=2, definition=None):
        return run_hard_case_benchmark(
            models, repetitions, warmups, self.output, provider_factory, self.console,
            self.packet, self.definition() if definition is None else definition,
        )

    def test_resume_skips_successes_retries_failures_and_extends_models_and_repetitions(self):
        provider = Mock()
        provider.invoke.side_effect = [successful_result(), successful_result(), successful_result(),
                                       ollama.ResponseError("request body must not exceed 64 KiB", 413)]
        factory = Mock(return_value=provider)
        first = MODELS[0]
        second = MODELS[-1]
        self.assertEqual(self.run_fixture([first], 2, factory), 1)
        self.assertEqual(provider.invoke.call_count, 4)
        store = ResultStore(self.output, self.definition())
        failed = store.latest[(first, 2)]
        self.assertEqual(json.loads(failed["raw_response_json"])["status_code"], 413)
        self.assertEqual(failed["input_tokens"], "")
        provider.invoke.side_effect = None
        provider.invoke.return_value = successful_result()
        before = provider.invoke.call_count
        self.assertEqual(self.run_fixture([first], 2, factory), 0)
        self.assertEqual(provider.invoke.call_count - before, 3)
        before = provider.invoke.call_count
        factories = factory.call_count
        self.assertEqual(self.run_fixture([first], 2, factory), 0)
        self.assertEqual(provider.invoke.call_count, before)
        self.assertEqual(factory.call_count, factories)
        self.assertEqual(self.run_fixture([first, second], 3, factory), 0)
        self.assertEqual(provider.invoke.call_count - before, 8)
        self.assertTrue(all(call.args == (self.packet.text,) for call in provider.invoke.call_args_list))
        store = ResultStore(self.output, self.definition())
        self.assertEqual((len(store.latest), len(store.history)), (6, 7))
        summary = summarize(results_frame(store.latest.values()), results_frame(store.history))
        self.assertEqual(summary.historical_failures.sum(), 1)
        self.assertEqual(summary.validation_failures.sum(), 0)

    def test_incompatible_metadata_fails_before_provider_calls_or_result_rewrites(self):
        original = self.definition()
        store = ResultStore(self.output, original)
        store.record(measured_row(MODELS[0], 1, successful_result(), 10))
        files = [self.output / name for name in ["metadata.json", "raw.csv", "attempt_history.csv"]]
        snapshot = {path: path.read_bytes() for path in files}
        changes = [
            ("source", lambda d: d["case_input"].update(sha256="changed")),
            ("schema", lambda d: d["schema"].update(description="changed")),
            ("prompt", lambda d: d["prompts"].update(llm_system="changed")),
            ("questions", lambda d: d["prompts"]["systemone_questions"].clear()),
            ("model", lambda d: d["model_configuration"][MODELS[0]].update(temperature=1)),
        ]
        for name, change in changes:
            candidate = copy.deepcopy(original)
            change(candidate)
            factory = Mock()
            with self.subTest(change=name), self.assertRaisesRegex(ValueError, "Incompatible"):
                self.run_fixture([MODELS[0]], 2, factory, definition=candidate)
            factory.assert_not_called()
            self.assertEqual({path: path.read_bytes() for path in files}, snapshot)

    def test_model_selection_additions_and_subsets_preserve_metadata_provenance(self):
        first = self.definition([MODELS[0]])
        ResultStore(self.output, first)
        second = self.definition([MODELS[-1]])
        ResultStore(self.output, second)
        manifest = (self.output / "metadata.json").read_bytes()
        saved = json.loads(manifest)
        self.assertEqual(set(saved["experiment"]["model_configuration"]), {MODELS[0], MODELS[-1]})
        self.assertEqual(saved["fingerprint"], fingerprint(saved["experiment"]))
        ResultStore(self.output, first)
        self.assertEqual((self.output / "metadata.json").read_bytes(), manifest)

    def test_local_digest_or_parameters_changes_are_incompatible(self):
        original = experiment_definition(self.packet, [MODELS[0]], local_information={
            MODELS[0]: {"digest": "first", "parameters": "num_ctx 2050"},
        })
        ResultStore(self.output, original)
        for field in ["digest", "parameters"]:
            candidate = copy.deepcopy(original)
            candidate["model_configuration"][MODELS[0]]["local_model"][field] = "changed"
            with self.assertRaisesRegex(ValueError, "Incompatible"):
                ResultStore(self.output, candidate)

    def test_history_recovers_stale_raw_and_rejects_duplicate_success(self):
        store = ResultStore(self.output, self.definition())
        store.record(measured_row(MODELS[0], 1, validate_result({}, None, error="fixture failure"), 2))
        store.record(measured_row(MODELS[0], 1, successful_result(), 3))
        (self.output / "raw.csv").write_text("stale")
        recovered = ResultStore(self.output, self.definition())
        self.assertEqual((len(recovered.latest), len(recovered.history)), (1, 2))
        self.assertFalse(recovered.pending(MODELS[0], 1))
        with (self.output / "raw.csv").open() as handle:
            row = next(csv.DictReader(handle))
        self.assertEqual(row["attempt"], "2")
        with self.assertRaisesRegex(ValueError, "completed"):
            recovered.record(measured_row(MODELS[0], 1, successful_result(), 4))

    def test_missing_metadata_history_tampered_metadata_and_incomplete_rows_fail(self):
        store = ResultStore(self.output, self.definition())
        store.record(measured_row(MODELS[0], 1, successful_result(), 2))
        manifest = self.output / "metadata.json"
        original = manifest.read_bytes()
        tampered = json.loads(original)
        tampered["experiment"]["prompts"]["llm_system"] = "tampered"
        manifest.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            ResultStore(self.output, self.definition())
        manifest.unlink()
        with self.assertRaisesRegex(ValueError, "no metadata"):
            ResultStore(self.output, self.definition())
        manifest.write_bytes(original)
        history = self.output / "attempt_history.csv"
        history.unlink()
        with self.assertRaisesRegex(ValueError, "history is missing"):
            ResultStore(self.output, self.definition())
        history.write_text(",".join(RAW_COLUMNS) + "\npartial,row\n")
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            ResultStore(self.output, self.definition())

    def test_lock_blocks_concurrent_writers(self):
        with output_lock(self.output):
            with self.assertRaisesRegex(ValueError, "Another benchmark"):
                with output_lock(self.output):
                    pass

    def test_measurement_and_warmup_interruptions_are_resumable(self):
        provider = Mock()
        provider.invoke.side_effect = [RuntimeError("warm-up failed"), successful_result(), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_fixture([MODELS[0]], 1, Mock(return_value=provider))
        store = ResultStore(self.output, self.definition())
        self.assertEqual(len(store.history), 1)
        self.assertIn("KeyboardInterrupt", store.latest[(MODELS[0], 1)]["error"])
        self.assertTrue((self.output / "summary.csv").exists())
        # A warm-up interruption in an empty directory must not leave an orphan raw snapshot.
        self.output = self.directory / "warmup_interrupt"
        provider.invoke.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_fixture([MODELS[0]], 1, Mock(return_value=provider))
        store = ResultStore(self.output, self.definition())
        self.assertEqual(len(store.history), 0)
        self.assertTrue(store.pending(MODELS[0], 1))

    def test_initialization_errors_are_recorded_without_warmups(self):
        factory = Mock(side_effect=ValueError("missing fixture credentials"))
        self.assertEqual(self.run_fixture([MODELS[-1]], 2, factory), 2)
        factory.assert_called_once_with(MODELS[-1])
        store = ResultStore(self.output, self.definition())
        self.assertEqual(len(store.history), 2)
        self.assertTrue(all("Provider initialization failed" in row["error"] for row in store.history))

    def test_summary_uses_valid_probabilities_latest_latency_and_available_tokens(self):
        rows = [measured_row(MODELS[0], 1, successful_result(), 10),
                measured_row(MODELS[0], 2, successful_result(), 20),
                measured_row(MODELS[0], 3, validate_result({PROBABILITY_FIELDS[0]: 2.0}, None, error="bad"), 100)]
        rows[1][PROBABILITY_FIELDS[0]] = 0.3
        rows[0]["validation_success"] = "True"
        summary = summarize(results_frame(rows)).iloc[0]
        self.assertEqual(summary.successful_repetitions, 2)
        self.assertEqual(summary.validation_failures, 1)
        self.assertAlmostEqual(summary.requires_clarification_mean, 0.2)
        self.assertAlmostEqual(summary.requires_clarification_std, math.sqrt(0.02))
        self.assertEqual(summary.requires_clarification_min, 0.1)
        self.assertEqual(summary.requires_clarification_max, 0.3)
        self.assertEqual(summary.latency_ms_p50, 20)
        self.assertEqual(summary.latency_ms_p95, 92)
        self.assertEqual(summary.input_tokens_mean, 10)
        self.assertEqual(summary.input_tokens_available_repetitions, 2)
        singleton = summarize(results_frame(rows[:1])).iloc[0]
        self.assertTrue(math.isnan(singleton.requires_clarification_std))
        failed = summarize(results_frame(rows[-1:])).iloc[0]
        self.assertTrue(math.isnan(failed.requires_clarification_mean))
        self.assertTrue(math.isnan(failed.input_tokens_mean))


class CliAndIsolationTests(unittest.TestCase):
    def test_package_does_not_mix_hard_case_into_implicit_discovery(self):
        suite = unittest.TestLoader().discover(str(HARD_CASE_DIR), top_level_dir=str(HARD_CASE_DIR.parent))
        self.assertEqual(suite.countTestCases(), 0)

    def test_defaults_and_selection(self):
        args = parse_args(["--all"])
        self.assertEqual((args.repetitions, args.warmups), (30, 2))
        self.assertEqual(args.output_dir, HARD_CASE_DIR / "results")
        with patch("hard_case.main.run_hard_case_benchmark", return_value=0) as run:
            self.assertEqual(main(["--all"]), 0)
        self.assertEqual(run.call_args.args[0], list(MODELS))
        with patch("hard_case.main.run_hard_case_benchmark", return_value=0) as run:
            main(["--models", "tev1:4b", "tev1:4b"])
        self.assertEqual(run.call_args.args[0], ["tev1:4b"])

    def test_rejects_regular_case_flags_and_invalid_options(self):
        for arguments in [[], ["--all", "--models", "tev1:4b"], ["--all", "--case", "1"],
                          ["--all", "--hard-case"], ["--all", "--repetitions", "0"],
                          ["--all", "--warmups", "-1"], ["--models", "unknown"]]:
            with self.subTest(arguments=arguments), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                parse_args(arguments)

    def test_exit_statuses(self):
        for result, code in [(0, 0), (1, 1)]:
            with patch("hard_case.main.run_hard_case_benchmark", return_value=result):
                self.assertEqual(main(["--all"]), code)
        for error, code in [(KeyboardInterrupt(), 130), (ValueError("fixture"), 1)]:
            with patch("hard_case.main.run_hard_case_benchmark", side_effect=error), patch(
                    "hard_case.main.Console", return_value=Console(file=io.StringIO())):
                self.assertEqual(main(["--all"]), code)

    def test_no_imports_from_original_benchmark_or_test_suite(self):
        for path in HARD_CASE_DIR.rglob("*.py"):
            for node in ast.walk(ast.parse(path.read_text())):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else (
                    [node.module or ""] if isinstance(node, ast.ImportFrom) else []
                )
                for name in names:
                    self.assertNotIn(name.split(".")[0], {"benchmark", "tests", "main"}, str(path))


if __name__ == "__main__":
    unittest.main()
