"""Tool-assisted scrub of agent memory notes: data values out, editing knowledge kept."""
from __future__ import annotations

import datetime as dt
import os
import re

from . import policy as policy_mod, tmdl
from .classify import Classifier
from .common import PbzError, commit_bytes, read_text

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


def run(ws, project, memory_dir: str, apply: bool = False, show: str | None = None, human: bool = False) -> dict:
    if apply and not human:
        raise PbzError("HUMAN_ONLY", "memscrub --apply is human-only")
    clf = None
    if project is not None:
        try:
            clf = Classifier(tmdl.load_model(os.path.join(project.model_dir, "definition")), ws.private,
                             policy_mod.load(ws))
        except OSError:
            clf = None
    files = sorted(f for f in os.listdir(memory_dir) if f.lower().endswith(".md"))
    report, shown = [], None
    planned = []
    for f in files:
        p = os.path.join(memory_dir, f)
        text, bom = read_text(p)
        new, n = scrub_text(text, clf)
        if show and f == show:
            shown = new
        if n:
            report.append({"file": f, "replacements": n})
            if apply:
                with open(p, "rb") as raw:
                    planned.append((p, new, bom, raw.read(), f))
    if apply and planned:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = os.path.join(ws.private, "memory-backup", stamp)
        n = 0
        while os.path.exists(backup):
            n += 1
            backup = os.path.join(ws.private, "memory-backup", f"{stamp}_{n}")
        ops: list[tuple] = []
        for p, new, bom, raw, name in planned:
            ops.append(("write_bytes", os.path.join(backup, name), raw))
            ops.append(("write", p, new, bom))
        commit_bytes(ops, private=True)
    out = {"verb": "memscrub", "dir": memory_dir, "files_changed": report,
           "applied": apply, "name_detection": "index" if clf and clf.key else "none (run pbz index first)"}
    if shown is not None:
        out["scrubbed_text"] = shown
    return out
