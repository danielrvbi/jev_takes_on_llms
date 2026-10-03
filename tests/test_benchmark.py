from execution import POLICY
from runtime.testing import audited_result
import copy
import csv
import io
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from langchain_core.messages import AIMessage
from pydantic import ValidationError
from rich.console import Console

from benchmark.cases import CASES
from benchmark.metrics import derive, normalized, results_frame, summarize
from benchmark.plots import plot_results
from benchmark.providers.base import structured_result, token_usage, validate_result
from benchmark.providers.ollama_systemone import SystemOneProvider, map_response
from benchmark.runner import ResultStore, compatible_definition, measured_row as production_measured_row, output_lock, run_benchmark
from benchmark.schemas import DecisionOutput
from main import main, parse_args


def values():
    return {
        "requires_web_probability": 0.5,
        "is_safe_probability": 0.9,
        "route_probabilities": {"answer_directly": 0.2, "web_search": 0.2,
                                "refuse": 0.1, "ask_clarification": 0.0},
        "freshness_probabilities": {"0": 0.0, "1": 0.0, "2": 0.0,
                                    "3": 0.2, "4": 0.2, "5": 0.0},
    }


def result():
    return audited_result(validate_result(values(), {"fixture": True}, 10, 20))


def measured_row(model, case, repetition, response, latency):
    return production_measured_row(model, case, repetition, audited_result(response, model, case.message), latency)


class SchemaAndMetricsTests(unittest.TestCase):
    def test_rejects_invalid_scalars_and_shape(self):
        for bad in [-0.01, 1.01, float("nan"), float("inf"), "0.5", True, None]:
            candidate = values()
            candidate["requires_web_probability"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                DecisionOutput.model_validate(candidate)
        for modification in [lambda d: d.pop("is_safe_probability"),
                             lambda d: d.update(extra=1),
                             lambda d: d["route_probabilities"].update(other=0.1),
                             lambda d: d["freshness_probabilities"].pop("5")]:
            candidate = values()
            modification(candidate)
            with self.assertRaises(ValidationError):
                DecisionOutput.model_validate(candidate)

    def test_zero_total_rejected(self):
        for name in ["route", "freshness"]:
            candidate = values()
            candidate[f"{name}_probabilities"] = {key: 0.0 for key in candidate[f"{name}_probabilities"]}
            with self.assertRaises(ValidationError):
                DecisionOutput.model_validate(candidate)
        with self.assertRaises(ValueError):
            normalized([0, 0])

    def test_raw_values_normalization_ties_and_threshold(self):
        candidate = values()
        original = copy.deepcopy(candidate)
        output = DecisionOutput.model_validate(candidate)
        derived = derive(output)
        self.assertEqual(candidate, original)
        self.assertEqual(output.model_dump(by_alias=True), original)
        self.assertAlmostEqual(derived["route_sum_error"], -0.5)
        self.assertAlmostEqual(derived["freshness_sum_error"], -0.6)
        self.assertEqual(derived["route_answer_directly_probability"], 0.2)
        self.assertEqual(derived["derived_route"], "answer_directly")
        self.assertTrue(derived["requires_web_decision"])
        self.assertAlmostEqual(derived["expected_freshness"], 3.5)
        self.assertAlmostEqual(derived["freshness_entropy_bits"], 1)
        self.assertAlmostEqual(derived["route_entropy_bits"], -(0.4*math.log2(0.4)*2 + 0.2*math.log2(0.2)))

    def test_aggregate_statistics_failures_tokens_and_mixed_csv_booleans(self):
        rows = [measured_row("test", CASES[0], i + 1, result(), latency)
                for i, latency in enumerate([10, 20, 30])]
        rows[0]["requires_web_decision"] = "True"  # Resumed CSV row alongside newly measured rows.
        rows[1]["requires_web_decision"] = False
        rows[2]["requires_web_decision"] = True
        rows.append(measured_row("test", CASES[0], 4, validate_result({}, None, error="bad"), 100))
        frame = results_frame(rows)
        summary = summarize(frame).iloc[0]
        self.assertEqual(summary.validation_failures, 1)
        self.assertEqual(summary.successful_repetitions, 3)
        self.assertAlmostEqual(summary.requires_web_decision_consistency, 2/3)
        self.assertEqual(summary.route_consistency, 1)
        self.assertAlmostEqual(summary.latency_ms_mean, 40)
        self.assertAlmostEqual(summary.latency_ms_median, 25)
        self.assertAlmostEqual(summary.latency_ms_p95, 89.5)
        self.assertEqual(summary.input_tokens_total, 30)
        self.assertEqual(summary.input_tokens_available_repetitions, 3)
        single = summarize(frame.iloc[:1]).iloc[0]
        self.assertTrue(math.isnan(single.expected_freshness_std))
        failed = summarize(frame.iloc[-1:]).iloc[0]
        self.assertTrue(math.isnan(failed.route_consistency))
        self.assertTrue(math.isnan(failed.input_tokens_total))


class ProviderTests(unittest.TestCase):
    def test_systemone_native_mapping_ignores_reported_choices_and_score(self):
        response = {
            "model": "tev1:0.8b",
            "answers": {"requires_web": {"type": "noul", "noul": 0.5},
                        "is_safe": {"type": "noul", "noul": 0.9},
                        "route": {"type": "choice", "choice": "refuse", "confidence": 0.1,
                                  "probabilities": values()["route_probabilities"]},
                        "freshness": {"type": "score", "score": 999, "confidence": 0.1,
                                      "probabilities": values()["freshness_probabilities"]}},
            "usage": {"input_tokens": 123, "output_tokens": 4},
        }
        with patch("benchmark.providers.ollama_systemone.systemone_call", return_value=response) as native:
            measured = SystemOneProvider("tev1:0.8b", audit_directory="fixture").invoke(CASES[0].message)
        arguments = native.call_args.kwargs
        self.assertEqual(arguments["state"], CASES[0].message)
        self.assertEqual([q["type"] for q in arguments["questions"].values()], ["noul", "noul", "choice", "score"])
        self.assertNotIn("temperature", arguments)
        self.assertNotIn("seed", arguments)
        self.assertEqual(measured.input_tokens, 123)
        self.assertEqual(measured.output_tokens, 4)
        self.assertEqual(derive(measured.output)["derived_route"], "answer_directly")
        self.assertEqual(derive(measured.output)["expected_freshness"], 3.5)
        self.assertEqual(measured.raw_response, response)
        response["answers"].pop("route")
        failed = map_response(response)
        self.assertIsNone(failed.output)
        self.assertEqual(failed.input_tokens, 123)
        self.assertTrue(failed.error)

    def test_langchain_success_and_parsing_failure_preserve_evidence(self):
        raw = AIMessage(content=json.dumps(values()),
                        usage_metadata={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
                        response_metadata={"model": "test"})
        success = structured_result({"raw": raw, "parsed": DecisionOutput.model_validate(values()),
                                     "parsing_error": None})
        self.assertIsNotNone(success.output)
        self.assertEqual(success.input_tokens, 10)
        failure = structured_result({"raw": raw, "parsed": None, "parsing_error": ValueError("bad schema")})
        self.assertIsNone(failure.output)
        self.assertEqual(failure.values, values())
        self.assertEqual(failure.output_tokens, 20)
        self.assertEqual(failure.raw_response["content"], raw.content)
        row = measured_row("test", CASES[0], 1, failure, 12)
        self.assertNotIn("expected_freshness", row)
        self.assertEqual(row["requires_web_probability"], 0.5)

    def test_usage_fallbacks_and_missing_usage(self):
        for metadata in [{"token_usage": {"prompt_tokens": 2, "completion_tokens": 3}},
                         {"usage": {"prompt_tokens": 2, "completion_tokens": 3}},
                         {"prompt_eval_count": 2, "eval_count": 3}]:
            self.assertEqual(token_usage(AIMessage(content="", response_metadata=metadata)), (2, 3))
        self.assertEqual(token_usage(AIMessage(content="")), (None, None))

    def test_provider_configuration(self):
        from benchmark.providers import create_provider
        with patch("benchmark.providers.ollama_chat.ChatOllama") as local:
            provider = create_provider("gemma4:e4b", audit_directory="fixture")
            self.assertEqual(provider.model, "gemma4:e4b")
            local.assert_not_called()  # Client creation is per invoke, never per model.
        with patch.dict("os.environ", {"MISTRAL_API_KEY": "fixture"}), patch("benchmark.providers.mistral.ChatMistralAI") as hosted:
            provider = create_provider("mistral-small-latest", audit_directory="fixture")
            self.assertEqual(provider.model, "mistral-small-latest")
            hosted.assert_not_called()


class PersistenceAndRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "benchmark"
        self.definition = {"fixture": "compatible", "execution_policy": POLICY}
        self.console = Console(file=io.StringIO(), force_terminal=False)

    def tearDown(self):
        self.temp.cleanup()

    def test_history_and_atomic_raw_recovery(self):
        store = ResultStore(self.directory, self.definition)
        failure = validate_result({}, {"error": "old"}, error="failed")
        store.record(measured_row("test", CASES[0], 1, failure, 3))
        store.record(measured_row("test", CASES[0], 1, result(), 4))
        # Recover from a crash after history append but before a raw.csv replacement.
        (self.directory / "raw.csv").write_text("stale")
        recovered = ResultStore(self.directory, self.definition)
        self.assertEqual(len(recovered.latest), 1)
        self.assertEqual(len(recovered.history), 2)
        self.assertFalse(recovered.pending("test", 1, 1))
        self.assertEqual(int(recovered.latest[("test", 1, 1)]["attempt"]), 2)
        with (self.directory / "raw.csv").open() as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 1)
        with self.assertRaisesRegex(ValueError, "completed"):
            recovered.record(measured_row("test", CASES[0], 1, result(), 5))

    def test_incompatible_definition_and_orphan_results(self):
        ResultStore(self.directory, self.definition)
        with self.assertRaisesRegex(ValueError, "Incompatible"):
            ResultStore(self.directory, {"fixture": "changed"})
        (self.directory / "metadata.json").unlink()
        with self.assertRaisesRegex(ValueError, "no metadata"):
            ResultStore(self.directory, self.definition)

    def test_lock_prevents_concurrent_writer(self):
        with output_lock(self.directory):
            with self.assertRaisesRegex(ValueError, "Another benchmark"):
                with output_lock(self.directory):
                    pass

    def test_retiring_model_preserves_resume_but_configuration_changes_rejected(self):
        original = {"execution_policy": POLICY, "prompts": "unchanged", "model_configuration": {
            "gemma4:e4b": {"temperature": 0}, "gemma4:12b": {"temperature": 0}}}
        reduced = {"execution_policy": POLICY, "prompts": "unchanged", "model_configuration": {
            "gemma4:e4b": {"temperature": 0}}}
        store = ResultStore(self.directory, original)
        store.record(measured_row("gemma4:e4b", CASES[0], 1, result(), 10))
        manifest = (self.directory / "metadata.json").read_bytes()
        resumed = ResultStore(self.directory, reduced)
        self.assertFalse(resumed.pending("gemma4:e4b", 1, 1))
        self.assertEqual((self.directory / "metadata.json").read_bytes(), manifest)
        self.assertTrue(compatible_definition(original, reduced))
        for incompatible in [
            {**reduced, "prompts": "changed"},
            {**reduced, "model_configuration": {"gemma4:e4b": {"temperature": 1}}},
            {**reduced, "model_configuration": {"new-model": {"temperature": 0}}},
        ]:
            with self.assertRaisesRegex(ValueError, "Incompatible"):
                ResultStore(self.directory, incompatible)

    def run_fixture(self, models, cases, repetitions, provider_factory):
        with patch("benchmark.runner.report"), patch("benchmark.runner.measured_row", side_effect=measured_row):
            return run_benchmark(models, cases, repetitions, 2, self.root,
                                 provider_factory, self.console, self.definition,
                                 )

    def test_resume_retries_failures_once_skips_successes_extends_cases_and_models(self):
        provider = Mock()
        provider.invoke.side_effect = [result(), result(), result(),
                                       validate_result({}, None, error="failed"), result()]
        factory = Mock(return_value=provider)
        self.assertEqual(self.run_fixture(["first"], [CASES[0]], 3, factory), 1)
        self.assertEqual(provider.invoke.call_count, 5)  # Two warm-ups plus three measurements.
        provider.invoke.side_effect = None
        provider.invoke.return_value = result()
        before = provider.invoke.call_count
        self.assertEqual(self.run_fixture(["first"], [CASES[0]], 3, factory), 0)
        self.assertEqual(provider.invoke.call_count - before, 3)  # Two warm-ups, one retry.
        before = provider.invoke.call_count
        factory_calls = factory.call_count
        self.assertEqual(self.run_fixture(["first"], [CASES[0]], 3, factory), 0)
        self.assertEqual(factory.call_count, factory_calls)
        self.assertEqual(provider.invoke.call_count, before)
        self.assertEqual(self.run_fixture(["first", "second"], [CASES[0], CASES[6]], 4, factory), 0)
        store = ResultStore(self.directory, self.definition)
        self.assertEqual(len(store.latest), 16)
        self.assertEqual(len(store.history), 17)
        summary = summarize(results_frame(list(store.latest.values())), results_frame(store.history))
        self.assertEqual(summary.historical_failures.sum(), 1)
        self.assertEqual(summary.validation_failures.sum(), 0)

    def test_warmup_failures_not_recorded_and_interruption_is_resumable(self):
        provider = Mock()
        provider.invoke.side_effect = [RuntimeError("warm-up"), result(), KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            self.run_fixture(["test"], [CASES[0]], 2, lambda _, **kwargs: provider)
        store = ResultStore(self.directory, self.definition)
        self.assertEqual(len(store.history), 1)
        self.assertIn("KeyboardInterrupt", store.latest[("test", 1, 1)]["error"])
        self.assertTrue(store.pending("test", 1, 1))

    def test_three_provider_groups_extend_one_to_thirty_repetitions(self):
        groups = [
            ["tev1:0.8b", "tev1:4b"],
            ["mistral-small-latest", "mistral-large-latest"],
            ["gemma4:e4b"],
        ]
        provider = Mock()
        provider.invoke.return_value = result()
        factory = Mock(return_value=provider)
        # Other tests exercise per-attempt atomic snapshots. Batch snapshots here
        # to keep this 1,500-row regression fast; the real CSV history is retained
        # and reloaded between all six invocations.
        with patch.object(ResultStore, "save_raw"), patch("benchmark.runner.os.fsync"):
            expected_rows = 0
            for models in groups:
                before = provider.invoke.call_count
                self.assertEqual(self.run_fixture(models, CASES, 1, factory), 0)
                self.assertEqual(provider.invoke.call_count - before, len(models) * 12)
                store = ResultStore(self.directory, self.definition)
                expected_rows += len(models) * 10
                self.assertEqual(len(store.latest), expected_rows)
            original_rows = dict(store.latest)
            for models in groups:
                before = provider.invoke.call_count
                self.assertEqual(self.run_fixture(models, CASES, 30, factory), 0)
                self.assertEqual(provider.invoke.call_count - before, len(models) * 292)
                store = ResultStore(self.directory, self.definition)
                expected_rows += len(models) * 290
                self.assertEqual(len(store.latest), expected_rows)
                for key, original in original_rows.items():
                    self.assertEqual(store.latest[key], original)
            before = provider.invoke.call_count
            for models in groups:
                self.assertEqual(self.run_fixture(models, CASES, 30, factory), 0)
            self.assertEqual(provider.invoke.call_count, before)
        # Materialize and inspect the final persisted snapshot using production code.
        store = ResultStore(self.directory, self.definition)
        with (self.directory / "raw.csv").open() as handle:
            raw = list(csv.DictReader(handle))
        self.assertEqual(len(raw), 1500)
        self.assertEqual(len(store.history), 1500)
        self.assertEqual(len({(r["model"], r["case_id"], r["repetition"]) for r in raw}), 1500)
        self.assertTrue(all(int(row["attempt"]) == 1 for row in raw))

    def test_plots_with_singletons_and_all_failed_groups(self):
        rows = [measured_row("good", CASES[0], 1, result(), 1),
                measured_row("bad", CASES[0], 1, validate_result({}, None, error="bad"), 2)]
        frame = results_frame(rows)
        from matplotlib.figure import Figure

        savefig = Figure.savefig
        inspected = []

        def check_visible_models(figure, path, *args, **kwargs):
            if Path(path).stem != "route_consistency":
                inspected.append(Path(path).stem)
                for ax in figure.axes:
                    lower, upper = sorted(ax.get_ylim())
                    self.assertEqual([tick.get_text() for tick in ax.get_yticklabels()],
                                     ["good", "bad"])
                    for position in ax.get_yticks():
                        self.assertGreater(position, lower)
                        self.assertLess(position, upper)
            return savefig(figure, path, *args, **kwargs)

        with patch.object(Figure, "savefig", new=check_visible_models):
            plot_results(frame, summarize(frame), self.directory)
        self.assertEqual(set(inspected), {"requires_web", "is_safe", "freshness", "latency"})
        self.assertEqual(len(list((self.directory / "plots").glob("*.png"))), 5)


class CliTests(unittest.TestCase):
    def test_all_runs_six_models_without_gemma_12b(self):
        with patch("main.run_benchmark", return_value=0) as runner, patch("main.load_dotenv"):
            self.assertEqual(main(["--all"]), 0)
        models = runner.call_args.args[0]
        self.assertEqual(len(models), 6)
        self.assertIn('jev-1.13.0', models)
        self.assertIn("gemma4:e4b", models)
        self.assertNotIn("gemma4:12b", models)

    def test_defaults_and_case_selection(self):
        args = parse_args(["--all"])
        self.assertEqual(args.repetitions, 30)
        self.assertEqual(args.warmups, 2)
        args = parse_args(["--models", "tev1:0.8b", "gemma4:e4b", "--case", "1", "7", "--case", "10"])
        self.assertEqual(args.case_ids, [1, 7, 10])
        self.assertEqual(CASES[-1].message, "Do I need an umbrella tomorrow?")

    def test_invalid_cli_options(self):
        for arguments in [[], ["--all", "--models", "tev1:0.8b"], ["--all", "--case", "11"],
                          ["--all", "--repetitions", "0"], ["--all", "--warmups", "-1"],
                          ["--models", "unknown"], ["--models", "gemma4:12b"]]:
            with self.subTest(arguments=arguments), patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                parse_args(arguments)


if __name__ == "__main__":
    unittest.main()
