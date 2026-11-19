"""Fresh-process suite bootstrap: preserve argv and record unittest skip reasons."""
import json
from pathlib import Path
import runpy
import sys
import unittest


def main():
    original = unittest.TestResult.addSkip
    def add_skip(result, test, reason):
        print('PBZ_SKIP ' + json.dumps({'test': test.id(), 'reason': reason}), flush=True)
        return original(result, test, reason)
    unittest.TestResult.addSkip = add_skip
    sys.argv = sys.argv[1:]
    sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
    runpy.run_path(sys.argv[0], run_name='__main__')


if __name__ == '__main__':
    main()
