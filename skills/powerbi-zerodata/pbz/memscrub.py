"""Tool-assisted scrub of agent memory notes: data values out, editing knowledge kept."""
from __future__ import annotations

import datetime as dt
import os
import re
import stat

from . import policy as policy_mod, tmdl
from .classify import Classifier
from . import guard
from .common import PbzError, _reparse_link, commit_bytes

RULES = [
    (re.compile(r"\b\d+(?:[.,]\d+)?\s?(?:→|->)\s?\d+(?:[.,]\d+)?[KMB%]?"), "[changed value]"),
    (re.compile(r"[$€£]\s?\d[\d,]*(?:\.\d+)?\s?[KMB]?"), "[amount]"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?%"), "[share]"),
    (re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b"), "[count]"),
    (re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s?[KMB]\b"), "[count]"),
    (re.compile(r"(?i)(?<![\w.])\d+(?:\.\d+)?\s+(rows|projects|hours|hrs|people|resources|employees|records|exceptions|items)\b"), r"[count] \1"),
    # bare figures: 4+ digit integers / decimals with 4+ integer digits, except years, ports, ids in paths
    (re.compile(r"(?<![\w.:/#v-])(?!(?:19|20)\d\d(?![\d.]))\d{4,}(?:\.\d+)?(?![\w.])"), "[number]"),
    (re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
]


def scrub_text(text: str, clf: Classifier | None) -> tuple[str, int]:
    n = 0
    for rx, rep in RULES:
        text, k = rx.subn(rep, text)
        n += k
    if clf and clf.key:
        spans = [s for s in clf.free_text(text) if s[2] != "num"]
        for s, e, _lab in reversed(spans):
            text = text[:s] + "[name]" + text[e:]
            n += 1
    return text, n


def _canonical(path: str) -> str:
    return guard.norm(os.path.realpath(path))


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def approved_memory_roots(ws) -> list[str]:
    """Canonical Claude and Codex memory folders, plus policy memory_dirs.

    A path is approved only when its resolved directory is one of these roots. A substring
    such as `/.codex/memories/` is not enough.
    """
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, ".codex", "memories")]
    projects = os.path.join(home, ".claude", "projects")
    if os.path.isdir(projects):
        try:
            names = os.listdir(projects)
        except OSError:
            names = []
        for name in names:
            candidates.append(os.path.join(projects, name, "memory"))
    pol = policy_mod.load(ws) if ws is not None else {}
    extra = pol.get("memory_dirs", [])
    if extra is None:
        extra = []
    if not isinstance(extra, list) or any(not isinstance(item, str) for item in extra):
        raise PbzError("UNSAFE_PATH", "memory_dirs must be a list of directories")
    for item in extra:
        if item.strip():
            candidates.append(os.path.expanduser(item.strip()))
    roots = []
    for raw in candidates:
        if _reparse_link(raw) or not os.path.isdir(raw):
            continue
        try:
            root = _canonical(raw)
        except OSError:
            continue
        if root not in roots:
            roots.append(root)
    return roots


def _links_within(path: str, boundary: str) -> None:
    """Refuse a junction or any resolution that leaves the approved memory boundary.

    Components above the boundary are not checked. Every component strictly below it is:
    an intermediate directory junction is refused before a listing or a read.
    """
    root_abs = os.path.abspath(boundary)
    try:
        root_real = os.path.realpath(root_abs)
    except OSError:
        raise PbzError("UNSAFE_PATH", "memory folder is outside approved roots") from None
    current = os.path.abspath(path)
    for _ in range(64):
        # 8.3 and long spellings of the boundary are the same directory. A string
        # compare would treat the short path as a child and then reject its parent.
        try:
            here = os.path.realpath(current)
        except OSError:
            here = current
        below = os.path.normcase(here) != os.path.normcase(root_real)
        if below and _reparse_link(current):
            raise PbzError("UNSAFE_PATH", "memory folder is outside approved roots")
        try:
            real = os.path.realpath(current)
            if os.path.normcase(os.path.commonpath([real, root_real])) != os.path.normcase(root_real):
                raise PbzError("UNSAFE_PATH", "memory folder is outside approved roots")
        except ValueError:
            raise PbzError("UNSAFE_PATH", "memory folder is outside approved roots") from None
        if not below:
            return
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    raise PbzError("UNSAFE_PATH", "memory folder is outside approved roots")


def _approved_dir(memory_dir: str, roots: list[str]) -> str:
    if not isinstance(memory_dir, str) or not memory_dir or _reparse_link(memory_dir) or not os.path.isdir(memory_dir):
        raise PbzError("UNSAFE_PATH", "memory folder is outside approved roots")
    try:
        canon = _canonical(memory_dir)
    except OSError:
        raise PbzError("UNSAFE_PATH", "memory folder is outside approved roots") from None
    if not any(_within(canon, root) for root in roots):
        raise PbzError("UNSAFE_PATH", "memory folder is outside approved roots")
    boundary = next(root for root in roots if _within(canon, root))
    _links_within(memory_dir, boundary)
    return canon


def _stat_approved(path: str, directory: str) -> tuple:
    """Identity of one regular, unlinked file inside `directory`. Nothing is read."""
    if os.path.basename(path) != os.path.basename(os.path.normpath(path)):
        raise PbzError("UNSAFE_PATH", "memory file is outside approved roots")
    _links_within(path, directory)
    if _reparse_link(path):
        raise PbzError("UNSAFE_PATH", "memory file is outside approved roots")
    try:
        info = os.lstat(path)
    except OSError:
        raise PbzError("UNSAFE_PATH", "memory file is outside approved roots") from None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise PbzError("UNSAFE_PATH", "memory file is outside approved roots")
    try:
        canon = _canonical(path)
    except OSError:
        raise PbzError("UNSAFE_PATH", "memory file is outside approved roots") from None
    if not _within(canon, directory):
        raise PbzError("UNSAFE_PATH", "memory file is outside approved roots")
    return (info.st_dev, info.st_ino, info.st_nlink)


def _approved_file(path: str, directory: str) -> None:
    _stat_approved(path, directory)


def _read_checked(path: str, directory: str, ident: tuple) -> tuple[str, bytes]:
    """Open `path` once and read that handle only when it is still `ident`."""
    _links_within(path, directory)
    try:
        handle = open(path, "rb")
    except OSError:
        raise PbzError("UNSAFE_PATH", "memory file is outside approved roots") from None
    try:
        try:
            info = os.fstat(handle.fileno())
        except OSError:
            raise PbzError("UNSAFE_PATH", "memory file is outside approved roots") from None
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PbzError("UNSAFE_PATH", "memory file is outside approved roots")
        if (info.st_dev, info.st_ino, info.st_nlink) != ident:
            raise PbzError("UNSAFE_PATH", "memory file is outside approved roots")
        if not _within(_canonical(path), directory):
            raise PbzError("UNSAFE_PATH", "memory file is outside approved roots")
        data = handle.read()
    finally:
        handle.close()
    bom = b""
    if data.startswith(b"\xef\xbb\xbf"):
        bom, data = data[:3], data[3:]
    return data.decode("utf-8"), bom


def run(ws, project, memory_dir: str, apply: bool = False, show: str | None = None, human: bool = False) -> dict:
    if show and (os.path.basename(show) != show or show in (".", "..")):
        raise PbzError("UNSAFE_PATH", "show names one file in the memory folder")
    directory = _approved_dir(memory_dir, approved_memory_roots(ws))
    if show:
        _stat_approved(os.path.join(memory_dir, show), directory)
    try:
        names = os.listdir(memory_dir)
    except OSError:
        raise PbzError("UNSAFE_PATH", "memory folder is outside approved roots") from None
    files = sorted(f for f in names if f.lower().endswith(".md") and os.path.basename(f) == f)
    idents = []
    for f in files:
        p = os.path.join(memory_dir, f)
        idents.append((p, f, _stat_approved(p, directory)))
    # Path checks finish before the human gate, so a link is UNSAFE_PATH and not HUMAN_ONLY.
    if apply and not human:
        raise PbzError("HUMAN_ONLY", "memscrub --apply is human-only")
    if show and not human:
        raise PbzError("HUMAN_ONLY", "memscrub --show is human-only")
    clf = None
    if project is not None:
        try:
            clf = Classifier(tmdl.load_model(os.path.join(project.model_dir, "definition")), ws.private,
                             policy_mod.load(ws))
        except OSError:
            clf = None
    report, shown = [], None
    planned = []
    for p, f, ident in idents:
        text, bom = _read_checked(p, directory, ident)
        new, n = scrub_text(text, clf)
        if show and f == show:
            shown = new
        if n:
            report.append({"file": f, "replacements": n})
            if apply:
                planned.append((p, new, bom, text.encode("utf-8") if not bom else bom + text.encode("utf-8"), f))
    if apply and planned:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = os.path.join(ws.private, "memory-backup", stamp)
        n = 0
        while os.path.exists(backup):
            n += 1
            backup = os.path.join(ws.private, "memory-backup", f"{stamp}_{n}")
        ops: list[tuple] = []
        roots = {}
        for p, new, bom, raw, name in planned:
            backup_path = os.path.join(backup, name)
            roots[p] = memory_dir
            roots[backup_path] = ws.private
            ops.append(("write_bytes", backup_path, raw))
            ops.append(("write", p, new, bom))
        commit_bytes(ops, private=True, roots=roots)
    out = {"verb": "memscrub", "dir": memory_dir, "files_changed": report,
           "applied": apply, "name_detection": "index" if clf and clf.key else "none (run pbz index first)"}
    if shown is not None and human:
        out["scrubbed_text"] = shown
    return out
