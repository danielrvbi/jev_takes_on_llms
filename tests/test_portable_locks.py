"""Exercise reader/writer exclusion across processes on every supported OS."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jev_bench.storage.io import output_lock
from jev_bench.run_evaluations.reporting import saved_snapshot


class PortableLockTests(unittest.TestCase):
    def contender(self, directory, *, reader=False):
        script = ("from pathlib import Path; import sys; "
                  "from jev_bench.storage.io import output_lock; "
                  "from jev_bench.run_evaluations.reporting import saved_snapshot; "
                  f"lock = {'saved_snapshot' if reader else 'output_lock'}(Path(sys.argv[1])); "
                  "lock.__enter__(); lock.__exit__(None, None, None)")
        return subprocess.run([sys.executable, '-c', script, str(directory)], capture_output=True, text=True)

    def test_writer_excludes_readers_and_writers_then_releases(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with output_lock(path):
                for reader in (False, True):
                    process = self.contender(path, reader=reader)
                    self.assertNotEqual(process.returncode, 0)
                    self.assertIn('ValueError', process.stderr)
            self.assertEqual(self.contender(path).returncode, 0)

    def test_readers_can_coexist_and_exclude_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            with output_lock(path):
                pass
            with saved_snapshot(path):
                self.assertEqual(self.contender(path, reader=True).returncode, 0)
                self.assertNotEqual(self.contender(path).returncode, 0)
