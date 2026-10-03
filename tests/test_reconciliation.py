from jev_bench.storage.io import output_lock
from jev_bench.runtime.execution import POLICY
from tests.fixtures.audit import audited_result
import copy
import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from jev_bench.suites.benchmark.cases import CASES
from jev_bench.suites.benchmark.metrics import RAW_COLUMNS, results_frame, summarize
from jev_bench.providers.suites.benchmark.base import validate_result
from jev_bench.run.benchmark import ResultStore, experiment_definition, measured_row
from jev_bench.run_evaluations.reconcile import (
    MISTRAL_MODELS, SUITE_MODELS, definition_fingerprint, load_source, reconcile,
)
from jev_bench.run_evaluations.reporting import load_results
from tests.fixtures.benchmark import result, measured_row


class ReconciliationTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("jev_bench.run.benchmark.experiment_execution", return_value={"execution_policy": POLICY, "runtime": "offline-fixture"})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch("jev_bench.run.benchmark.model_identity", side_effect=lambda m: {"model":m,"digest":"fixture"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.suite, self.mistral, self.output = [self.root / name for name in ["suite", "mistral", "combined"]]
        self.definition = experiment_definition()
        previous = copy.deepcopy(self.definition)
        previous["model_configuration"]["gemma4:12b"] = dict(previous["model_configuration"]["gemma4:e4b"])
        suite = ResultStore(self.suite, previous)
        separate = ResultStore(self.mistral, self.definition)
        for model in SUITE_MODELS:
            suite.record(measured_row(model, CASES[0], 1, result(), 20))
        failure = validate_result({}, {"failure": "keep this evidence"}, error="fixture schema failure")
        for store in [suite, separate]:
            store.record(measured_row("mistral-small-latest", CASES[6], 1, failure, 5))
        old = result()
        old.raw_response = {"run": "earlier suite Mistral"}
        old = audited_result(old)
        suite.record(measured_row("mistral-large-latest", CASES[0], 1, old, 6))
        separate.record(measured_row("mistral-small-latest", CASES[0], 1, failure, 7))
        new = result()
        new.raw_response = {"run": "authoritative Mistral"}
        new = audited_result(new)
        separate.record(measured_row("mistral-small-latest", CASES[0], 1, new, 30))
        separate.record(measured_row("mistral-large-latest", CASES[0], 1, new, 40))
        self.before = self.source_bytes()

    def tearDown(self):
        self.temp.cleanup()

    def source_bytes(self):
        return {str(path): path.read_bytes() for directory in [self.suite, self.mistral]
                for path in directory.rglob("*") if path.is_file()}

    def run_reconcile(self):
        # Plot generation is covered by the existing plot tests and the real-data run.
        with patch("jev_bench.run_evaluations.reconcile.plot_results"), patch("ollama.systemone", side_effect=AssertionError("No calls")), \
                patch("jev_bench.providers.suites.benchmark.create_provider", side_effect=AssertionError("No providers")):
            return reconcile(self.suite, self.mistral, self.output)

    def update_definition(self, directory, change):
        path = directory / "metadata.json"
        metadata = json.loads(path.read_text())
        change(metadata["experiment"])
        metadata["fingerprint"] = definition_fingerprint(metadata["experiment"])
        path.write_text(json.dumps(metadata))

    def test_authoritative_sources_preserve_cells_attempts_and_failed_rows(self):
        metadata, summary = self.run_reconcile()
        self.assertEqual(self.before, self.source_bytes())
        frame, history, loaded_metadata = load_results(self.output)
        self.assertEqual(len(frame), 6)
        self.assertEqual(len(history), 7)
        self.assertEqual(int((~frame.validation_success).sum()), 1)
        self.assertEqual(int((~history.validation_success).sum()), 2)
        self.assertFalse(frame.duplicated(["model", "case_id", "repetition"]).any())
        self.assertEqual(metadata, loaded_metadata)
        sources = [load_source(directory) for directory in [self.suite, self.mistral]]
        expected_raw = [row for source, models in zip(sources, [SUITE_MODELS, MISTRAL_MODELS])
                        for row in source["raw"] if row["model"] in models]
        expected_history = [row for source, models in zip(sources, [SUITE_MODELS, MISTRAL_MODELS])
                            for row in source["history"] if row["model"] in models]
        for filename, expected in [("raw.csv", expected_raw), ("attempt_history.csv", expected_history)]:
            with (self.output / filename).open(newline="") as handle:
                self.assertEqual(list(csv.DictReader(handle)), expected)
        for source, models in zip(sources, [SUITE_MODELS, MISTRAL_MODELS]):
            source_frame = results_frame([r for r in source["raw"] if r["model"] in models])
            source_history = results_frame([r for r in source["history"] if r["model"] in models])
            expected = summarize(source_frame, source_history)
            actual = summary[summary.model.isin(models)]
            pd.testing.assert_frame_equal(expected.reset_index(drop=True), actual.reset_index(drop=True), check_dtype=False)
        mistral_raw = frame[frame.model == "mistral-large-latest"].iloc[0].raw_response_json
        self.assertIn("authoritative Mistral", mistral_raw)
        self.assertNotIn("earlier suite", mistral_raw)

    def test_metadata_and_report_document_selection_and_unequal_coverage(self):
        metadata, _ = self.run_reconcile()
        provenance = metadata["reconciliation"]
        self.assertEqual(provenance["sources"][0]["excluded_latest_rows"], 2)
        self.assertEqual(provenance["model_sources"]["mistral-small-latest"], str(self.mistral.resolve()))
        self.assertIn("gemma4:12b", provenance["sources"][0]["original_metadata"]["experiment"]["model_configuration"])
        self.assertNotIn("gemma4:12b", metadata["experiment"]["model_configuration"])
        for source in provenance["sources"]:
            for filename, fingerprint in source["file_sha256"].items():
                data = (Path(source["path"]) / filename).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), fingerprint)
        report = (self.output / "evaluation.md").read_text()
        self.assertIn("evaluation-only", report)
        self.assertIn("Sample counts are unequal", report)
        self.assertIn("Earlier Mistral samples in suite are excluded", report)
        self.assertIn("fixture schema failure", report)
        self.assertIn("Min repetitions/case", report)
        self.assertNotIn(".combined.", report)

    def test_evaluation_only_cannot_resume_even_with_matching_fingerprint(self):
        metadata, _ = self.run_reconcile()
        before = {p.name: p.read_bytes() for p in self.output.iterdir() if p.is_file()}
        with self.assertRaisesRegex(ValueError, "evaluation-only"):
            ResultStore(self.output, metadata["experiment"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.output.iterdir() if p.is_file()})

    def test_duplicate_raw_repetition_rejected(self):
        path = self.suite / "raw.csv"
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        with path.open("a", newline="") as handle:
            csv.DictWriter(handle, RAW_COLUMNS).writerow(rows[0])
        with self.assertRaisesRegex(ValueError, "Duplicate repetition"):
            self.run_reconcile()
        self.assertFalse(self.output.exists())

    def test_methodology_and_included_model_setting_changes_rejected(self):
        self.update_definition(self.mistral, lambda d: d["prompts"].update(llm_system="different"))
        with self.assertRaisesRegex(ValueError, "methodology"):
            self.run_reconcile()
        (self.mistral / "metadata.json").write_bytes(self.before[str(self.mistral / "metadata.json")])
        self.update_definition(self.mistral, lambda d: d["model_configuration"]["mistral-large-latest"].update(temperature=1))
        with self.assertRaisesRegex(ValueError, "configurations differ"):
            self.run_reconcile()

    def test_tampered_metadata_rejected(self):
        path = self.mistral / "metadata.json"
        metadata = json.loads(path.read_text())
        metadata["experiment"]["format_version"] = 99
        path.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            self.run_reconcile()

    def test_invalid_attempt_sequence_and_stale_raw_rejected(self):
        path = self.mistral / "attempt_history.csv"
        with path.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["attempt"] = "2"
        with path.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, RAW_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaisesRegex(ValueError, "attempt sequence"):
            self.run_reconcile()
        path.write_bytes(self.before[str(path)])
        raw = self.mistral / "raw.csv"
        with raw.open(newline="") as handle:
            rows = list(csv.DictReader(handle))
        rows[0]["latency_ms"] = "123"
        with raw.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, RAW_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)
        with self.assertRaisesRegex(ValueError, "disagree"):
            self.run_reconcile()

    def test_destination_cannot_overwrite_sources_or_existing_results(self):
        for output in [self.suite, self.suite / "combined", self.root]:
            with self.assertRaisesRegex(ValueError, "separate"):
                reconcile(self.suite, self.mistral, output)
        self.output.mkdir()
        (self.output / "keep.txt").write_text("preserve")
        with self.assertRaisesRegex(ValueError, "not empty"):
            self.run_reconcile()
        self.assertEqual((self.output / "keep.txt").read_text(), "preserve")

    def test_failure_during_plotting_does_not_publish_or_leave_staging(self):
        with patch("jev_bench.run_evaluations.reconcile.plot_results", side_effect=RuntimeError("plot failed")):
            with self.assertRaisesRegex(RuntimeError, "plot failed"):
                reconcile(self.suite, self.mistral, self.output)
        self.assertFalse(self.output.exists())
        self.assertEqual(list(self.root.glob(".combined.*")), [])
        self.assertEqual(self.before, self.source_bytes())

    def test_empty_destination_allowed(self):
        self.output.mkdir()
        self.run_reconcile()
        self.assertTrue((self.output / "raw.csv").exists())

    def test_source_lock_prevents_reading_active_run(self):
        with output_lock(self.suite):
            with self.assertRaisesRegex(ValueError, "currently writing"):
                self.run_reconcile()


if __name__ == "__main__":
    unittest.main()
