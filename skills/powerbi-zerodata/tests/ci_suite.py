"""Fresh-process suite bootstrap: preserve argv and record unittest skip reasons."""
import json
import os
from pathlib import Path
import runpy
import sys
import unittest


def main():
    sidecar = os.environ.get('PBZ_SKIP_SIDECAR')
    if sidecar:
        open(sidecar, 'a', encoding='utf-8').close()
    original = unittest.TestResult.addSkip
    def add_skip(result, test, reason):
        print('PBZ_SKIP ' + json.dumps({'test': test.id(), 'reason': reason}), flush=True)
        if sidecar:
            with open(sidecar, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps({'suite': os.environ.get('PBZ_SKIP_SUITE', ''),
                                         'test': test.id(), 'reason': reason}) + '\n')
        return original(result, test, reason)
    unittest.TestResult.addSkip = add_skip
    sys.argv = sys.argv[1:]
    sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
    runpy.run_path(sys.argv[0], run_name='__main__')


if __name__ == '__main__':
    main()
