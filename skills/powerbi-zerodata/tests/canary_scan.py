"""Fail-closed canary scanner; never print matched values."""
import os
import re
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / 'fixture'
def load_canaries() -> dict[str, list[str]]:
    text = (FIXTURE / "CANARIES.md").read_text("utf-8")
    blocks: dict[str, list[str]] = {}
    for m in re.finditer(r"```(canary-[a-z-]+)\r?\n(.*?)```", text, re.S):
        blocks[m.group(1)] = [ln.strip() for ln in m.group(2).splitlines() if ln.strip()]
    return blocks


CAN = load_canaries()
INT_CANARIES = sorted(int(n) for n in CAN['canary-numbers'] if re.fullmatch(r'\d{6}', n))
DERIVED = CAN['canary-derived-numbers']

def scan_patterns(for_files: bool) -> list[re.Pattern]:
    pats: list[re.Pattern] = []
    for s in CAN["canary-strings"] + CAN["canary-blob-strings"] + CAN["canary-request-strings"] + CAN["canary-stems"]:
        pats.append(re.compile(re.escape(s), re.I))
    pats.append(re.compile(r"(?<![0-9a-fA-F])(?:7f3a[1-49]|5b9d[12])(?![0-9a-fA-F])"))
    for n in CAN["canary-numbers"]:
        core = n.lstrip("-")
        if for_files and re.fullmatch(r"\d+", core) and int(core) == INT_CANARIES[2]:
            continue  # bare value shared with tests/test_mirror.py synthetic data (see CANARIES.md)
        tail = r"(?![0-9A-Za-z])" if re.fullmatch(r"\d+", core) else r"(?![0-9])"
        pats.append(re.compile(r"(?<![0-9A-Za-z.])" + re.escape(core) + tail))
    for n in DERIVED:
        ip = n.split(".")[0]
        pats.append(re.compile(r"(?<![0-9A-Za-z.])" + re.escape(ip) + r"(?![0-9])"))
        pats.append(re.compile(re.escape(ip[:-3] + "," + ip[-3:])))
    return pats



def scan(roots, excluded=(FIXTURE,), patterns=None, read=None, walk=None):
    patterns = scan_patterns(True) if patterns is None else patterns
    read = (lambda p: p.read_bytes()) if read is None else read
    walk = os.walk if walk is None else walk
    excluded = [Path(p).absolute() for p in excluded]
    result = {'completed': True, 'files': 0, 'failures': []}

    def fail(path, reason):
        if reason in ('missing root', 'enumeration error'):
            result['completed'] = False
        result['failures'].append({'path': str(path), 'reason': reason})

    def ignored(path):
        return any(path == x or x in path.parents for x in excluded)

    for root in dict.fromkeys(Path(r).absolute() for r in roots):
        if not root.is_dir():
            fail(root, 'missing root')
            continue
        try:
            for directory, dirs, files in walk(root, onerror=lambda e: fail(e.filename, 'enumeration error')):
                base = Path(directory)
                dirs[:] = [d for d in dirs if not ignored(base / d)]
                for name in files:
                    path = base / name
                    if ignored(path):
                        continue
                    try:
                        data = read(path)
                        result['files'] += 1
                        if any(p.search(t) for p in patterns for t in
                               (data.decode('latin-1'), data.decode('utf-16-le', 'ignore'))):
                            fail(path, 'canary')
                    except OSError:
                        fail(path, 'read error')
        except OSError as error:
            fail(error.filename or root, 'enumeration error')
    result['ok'] = not result['failures']
    return result


OUTPUT_PATTERNS = scan_patterns(False)


def redact(text):
    for pattern in OUTPUT_PATTERNS:
        text = pattern.sub('<<REDACTED>>', text)
    return text
