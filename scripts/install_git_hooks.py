"""Install verified PBZ hooks; --chain explicitly preserves and chains host hooks."""
from __future__ import annotations

import argparse
import hashlib
import os
import stat
import subprocess
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(ROOT, "scripts", "git-hooks")
NAMES = ("pre-commit", "pre-push")
# Exact shipped content, not a marker, establishes ownership. Add reviewed old
# release digests here when the shipped hooks change.
KNOWN = {"8e473fe563a3328033763cdf5b5670a557746765aa8a21fae3eea62e1a790523"}


def wrapper(name: str) -> bytes:
    if name == "pre-commit":
        return b"""#!/bin/sh
# PBZ owned chain v1
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
"$here/pre-commit.pbz-host" "$@" || exit $?
exec "$here/pre-commit.pbz" "$@"
"""
    return (f'''#!/bin/sh
# PBZ owned chain v1
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
input=$(mktemp) || exit 1
trap 'rm -f "$input"' EXIT HUP INT TERM
cat > "$input" || exit 1
"$here/{name}.pbz-host" "$@" < "$input"
status=$?
[ "$status" -eq 0 ] || exit "$status"
"$here/{name}.pbz" "$@" < "$input"
exit $?
''').encode("utf-8")


def install(root: str, source: str, chain: bool = False) -> list[str]:
    shared = subprocess.run(["git", "config", "--get", "core.hooksPath"], cwd=root,
                            capture_output=True, text=True)
    if shared.returncode == 0 and not chain:
        raise ValueError("core.hooksPath requires explicit --chain, even for an empty directory")
    git_path = subprocess.check_output(["git", "rev-parse", "--git-path", "hooks"], cwd=root, text=True).strip()
    hooks = Path(git_path if os.path.isabs(git_path) else os.path.join(root, git_path))
    sources = {name: (Path(source) / name).read_bytes() for name in NAMES}
    known = KNOWN | {hashlib.sha256(body).hexdigest() for body in sources.values()}
    writes = []

    def read(path):
        if path.is_symlink():
            raise ValueError(f"refusing linked hook: {path}")
        return path.read_bytes() if path.exists() else None

    # Plan both hooks and every sidecar before writing any destination.
    for name in NAMES:
        dest = hooks / name
        current = read(dest)
        host, owned = hooks / (name + ".pbz-host"), hooks / (name + ".pbz")
        host_body, owned_body = read(host), read(owned)
        if current == wrapper(name):
            if host_body is None or owned_body is None or hashlib.sha256(owned_body).hexdigest() not in known:
                raise ValueError(f"unverified PBZ chain: {dest}")
            writes.append((owned, sources[name]))
        elif current is None or hashlib.sha256(current).hexdigest() in known:
            if host_body is not None or owned_body is not None:
                raise ValueError(f"foreign hook sidecar: {dest}")
            writes.append((dest, sources[name]))
        elif not chain:
            raise ValueError(f"foreign hook: {dest}; use --chain to preserve it")
        else:
            if host_body is not None or owned_body is not None:
                raise ValueError(f"foreign hook sidecar: {dest}")
            writes.extend(((host, current), (owned, sources[name]), (dest, wrapper(name))))
    hooks.mkdir(parents=True, exist_ok=True)
    for dest, body in writes:
        if not dest.exists() or dest.read_bytes() != body:
            dest.write_bytes(body)
        dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    return [str(hooks / name) for name in NAMES]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chain", action="store_true", help="opt in to shared directories and preserve host hooks")
    args = parser.parse_args()
    try:
        for path in install(ROOT, SOURCE, args.chain):
            print(f"installed {path}")
    except ValueError as error:
        parser.exit(1, str(error) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
