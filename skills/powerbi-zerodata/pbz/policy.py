"""Workspace policy (.pbz/policy.json). Agents may only tighten it; loosening is `pbz admin policy`."""
from __future__ import annotations

import copy
import json
import os

from .common import PbzError, Workspace, pbz_state_path, read_checked, write_text, _reparse_link

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
    "memory_dirs": [],               # extra folders memscrub may read (user only; canonicalized)
    "category_max": 50,              # max distinct values for a text column to count as a category
    "budgets": {"session_checks": 300, "per_target": 60, "constants_per_target": 3},
}

MALFORMED = "policy is malformed; fix or remove .pbz/policy.json by hand in your own terminal"
_LIST_KEYS = (
    "deny", "allow", "deny_scripts", "sensitive_hosts", "category_columns",
    "sensitive_columns", "reveal_columns", "memory_dirs",
)


def security_values_ok(data) -> bool:
    """Closed security fields. Unknown keys are allowed. Absent keys are not checked."""
    if not isinstance(data, dict):
        return False
    if "version" in data and (type(data["version"]) is not int or data["version"] != 1):
        return False
    if "synthetic" in data and type(data["synthetic"]) is not bool:
        return False
    if "disclosure" in data and data["disclosure"] not in ("shape", "strict"):
        return False
    if "literals" in data and data["literals"] != "strict":
        return False
    for key in _LIST_KEYS:
        if key in data:
            value = data[key]
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                return False
    if "budgets" in data:
        budgets = data["budgets"]
        if (not isinstance(budgets, dict)
                or any(type(value) is not int or value < 0 for value in budgets.values())):
            return False
    if "category_max" in data and (type(data["category_max"]) is not int or data["category_max"] < 0):
        return False
    return True


def _guard_policy_paths(ws: Workspace) -> str:
    """Refuse a junction at `.pbz` or the policy leaf before any read or mkdir.

    The trust root is the workspace root. Using `.pbz` itself would treat a junction
    there as a trusted boundary.
    """
    return pbz_state_path(ws, "policy.json")


def _policy_missing(ws: Workspace) -> bool:
    """True only when `.pbz` or `policy.json` is absent. A link is present and unusable."""
    pbz = os.path.join(ws.root, ".pbz")
    try:
        os.lstat(pbz)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if _reparse_link(pbz):
        return False
    try:
        os.lstat(os.path.join(pbz, "policy.json"))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def load(ws: Workspace) -> dict:
    if _policy_missing(ws):
        return copy.deepcopy(DEFAULT)
    path = _guard_policy_paths(ws)
    try:
        raw = read_checked(path, root=ws.root)
    except PbzError:
        raise
    except OSError:
        raise PbzError("REFUSED", MALFORMED) from None
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        raise PbzError("REFUSED", MALFORMED) from None
    if not security_values_ok(data):
        raise PbzError("REFUSED", MALFORMED)
    pol = copy.deepcopy(DEFAULT)
    for key, value in data.items():
        if key == "budgets" and isinstance(value, dict):
            pol["budgets"].update(value)
        else:
            pol[key] = value
    return pol


def save(ws: Workspace, pol: dict) -> None:
    path = _guard_policy_paths(ws)
    body = json.dumps(pol, indent=2, ensure_ascii=False) + "\n"
    write_text(path, body, root=ws.root)


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
