from jev_bench.storage.io import output_lock
from jev_bench.runtime.execution import POLICY
from tests.fixtures.audit import audited_result
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from jev_bench.suites.benchmark.cases import CASES
from jev_bench.providers.suites.benchmark.base import validate_result
from jev_bench.run.benchmark import ResultStore, measured_row
from jev_bench.run_evaluations.reporting import build_report, find_results, load_results, main
from tests.fixtures.benchmark import result


class EvaluationReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.directory = self.root / "smoke"
        self.store = ResultStore(self.directory, {"execution_policy": POLICY, "prompts": {"fixture": "original prompt"}})
        failed = validate_result({}, {"content": "invalid structured output"}, error="fixture parsing failure")
        self.store.record(measured_row("tev1:0.8b", CASES[0], 1, failed, 10))
        self.store.record(measured_row("tev1:0.8b", CASES[0], 1, result(), 20))
        self.store.record(measured_row("gemma4:e4b", CASES[6], 1, result(), 30))

    def tearDown(self):
        self.temp.cleanup()

    def test_report_is_self_contained_and_preserves_failure_history(self):
        frame, history, metadata = load_results(self.directory)
        report = build_report(self.directory, frame, history, metadata)
        self.assertIn("Selected latest rows: 2; valid: 2; failed: 0", report)
        self.assertIn("historical failures: 1", report)
        self.assertIn("fixture parsing failure", report)
        self.assertIn(CASES[0].message, report)
        self.assertIn(CASES[6].message, report)
        self.assertIn("original prompt", report)
        self.assertIn("requires_web_probability", report)
        self.assertIn("### Every latest repetition", report)
        self.assertIn("no gold labels, accuracy scores", report)
        self.assertIn("Sample std", report)
        self.assertIn("NA", report)  # Singleton standard deviations are undefined.
        self.assertNotIn("### Original provider responses for all attempts", report)

    def test_filters_limit_history_and_report_groups(self):
        frame, history, metadata = load_results(self.directory, ["tev1:0.8b"], [1])
        self.assertEqual(len(frame), 1)
        self.assertEqual(len(history), 2)
        report = build_report(self.directory, frame, history, metadata, include_raw=True)
        self.assertIn("## Model tev1:0.8b — case 1", report)
        self.assertNotIn("## Model gemma4:e4b", report)
        self.assertIn("### Original provider responses for all attempts", report)
        self.assertIn('"fixture": true', report)
        with self.assertRaisesRegex(ValueError, "Models have no saved rows"):
            load_results(self.directory, ["missing"])
        with self.assertRaisesRegex(ValueError, "Cases have no saved rows"):
            load_results(self.directory, ["tev1:0.8b"], [7])

    def test_missing_history_and_metadata_are_explicit(self):
        (self.directory / "attempt_history.csv").unlink()
        (self.directory / "metadata.json").unlink()
        frame, history, metadata = load_results(self.directory)
        report = build_report(self.directory, frame, history, metadata)
        self.assertIn("WARNING: metadata.json is missing", report)
        self.assertIn("WARNING: attempt_history.csv is missing", report)
        self.assertEqual(len(frame), 2)

    def test_directory_discovery_does_not_combine_experiments(self):
        self.assertEqual(find_results(self.root), self.directory)
        other = self.root / "other"
        ResultStore(other, {"execution_policy": POLICY, "fixture": "other"})
        with self.assertRaisesRegex(ValueError, "Multiple result directories"):
            find_results(self.root)
        self.assertEqual(find_results(self.directory), self.directory)

    def test_duplicate_rows_are_rejected(self):
        path = self.directory / "raw.csv"
        lines = path.read_text().splitlines(keepends=True)
        path.write_text("".join(lines + [lines[-1]]))
        with self.assertRaisesRegex(ValueError, "duplicate repetition"):
            load_results(self.directory)

    def test_snapshot_refuses_concurrent_measurements(self):
        with output_lock(self.directory):
            with self.assertRaisesRegex(ValueError, "currently writing"):
                load_results(self.directory)

    def test_cli_does_not_change_measurements_or_trust_stale_summary(self):
        (self.directory / "summary.csv").write_text("a stale summary that must never be read")
        originals = {path.name: path.read_bytes() for path in self.directory.iterdir() if path.is_file()}
        output = self.directory / "evaluation.md"
        with redirect_stderr(io.StringIO()), patch("ollama.systemone", side_effect=AssertionError("No model calls")):
            self.assertEqual(main(["--input-dir", str(self.directory), "--output", str(output)]), 0)
        self.assertIn("Selected latest rows: 2", output.read_text())
        for name, contents in originals.items():
            self.assertEqual((self.directory / name).read_bytes(), contents)
        with redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(main(["--input-dir", str(self.directory), "--case", "1"]), 0)
            self.assertIn("## Model tev1:0.8b — case 1", stdout.getvalue())
        with redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(["--input-dir", str(self.directory), "--output", str(self.directory / "raw.csv")]), 1)
            self.assertIn("must not overwrite", stderr.getvalue())

    def test_stale_raw_snapshot_is_labelled(self):
        raw_path = self.directory / "raw.csv"
        previous = raw_path.read_bytes()
        self.store.record(measured_row("gemma4:e4b", CASES[6], 2, result(), 40))
        self.store.record(measured_row("gemma4:e4b", CASES[6], 3,
                                      validate_result({}, None, error="failed"), 50))
        failure_snapshot = raw_path.read_bytes()
        self.store.record(measured_row("gemma4:e4b", CASES[6], 3, result(), 60))
        raw_path.write_bytes(failure_snapshot)
        frame, history, metadata = load_results(self.directory)
        report = build_report(self.directory, frame, history, metadata)
        self.assertIn("WARNING: raw.csv and history do not agree", report)
        self.assertNotEqual(previous, failure_snapshot)


if __name__ == "__main__":
    unittest.main()
