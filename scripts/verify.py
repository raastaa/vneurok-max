"""Run synthetic local checks and write a reproducible, secret-free test journal.

No real MAX request is made. The output does not prove live or Docker readiness.
"""
import argparse
from datetime import datetime, timezone
import json
import gc
from pathlib import Path
import platform
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class Journal(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.started = {}

    def startTest(self, test):
        self.started[test.id()] = time.monotonic()
        super().startTest(test)

    def record(self, test, status):
        self.records.append({'case': test.id(), 'status': status,
                             'seconds': round(time.monotonic()-self.started[test.id()], 4)})

    def addSuccess(self, test):
        self.record(test, 'PASS'); super().addSuccess(test)

    def addFailure(self, test, err):
        self.record(test, 'FAIL'); super().addFailure(test, err)

    def addError(self, test, err):
        self.record(test, 'ERROR'); super().addError(test, err)

    def addSkip(self, test, reason):
        self.record(test, 'SKIP'); super().addSkip(test, reason)


def run():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, help='Local JSON result path')
    args = parser.parse_args()
    import warnings
    warnings.simplefilter('error', ResourceWarning)
    unclosed=[]
    original_hook=sys.unraisablehook
    def track_unraisable(event):
        if event.exc_type and issubclass(event.exc_type,ResourceWarning):unclosed.append('unclosed_resource')
        original_hook(event)
    sys.unraisablehook=track_unraisable
    suite = unittest.defaultTestLoader.discover(str(ROOT/'tests'))
    start = datetime.now(timezone.utc).isoformat(timespec='seconds')
    result = unittest.TextTestRunner(verbosity=1, resultclass=Journal).run(suite)
    suite=None
    gc.collect();gc.collect()
    sys.unraisablehook=original_hook
    checks = []
    for cmd in (['node','--check','web/app.js'], ['node','tests/test_web_guided.js'],
                [sys.executable,'-m','compileall','-q','vneurok','tests','scripts'],
                ['git','diff','--check']):
        try:
            completed = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=120)
            status = 'PASS' if completed.returncode == 0 else 'FAIL'
            print(' '.join(cmd[1:] if cmd[0] == sys.executable else cmd), status)
            if status == 'FAIL': print(completed.stdout, completed.stderr)
            checks.append({'command': ['python3',*cmd[1:]] if cmd[0] == sys.executable else cmd,
                           'status': status, 'exit_code': completed.returncode})
        except (OSError, subprocess.TimeoutExpired) as error:
            checks.append({'command': cmd, 'status': 'BLOCKED', 'reason': type(error).__name__})
    report = {'started_at_utc': start, 'finished_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'runtime': {'python': platform.python_version(), 'platform': platform.system(), 'machine': platform.machine()},
              'scope': 'Synthetic temporary local databases and mocked MAX transport; no production or real school data.',
              'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'skipped': len(result.skipped), 'unclosed_resource_finalizers':len(unclosed), 'cases': result.records, 'checks': checks,
              'not_verified': ['Live MAX', 'Chosen HTTPS host and webhook', 'Current Docker image', 'Real school pilot and target timings', 'Publication and contest submission']}
    target = Path(args.output)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    return 0 if result.wasSuccessful() and not unclosed and all(x['status']=='PASS' for x in checks) else 1


if __name__ == '__main__':
    raise SystemExit(run())
