"""Fast non-mutating hook checks against the index, including staged-only failures."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    root = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip()
    with tempfile.TemporaryDirectory(prefix='pbz-index-') as temp:
        subprocess.run(['git', 'checkout-index', '--all', '--prefix=' + temp + os.sep], cwd=root, check=True)
        tree = Path(temp)
        for path in tree.rglob('*.py'):
            compile(path.read_bytes(), str(path.relative_to(tree)), 'exec')
        skill = tree / 'skills/powerbi-zerodata'
        # Export uses the caller's index; checks must own their Git repositories.
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        for argv in (['tests/run_offline.py', '--check'], ['tests/test_ci_registration.py'], ['tests/test_plan_pure.py']):
            subprocess.run([sys.executable, *argv], cwd=skill, env=env, check=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
