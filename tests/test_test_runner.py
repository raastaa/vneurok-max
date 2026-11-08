"""A finalizer warning must make the CI runner fail, not just print a warning."""
from pathlib import Path
import subprocess
import sys
import unittest


class StrictRunnerTests(unittest.TestCase):
    def test_resource_finalizer_makes_runner_nonzero(self):
        code = '''
import gc, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from scripts.run_tests import main
class LeakingTest(unittest.TestCase):
    def runTest(self):
        with tempfile.TemporaryDirectory() as root:
            handle=open(Path(root)/'synthetic.txt','w')
            del handle
            gc.collect()
with patch.object(unittest.defaultTestLoader,'discover',return_value=unittest.TestSuite([LeakingTest()])):
    raise SystemExit(main([]))
'''
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parent.parent,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1)
        self.assertIn('ResourceWarning finalizer', result.stderr)


if __name__ == '__main__': unittest.main()
