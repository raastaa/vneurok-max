"""Fail tests on ResourceWarning, including warnings from resource finalizers."""
import argparse
import gc
from pathlib import Path
import sys
import unittest
import warnings

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run local unittest suite with strict resource cleanup checks.')
    parser.add_argument('--pattern', default='test*.py')
    args = parser.parse_args(argv)
    unclosed = []
    original_hook = sys.unraisablehook
    def track_unraisable(event):
        if event.exc_type and issubclass(event.exc_type, ResourceWarning):
            unclosed.append(event.err_msg or 'An unclosed resource was finalized.')
        original_hook(event)
    sys.unraisablehook = track_unraisable
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', ResourceWarning)
            suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=args.pattern)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
            suite = None
            gc.collect()
            gc.collect()
        if unclosed:
            print(f'FAILED: {len(unclosed)} ResourceWarning finalizer(s).', file=sys.stderr)
        return 0 if result.wasSuccessful() and not unclosed else 1
    finally:
        sys.unraisablehook = original_hook


if __name__ == '__main__':
    raise SystemExit(main())
