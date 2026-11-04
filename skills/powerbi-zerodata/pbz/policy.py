"""Workspace policy (.pbz/policy.json). Agents may only tighten it; loosening is `pbz admin policy`."""
from __future__ import annotations

import copy
import json
import os

from .common import Workspace

DEFAULT = {
    "version": 1,
    "synthetic": False,
    "disclosure": "shape",          # shape: booleans + fixed buckets; strict: booleans only
    "deny": [],                      # extra globs (relative to the workspace root) agents may not read
    "allow": [],                     # exceptions to deny rules (user only)
    "deny_scripts": [],              # script file names agents may not run
    "sensitive_hosts": [],           # data-source hosts agents may not contact
    "category_columns": [],          # "Table[Column]" forced readable (user only)
    "sensitive_columns": [],         # "Table[Column]" forced hidden
    "reveal_columns": [],            # extra group-by columns whose keys checks may print (user only)
    "category_max": 50,              # max distinct values for a text column to count as a category
    "budgets": {"session_checks": 300, "per_target": 60, "constants_per_target": 3},
}


def load(ws: Workspace) -> dict:
    pol = copy.deepcopy(DEFAULT)
    try:
        with open(ws.policy_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k, v in data.items():
                if k == "budgets" and isinstance(v, dict):
                    pol["budgets"].update(v)
                else:
                    pol[k] = v
    except (OSError, ValueError):
        pass
    return pol


def save(ws: Workspace, pol: dict) -> None:
    os.makedirs(ws.pbz_dir, exist_ok=True)
    tmp = ws.policy_path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(pol, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, ws.policy_path)


def tighten(ws: Workspace, deny=(), deny_scripts=(), sensitive_hosts=()) -> dict:
    """Add entries (never removes). Returns what was added."""
    pol = load(ws)
    added = {"deny": [], "deny_scripts": [], "sensitive_hosts": []}
    for key, items in (("deny", deny), ("deny_scripts", deny_scripts), ("sensitive_hosts", sensitive_hosts)):
        for it in items:
            if it not in pol[key]:
                pol[key].append(it)
                added[key].append(it)
    if any(added.values()) or not os.path.exists(ws.policy_path):
        save(ws, pol)
    return added
