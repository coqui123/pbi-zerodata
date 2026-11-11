"""Workspace inventory: data-bearing files, leaky scripts, embedded payloads. Tightens policy only."""
from __future__ import annotations

import os
import re

from . import guard, policy as policy_mod
from .common import MIRROR_DIR

TEXT_EXT = (".md", ".txt", ".log", ".html", ".htm", ".json", ".yml", ".yaml", ".xml", ".tsv")
SCRIPT_EXT = (".py", ".ps1", ".psm1", ".js", ".mjs", ".ts", ".bat", ".cmd", ".sh")
NUMERIC = re.compile(r"[$€£]\s?\d|\b\d+(?:\.\d+)?\s?%|\b\d{1,3}(?:,\d{3})+\b|\b\d+(?:\.\d+)?\s?[KMB]\b|\b\d{3,}(?:\.\d+)?\b")
EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
DATA_DIR_SHARE = 0.5


def _rel(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace("\\", "/")


def scan(ws, apply: bool = True) -> dict:
    root = ws.root
    deny, scripts, reasons = [], [], []
    for cur, dirs, files in os.walk(root):
        rcur = _rel(root, cur)
        low = rcur.lower()
        dirs[:] = [d for d in dirs if d.lower() not in (MIRROR_DIR, ".pbz", ".git", "node_modules", "__pycache__",
                                                        ".venv", "venv", ".mangocode")]
        if any(part in ("node_modules", "site-packages", "dist", "build") for part in low.split("/")):
            dirs[:] = []
            continue
        if re.search(r"\.(semanticmodel|report)$", low) or re.search(r"\.bak-", low):
            dirs[:] = []
            continue
        data_files = sum(1 for f in files if guard.DATA_EXT.search("." + f.rsplit(".", 1)[-1]) or
                         re.search(r"\.(png|jpg|jpeg|pdf|docx|pptx)$", f.lower()))
        if rcur != "." and files and data_files / max(len(files), 1) >= DATA_DIR_SHARE:
            deny.append(f"{rcur}/**")
            reasons.append((f"{rcur}/**", f"{data_files}/{len(files)} files are data, images or documents"))
            dirs[:] = []
            continue
        for f in files:
            p = os.path.join(cur, f)
            rel = _rel(root, p)
            fl = f.lower()
            if fl.endswith(SCRIPT_EXT):
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        text = fh.read(2_000_000)
                except OSError:
                    continue
                if guard.script_bad(text, policy_mod.load(ws)):
                    scripts.append(f)
                    reasons.append((f, "script reads data files, real definitions or a Power BI engine"))
                continue
            if fl.endswith(TEXT_EXT) and not guard.classify_path(guard.norm(p)):
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        text = fh.read(2_000_000)
                except OSError:
                    continue
                kb = max(len(text) / 1024, 1)
                density = len(NUMERIC.findall(text)) / kb
                emails = len(EMAIL.findall(text))
                if density >= 3 or emails >= 3 or (len(text) > 50_000 and density >= 1):
                    deny.append(rel)
                    reasons.append((rel, f"looks data-heavy ({density:.1f} figures/KB, {emails} emails)"))
    added = policy_mod.tighten(ws, deny=deny, deny_scripts=sorted(set(scripts))) if apply else {"deny": deny, "deny_scripts": scripts}
    return {"verb": "scan", "added": added, "reasons": [{"path": p, "why": w} for p, w in reasons]}
