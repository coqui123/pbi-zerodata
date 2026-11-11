"""pbz guard: PreToolUse / SessionStart hook shared by Claude Code and Codex.

Keeps company data out of the agent's context in Power BI workspaces:
- real *.SemanticModel / *.Report definition folders (the agent edits the pbz-workspace mirror),
- data files, Desktop caches and app data, the pbz private store,
- engine / REST / data-reading commands and scripts,
- screenshots, UI automation and Power BI service pages.

Contract: stdin = hook JSON, stdout = deny JSON (or nothing to allow), exit code 0.
Invalid JSON, a non-object event, or a failure while resolving the workspace exits
non-zero and still prints a deny decision. Deny reasons never echo tool input.
Stdlib only; run as `python -I -S guard.py --agent <name>`.
"""
from __future__ import annotations

import base64
import datetime as _dt
import fnmatch
import json
import os
import re
import stat
import sys

POLICY_REL = os.path.join(".pbz", "policy.json")
MIRROR_DIR = "pbz-workspace"
MAX_WALK_ENTRIES = 6000
MAX_SCRIPT_BYTES = 2_000_000


def _long_existing(path: str) -> str:
    """Expand 8.3 names on the longest existing prefix. A missing tail stays literal.

    `RUNNER~1` is a profile directory, not `.pbz`. `PBZ~1` that does not exist stays
    `PBZ~1`, so a short name that cannot be checked is still refused. Junctions are
    not resolved; only the 8.3 spelling of this path changes.
    """
    if os.name != "nt" or not re.search(r"~\d", path):
        return path
    raw = os.path.normpath(path)
    probe = raw
    tail: list[str] = []
    while True:
        try:
            exists = os.path.exists(probe)
        except OSError:
            return path
        if exists:
            break
        parent, name = os.path.split(probe)
        if not name or parent == probe:
            return path
        tail.append(name)
        probe = parent
    if not re.search(r"~\d", probe):
        return path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetLongPathNameW(probe, buf, len(buf))
    except (OSError, AttributeError):
        return path
    if n <= 0 or n >= len(buf):
        return path
    out = buf.value
    if out.startswith("\\\\?\\"):
        out = out[4:]
    for name in reversed(tail):
        out = os.path.join(out, name)
    return out


def _expand_cmd_shorts(text: str) -> str:
    """Rewrite absolute paths in a command so an existing `RUNNER~1` is its long name."""
    if "~" not in text:
        return text

    def repl(match: re.Match) -> str:
        token = match.group(0)
        if not re.search(r"~\d", token):
            return token
        return _long_existing(token)

    return re.sub(
        r'(?i)(?<![A-Za-z0-9])[a-z]:[\\/](?:[^\\/:*?"<>|\r\n]+[\\/])*(?:[^\\/:*?"<>|\r\n]*)',
        repl,
        text,
    )


HOME = os.path.normcase(_long_existing(os.path.normpath(os.path.expanduser("~")))).replace("\\", "/")
LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
APPDATA = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRIVATE_ROOT = os.path.join(os.path.expanduser("~"), ".pbz-private")  # pbz private store (DPAPI, HMACs, backups)

# Content access denied everywhere, workspace or not.
GLOBAL_DENY_DIRS = [
    os.path.join(os.path.expanduser("~"), "Microsoft", "Power BI Desktop Store App"),
    os.path.join(LOCALAPPDATA, "Microsoft", "Power BI Desktop"),
    os.path.join(LOCALAPPDATA, "pbz"),
    os.path.join(APPDATA, "Measure Killer"),  # activity logs, save files, exports, deletion backups
    PRIVATE_ROOT,
]
# .measurekiller analysis files hold user emails and column sizes.
GLOBAL_DENY_PATTERNS = [r"(^|/)\.pbi(/|$)", r"\.abf(\.stale)?$", r"\.measurekiller$"]

# Content access denied inside zero-data workspaces (globs relative to the workspace root).
DEFAULT_WS_DENY = [
    "**/*.xlsx", "**/*.xls", "**/*.xlsm", "**/*.xlsb", "**/*.csv", "**/*.tsv", "**/*.parquet",
    "**/*.pbix", "**/*.pbit", "**/*.pbids", "**/*.zip", "**/*.tgz", "**/*.gz", "**/*.7z", "**/*.rar",
    "**/*.dax", "**/*.trc", "**/*.xel", "**/*.png", "**/*.jpg", "**/*.jpeg", "**/*.gif", "**/*.bmp",
    "**/*.webp", "**/*.pdf", "**/*.docx", "**/*.pptx", "**/*.msg", "**/*.eml",
    "**/.env", "**/*.bak-*", "**/*.bak-*/**", "**/DAXQueries/**", "**/TMDLScripts/**",
]
SAFE_DEFINITION_FILES = {"definition.pbir", "definition.pbism", ".platform", "item.metadata.json", "item.config.json"}

# Engine / REST access: blocked everywhere except synthetic (dev / fixture) trees.
ENGINE_PATTERNS = [
    r"(?i)adomd", r"(?i)microsoft\.analysisservices", r"(?i)invoke-ascmd", r"(?i)invoke-processas",
    r"(?i)executequeries", r"\bEVALUATE\b", r"(?i)msmdsrv", r"(?i)analysisservicesworkspace",
    r"(?i)api\.powerbi\.com", r"(?i)analysis\.windows\.net", r"(?i)powerbi://", r"(?i)\bxmla\b",
    r"(?i)pyadomd", r"(?i)\bsempy\b", r"(?i)semantic-link", r"(?i)powerbi-modeling-mcp", r"(?i)pbz-engine",
    r"(?i)powerbi-desktop(\.cmd|\.ps1)?['\"]?\s+screenshot",
    r"(?i)powerbi-report-author(\.cmd|\.ps1)?['\"]?\s+(preview\b|preview-filters)",
    r"(?i)export-data", r"(?i)dax\s?studio", r"(?i)\$expand=fields", r"(?i)/odata/",
]
# Generic data reading: blocked inside zero-data workspaces.
DATA_READ_PATTERNS = [
    r"(?i)read_csv", r"(?i)read_excel", r"(?i)read_parquet", r"(?i)openpyxl", r"(?i)\bxlrd\b",
    r"(?i)import-csv", r"(?i)import-excel", r"(?i)get-clipboard", r"(?i)pyperclip", r"(?i)win32clipboard",
    r"(?i)csv\.(reader|dictreader)",
]
DEFINITION_REF = re.compile(r"(?i)\.(semanticmodel|report)\b")
DATA_EXT = re.compile(
    r"(?i)\.(xlsx|xls|xlsm|xlsb|csv|tsv|parquet|pbix|pbit|pbids|abf|zip|tgz|7z|rar|dax|trc|xel|measurekiller)\b"
)

# Listing and launching aliases are not exempt: a denied path in the pipeline must stick.
SAFE_PATH_VERBS = {
    # Listing and process launchers are not exempt: a denied path in `dir` or
    # `get-childitem` must still deny, or a later pipeline stage never sees it.
    "rm", "del", "erase", "rmdir", "rd", "remove-item",
    "ri", "mkdir", "md", "new-item", "ni",
    "split-path", "join-path",
    "cd", "chdir", "set-location", "sl", "pushd", "push-location", "popd", "pop-location",
}
CONTENT_SEARCH_VERBS = {"rg", "grep", "egrep", "fgrep", "findstr", "select-string", "sls", "ag", "ack"}
CD_VERBS = {"cd", "chdir", "set-location", "sl", "pushd", "push-location"}
POP_VERBS = {"popd", "pop-location"}
PUSH_VERBS = {"pushd", "push-location"}
GREP_TYPE_EXT = {"py": "py", "js": "js", "ts": "ts", "cs": "cs", "ps": "ps1", "powershell": "ps1",
                 "json": "json", "md": "md", "markdown": "md", "yaml": "yaml", "toml": "toml"}

GLOBAL_SENSITIVE_HOSTS = [
    "app.powerbi.com", "api.powerbi.com", "analysis.windows.net", "pbidedicated.windows.net",
    "app.fabric.microsoft.com", "api.fabric.microsoft.com", "onelake.dfs.fabric.microsoft.com",
]
WS_SENSITIVE_HOSTS = ["sharepoint.com", "powerbi.com", "fabric.microsoft.com"]

BROWSER_READ_TOOLS = re.compile(
    r"^mcp__Claude_Browser__(computer|get_page_text|read_page|find|javascript_tool|read_network_requests|"
    r"read_console_messages|browser_batch|form_input)$")
UI_TOOLS = re.compile(r"(?i)^mcp__(computer-use|computer_use|claude-in-chrome)__|computer.?use|screenshot|screen_?capture")
PBI_MCP = re.compile(r"(?i)^mcp__[^_]*(powerbi|power-bi|power_bi|fabric)")
FILEISH_TOOLS = re.compile(r"(?i)(read|view|open|file|image|filesystem|lsp|notebook|attach|upload)")
_MUTATING_TOOL = re.compile(
    r"(?i)(write|edit|patch|delete|remove|move|rename|unlink|trash|upload|save|create|copy|replace)"
)
NON_FILE_TOOLS = {"Agent", "Task", "Skill", "TodoWrite", "AskUserQuestion", "ExitPlanMode", "EnterPlanMode",
                  "ToolSearch", "WebSearch", "SendMessage", "ScheduleWakeup"}

MEMORY_DATA_PATTERNS = [
    r"[$€£]\s?\d", r"\b\d+(\.\d+)?\s?%", r"\b\d{1,3}(,\d{3})+\b", r"\b\d+(\.\d+)?\s?[KMB]\b",
    r"\b\d+\s?(→|->)\s?\d+\b", r"(?i)\b\d+\s+(rows|projects|hours|hrs|people|resources|employees|records)\b",
    r"[\w.+-]+@[\w-]+\.[\w.-]+",
]

REASONS = {
    "definition": "Real Power BI definition files are off-limits in zero-data mode. Run `pbz pull` and edit the sanitized mirror in pbz-workspace\\, then `pbz push`.",
    "data_file": "Data-bearing file blocked in zero-data mode. For an external reference, use pbz consent request; ask the user to verify no sensitive data before approving the exact file. Read only through the consent broker. Model/report definitions, caches and private stores remain denied.",
    "global": "Power BI caches, Desktop and Measure Killer app data and the pbz private store are never readable by agents.",
    "engine": "Direct engine, REST or data access is blocked. Use pbz verbs (health, check, probe, snapshot, smoke, describe, values).",
    "admin": "`pbz admin` is for the user in their own terminal only.",
    "script": "Script blocked: it reads data files, real definition folders or a Power BI engine. Use pbz verbs or work on pbz-workspace\\.",
    "search": "Content search over a folder holding data or real definition files is blocked. Search pbz-workspace\\ or a specific clean file.",
    "ui": "Screenshots, UI automation and browser page reads are disabled in zero-data mode. The user verifies visuals; use `pbz handoff` for the checklist.",
    "web": "Power BI service and data-source hosts are blocked.",
    "pbi_mcp": "Power BI MCP servers return model data and are blocked. Use pbz.",
    "policy": "This edit would loosen .pbz/policy.json. Only the user can do that (`pbz admin policy` in a terminal).",
    "consumers": ".pbz/consumers.json is managed by pbz: add consumers with `pbz consumers add`; only the user removes one (`pbz admin consumer-remove <id>`).",
    "selfprotect": "Guard, skill, hook and agent-rule files are read-only from a zero-data workspace session. Edit them from a session in the pbi-zerodata repo.",
    "memory": "Memory notes must not hold data values (amounts, percentages, counts, emails). Rewrite qualitatively; keep names of tables, measures, pages and visuals.",
    "error": "pbz guard could not evaluate this call safely, so it is blocked. Retry with a simpler command or use pbz.",
}


# ---------------------------------------------------------------- paths

def norm(p: str, base: str | None = None) -> str:
    """Normalize Windows / Git Bash / relative paths: absolute, lowercase, forward slashes."""
    p = str(p).strip().strip("'\"")
    m = re.match(r"^/([a-zA-Z])(/|$)", p)
    if m:
        p = m.group(1) + ":/" + p[3:]
    p = os.path.expandvars(os.path.expanduser(p))
    if base and not os.path.isabs(p):
        p = os.path.join(base, p)
    p = os.path.normpath(p)
    if "~" in p:
        p = _long_existing(p)
    return os.path.normcase(p).replace("\\", "/")


def glob_to_regex(glob: str) -> re.Pattern:
    g = glob.replace("\\", "/").lower()
    out, i = "", 0
    while i < len(g):
        if g.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif g.startswith("**", i):
            out, i = out + ".*", i + 2
        elif g[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif g[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(g[i]), i + 1
    return re.compile("^" + out + "$")


def security_values_ok(data) -> bool:
    """Same closed table as policy.security_values_ok. Unknown keys are kept. Absent keys are not checked."""
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
    for key in ("deny", "allow", "deny_scripts", "sensitive_hosts", "category_columns",
                "sensitive_columns", "reveal_columns", "memory_dirs"):
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


def _lockdown() -> dict:
    """Fail closed: strict disclosure and deny-all. Not an empty or synthetic policy."""
    return {"_pbz_lockdown": True, "disclosure": "strict", "deny": ["**/*"], "allow": [], "synthetic": False}


_LINK_TAGS = (0xA000000C, 0xA0000003)


def _is_link(path: str) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if os.path.islink(path):
        return True
    return getattr(info, "st_reparse_tag", None) in _LINK_TAGS


def _policy_absent(d: str) -> bool:
    """True only when `.pbz` is missing, or a real `.pbz` directory has no policy file.

    Links and inspection failures are present/unusable (not absent).
    """
    pbz = os.path.join(d, ".pbz")
    try:
        info = os.lstat(pbz)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if (os.path.islink(pbz) or getattr(info, "st_reparse_tag", None) in _LINK_TAGS
            or not stat.S_ISDIR(info.st_mode)):
        return False
    try:
        os.lstat(os.path.join(pbz, "policy.json"))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def read_policy_file(d: str) -> dict | None:
    """None only when the policy file is absent. A present file that cannot be trusted is lockdown."""
    pbz = os.path.join(d, ".pbz")
    try:
        pbz_info = os.lstat(pbz)
    except FileNotFoundError:
        return None
    except OSError:
        return _lockdown()
    if (os.path.islink(pbz) or getattr(pbz_info, "st_reparse_tag", None) in _LINK_TAGS
            or not stat.S_ISDIR(pbz_info.st_mode)):
        return _lockdown()
    path = os.path.join(d, POLICY_REL)
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        return _lockdown()
    try:
        if _is_link(path):
            return _lockdown()
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            return _lockdown()
        with open(path, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_dev != before.st_dev or info.st_ino != before.st_ino):
                return _lockdown()
            after = os.lstat(path)
            # Re-check the .pbz directory boundary after open — a mid-flight junction swap
            # can make leaf identity agree on an external policy.json.
            try:
                again = os.lstat(pbz)
            except OSError:
                return _lockdown()
            if (after.st_dev != before.st_dev or after.st_ino != before.st_ino or _is_link(path)
                    or os.path.islink(pbz) or getattr(again, "st_reparse_tag", None) in _LINK_TAGS
                    or not stat.S_ISDIR(again.st_mode)):
                return _lockdown()
            raw = handle.read()
        if raw.startswith(b"\xef\xbb\xbf"):
            raw = raw[3:]
        data = json.loads(raw.decode("utf-8"))
        if not security_values_ok(data):
            return _lockdown()
        return data
    except Exception:
        return _lockdown()


def is_workspace_root(d: str) -> bool:
    if os.path.basename(d) == MIRROR_DIR:
        return False
    if read_policy_file(d) is not None:
        return True
    try:
        with os.scandir(d) as it:
            for e in it:
                n = e.name.lower()
                if n.endswith(".pbip") and e.is_file():
                    return True
                if (n.endswith(".semanticmodel") or n.endswith(".report")) and e.is_dir():
                    return True
    except OSError:
        return False
    return False


def ancestors(path: str):
    d = path if os.path.isdir(path) else os.path.dirname(path)
    d = norm(d)
    while True:
        if d == HOME or os.path.dirname(d) == d:
            return
        yield d
        parent = norm(os.path.dirname(d))
        if parent == d:
            return
        d = parent


_ROOT: dict[str, str | None] = {}
_SYN: dict[str, bool] = {}
_POL: dict[str, dict] = {}
_PRIVATE: dict[str, object] = {"stamp": None, "roots": []}


def find_root(path: str) -> str | None:
    key = norm(path)
    if key in _ROOT:
        return _ROOT[key]
    root = next((d for d in ancestors(key) if is_workspace_root(d)), None)
    _ROOT[key] = root
    return root


def synthetic(path: str) -> bool:
    """True only when this path's own workspace policy sets synthetic.

    An ancestor policy with synthetic true does not exempt a child workspace.
    """
    key = norm(path)
    if key not in _SYN:
        root = find_root(key)
        data = read_policy_file(root) if root else None
        _SYN[key] = bool(data) and not data.get("_pbz_lockdown") and data.get("synthetic") is True
    return _SYN[key]


def load_policy(root: str | None) -> dict:
    if not root:
        return {}
    if root not in _POL:
        _POL[root] = read_policy_file(root) or {}
    return _POL[root]


def active_root(path: str) -> str | None:
    """Root of the zero-data workspace containing path (None for synthetic trees)."""
    root = find_root(path)
    return root if root and not synthetic(root) else None


def rel(path: str, root: str) -> str:
    return path[len(root):].lstrip("/") if path.startswith(root + "/") or path == root else path


def _node_ident(path: str):
    """Identity tuple for stamp walks. Index 1 is True, False (missing), or a failure tag.

    A reparse/symlink whose target cannot be read, or an lstat that fails for a
    reason other than absence, is marked so alias closure reports incomplete
    instead of treating the node as an ordinary non-link.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return (norm(path), False)
    except OSError:
        return (norm(path), "lstat_failed")
    link = None
    tag = getattr(info, "st_reparse_tag", 0)
    if tag or os.path.islink(path):
        try:
            link = os.readlink(path)
        except OSError:
            return (norm(path), "readlink_failed", info.st_dev, info.st_ino,
                    tag, info.st_nlink, None)
    return (norm(path), True, info.st_dev, info.st_ino, tag, info.st_nlink, link)


def _stamp_alias_closure(path: str, parts: list, *, budget: int = 256) -> bool:
    """Stamp `path`, every lexical ancestor to the root, and every alias target.

    Returns True when the closure finished; False when the BFS budget ran out.
    A partial stamp must not authorize memo reuse. lstat/readlink only.
    """
    queue = [os.path.abspath(path)]
    seen: set[str] = set()
    remaining = budget
    while queue:
        if remaining <= 0:
            return False
        remaining -= 1
        node = queue.pop(0)
        cur = node
        while True:
            key = norm(cur)
            if key not in seen:
                seen.add(key)
                ident = _node_ident(cur)
                parts.append(ident)
                status = ident[1] if len(ident) > 1 else False
                if status in ("lstat_failed", "readlink_failed"):
                    return False
                if status is True and len(ident) >= 7 and ident[6] is not None:
                    nxt = ident[6]
                    if not os.path.isabs(nxt):
                        nxt = os.path.join(os.path.dirname(cur), nxt)
                    queue.append(os.path.normpath(nxt))
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return True


def _private_stamp(raws: list[str]) -> tuple[tuple, bool]:
    """Identity of each configured root and every alias reachable from its path.

    Returns `(stamp_parts, complete)`. Incomplete traversal must not reuse the
    process memo. Uses lstat/readlink only — never realpath.
    """
    parts = []
    complete = True
    for raw in raws:
        expanded = os.path.expanduser(raw)
        parts.append(norm(raw))
        if not os.path.isabs(expanded):
            parts.append(_node_ident(os.getcwd()))
        if not _stamp_alias_closure(expanded, parts):
            complete = False
    return tuple(parts), complete


def _private_roots() -> list[str]:
    """Default store plus PBZ_PRIVATE_ROOT, each as a lexical path and a resolved path when it exists.

    Resolving the configured root protects its canonical target. The lexical path is kept as well,
    so the alias itself stays denied. A missing root is normalized without being treated as a link.
    The resolved list is reused only when the alias-closure stamp completed and still matches.
    A partial stamp never authorizes memo reuse.
    """
    raws = [PRIVATE_ROOT]
    configured = (os.environ.get("PBZ_PRIVATE_ROOT") or "").strip()
    if configured:
        raws.append(os.path.expanduser(configured))
    stamp_parts, complete = _private_stamp(raws)
    stamp = (tuple(norm(raw) for raw in raws), stamp_parts)
    if complete and _PRIVATE["stamp"] == stamp:
        return _PRIVATE["roots"]  # type: ignore[return-value]
    found: list[str] = []
    for raw in raws:
        lexical = norm(raw)
        if lexical not in found:
            found.append(lexical)
        if not os.path.lexists(raw):
            continue
        try:
            resolved = norm(os.path.realpath(raw))
        except OSError:
            continue
        if resolved and resolved not in found:
            found.append(resolved)
    if complete:
        _PRIVATE["stamp"] = stamp
        _PRIVATE["roots"] = found
    else:
        # Incomplete closure: return fresh roots but do not cache them.
        _PRIVATE["stamp"] = None
        _PRIVATE["roots"] = None
    return found


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + "/")


def global_denied(path: str) -> bool:
    for d in GLOBAL_DENY_DIRS:
        if _under(path, norm(d)):
            return True
    for root in _private_roots():
        if _under(path, root):
            return True
    return any(re.search(p, path) for p in GLOBAL_DENY_PATTERNS)


def _mirror_blocked(path: str, root: str) -> bool:
    """Reparse component from the mirror root through the leaf, or a hardlinked file leaf."""
    mirror = root.rstrip("/") + "/" + MIRROR_DIR
    if path != mirror and not path.startswith(mirror + "/"):
        return False
    if _is_link(mirror):
        return True
    if path == mirror:
        return False
    cur = mirror
    parts = path[len(mirror) + 1:].split("/")
    for index, part in enumerate(parts):
        if not part:
            continue
        cur = cur + "/" + part
        if _is_link(cur):
            return True
        if index == len(parts) - 1:
            try:
                info = os.lstat(cur)
            except OSError:
                return False
            if stat.S_ISREG(info.st_mode) and getattr(info, "st_nlink", 1) != 1:
                return True
    return False


def _reparse_blocked(path: str, root: str) -> bool:
    """Symlink or junction below the workspace. Same refusal the mirror uses.

    The workspace root itself may be reached through a junction and is not a refusal.
    Checked before an allow glob can exempt the lexical path.
    """
    root = root.rstrip("/")
    if path != root and not path.startswith(root + "/"):
        return False
    if path == root:
        return False
    cur = root
    for part in path[len(root) + 1:].split("/"):
        if not part:
            continue
        cur = cur + "/" + part
        if _is_link(cur):
            return True
    return False


def classify_path(path: str) -> str | None:
    """Reason key if content access to path is denied, else None."""
    path = norm(path)
    if global_denied(path):
        return "global"
    root = active_root(path)
    if not root:
        return None
    pol = load_policy(root)
    if pol.get("_pbz_lockdown"):
        return "error"
    r = rel(path, root)
    parts = r.split("/")
    if parts[0] == MIRROR_DIR:
        if _mirror_blocked(path, root):
            return "definition"
        return None
    if _reparse_blocked(path, root):
        return "definition"
    if any(p.endswith(".semanticmodel") or p.endswith(".report") for p in parts):
        if parts[-1] not in SAFE_DEFINITION_FILES:
            return "definition"
    if any(glob_to_regex(g).match(r) for g in pol.get("allow", [])):
        return None
    if any(glob_to_regex(g).match(r) for g in DEFAULT_WS_DENY + list(pol.get("deny", []))):
        return "data_file"
    return None


def dir_has_denied(d: str, name_filter: re.Pattern | None = None, cap_is_deny: bool = True) -> bool:
    """True if a denied file under d would be reached (optionally only files matching name_filter)."""
    count = 0
    for cur, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x.lower() != MIRROR_DIR]
        ncur = norm(cur)
        for name in files:
            count += 1
            if count > MAX_WALK_ENTRIES:
                return cap_is_deny
            if name_filter and not name_filter.search(name.lower()):
                continue
            if classify_path(ncur + "/" + name.lower()):
                return True
    return False


# ---------------------------------------------------------------- shell

SEG_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\r?\n)\s*")
PIPE_SPLIT = re.compile(r"(?<!\|)\|(?!\|)")
TOKEN_RE = re.compile(r"\"[^\"]*\"|'[^']*'|[^\s|<>]+")


def segments(cmd: str) -> list[list[str]]:
    out = []
    for s in SEG_SPLIT.split(cmd):
        if s.strip():
            out.append([p for p in PIPE_SPLIT.split(s) if p.strip()])
    return out


def tokens(part: str) -> list[str]:
    return [t.strip("'\"") for t in TOKEN_RE.findall(part)]


def verb_index(toks: list[str]) -> int:
    for i, t in enumerate(toks):
        if t not in ("&", "."):
            return i
    return len(toks)


def verb_of(toks: list[str]) -> str:
    i = verb_index(toks)
    return os.path.basename(toks[i].replace("\\", "/")).lower() if i < len(toks) else ""


def _bare_command(token: str) -> bool:
    text = token.strip()
    if re.match(r"^[a-zA-Z]:[\\/]", text) or text.startswith(("/", "\\", "~")):
        return False
    return "/" not in text and "\\" not in text


def _installed_cli_paths() -> set[str]:
    """Absolute paths of the pbz launcher shipped with this skill and its install links."""
    roots: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if not path:
            return
        key = norm(path)
        if key in seen:
            return
        seen.add(key)
        roots.append(path)
        try:
            real = os.path.realpath(path)
        except OSError:
            return
        rkey = norm(real)
        if rkey not in seen:
            seen.add(rkey)
            roots.append(real)

    add(SKILL_DIR)
    home = os.path.expanduser("~")
    for parts in (
        (".agents", "skills", "powerbi-zerodata"),
        (".claude", "skills", "powerbi-zerodata"),
        (".codex", "skills", "powerbi-zerodata"),
    ):
        add(os.path.join(home, *parts))
    found = set()
    for root in roots:
        for name in ("pbz.cmd", "pbz", "pbz.ps1", "pbz.exe"):
            found.add(norm(os.path.join(root, "bin", name)))
    return found


def _pbz_basename(toks: list[str]) -> bool:
    return verb_of(toks) in ("pbz", "pbz.cmd", "pbz.ps1", "pbz.exe")


def is_pbz(toks: list[str], cwd: str | None = None) -> bool:
    """True only when the verb is the installed pbz launcher.

    A command whose basename is pbz is not trusted, and neither is python invoking
    some path that merely ends in cli.py. Arguments are still scanned by the caller.
    """
    i = verb_index(toks)
    if i >= len(toks) or _bare_command(toks[i]):
        return False
    shaped = norm(toks[i], cwd)
    known = _installed_cli_paths()
    if shaped in known:
        return True
    try:
        if os.path.lexists(shaped) and norm(os.path.realpath(shaped)) in known:
            return True
    except OSError:
        return False
    return False


def looks_like_path(t: str) -> bool:
    if not t or len(t) > 400 or re.match(r"^[a-z][a-z0-9+.-]*://", t, re.I):
        return False
    if t.startswith("-"):
        return False
    if re.match(r"^[a-zA-Z]:[\\/]", t) or t.startswith(("/", "./", "../", ".\\", "..\\", "~")):
        return True
    return "/" in t or "\\" in t or bool(re.search(r"\.[A-Za-z0-9]{1,6}$", t))


def _lexical_definition(token: str) -> bool:
    """True when a token names a real model or report folder, including a `..` escape.

    A model or report whose parent component is still pbz-workspace after `..` is applied
    is the mirror and stays allowed.
    """
    bits = [part.lower() for part in _collapse_dots(token.replace("\\", "/").split("/"))]
    for index, part in enumerate(bits):
        if part.endswith(".semanticmodel") or part.endswith(".report"):
            if not (index > 0 and bits[index - 1] == MIRROR_DIR.lower()):
                return True
    return False


def interpreter_targets(toks: list[str]) -> tuple[list[str], list[str]]:
    """(script files, inline code) run by this pipe part."""
    i = verb_index(toks)
    if i >= len(toks):
        return [], []
    v, rest = verb_of(toks), toks[i + 1:]
    files: list[str] = []
    inline: list[str] = []

    def first_positional(stop_flags=(), code_flags=(), value_flags=()):
        values = {flag.lower() for flag in value_flags}
        j = 0
        while j < len(rest):
            t = rest[j]
            tl = t.lower()
            if tl in code_flags and j + 1 < len(rest):
                inline.append(" ".join(rest[j + 1:]))
                return
            if tl in stop_flags:
                return
            if tl in values and j + 1 < len(rest):
                j += 2
                continue
            if not t.startswith("-") and not (t.startswith("/") and len(t) <= 3):
                files.append(t)
                return
            j += 1

    if v.startswith("python") or v in ("py", "py.exe"):
        # `-X utf8` is a flag plus a value. The script is the next positional, not `utf8`.
        first_positional(stop_flags=("-m",), code_flags=("-c",), value_flags=("-x",))
    elif v in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
        value_flags = {
            "-executionpolicy", "-inputformat", "-outputformat", "-windowstyle",
            "-configurationname", "-workingdirectory", "-version", "-psconsolefile",
            "-custompipename",
        }
        j = 0
        while j < len(rest):
            t = rest[j]
            tl = t.lower()
            if tl in ("-file", "-f") and j + 1 < len(rest):
                files.append(rest[j + 1])
                break
            if tl in ("-command", "-c") and j + 1 < len(rest):
                inline.append(" ".join(rest[j + 1:]))
                break
            if tl in ("-encodedcommand", "-enc", "-e", "-ec") and j + 1 < len(rest):
                try:
                    inline.append(base64.b64decode(rest[j + 1]).decode("utf-16-le"))
                except (ValueError, UnicodeDecodeError):
                    inline.append("EVALUATE")  # undecodable payload: treat as engine access
                break
            if tl in value_flags and j + 1 < len(rest):
                j += 2
                continue
            if t.startswith("-") or (t.startswith("/") and len(t) <= 3):
                j += 1
                continue
            files.append(t)  # positional script, e.g. `pwsh .\\tool.ps1`
            break
    elif v in ("node", "node.exe", "deno", "bun"):
        first_positional(code_flags=("-e", "--eval", "-p"))
    elif v in ("bash", "sh", "zsh", "cmd", "cmd.exe"):
        first_positional(code_flags=("-c", "/c", "/k"))
    elif re.search(r"\.(ps1|py|js|bat|cmd|sh)$", v):
        files.append(toks[i])
    return files, inline


def hits(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


def _path_token(text: str, start: int, end: int) -> str:
    breaks = set(" \t\r\n\"'`<>|()[]{};,=+")
    while start > 0 and text[start - 1] not in breaks:
        start -= 1
    while end < len(text) and text[end] not in breaks:
        end += 1
    return text[start:end]


def _collapse_dots(parts: list[str]) -> list[str]:
    """Lexical path with `.` dropped and `..` applied. A leading `..` stays."""
    out: list[str] = []
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if out and out[-1] != "..":
                out.pop()
            else:
                out.append("..")
            continue
        out.append(part)
    return out


def _definition_outside_mirror(text: str) -> bool:
    """True when a model or report reference is not beside a pbz-workspace path component.

    The mirror name has to be its own component in that same path after `..` is applied.
    A file that merely contains the text pbz-workspace does not cover a separate definition reference.
    """
    for match in DEFINITION_REF.finditer(text):
        bits = _collapse_dots(re.split(r"[\\/]", _path_token(text, match.start(), match.end())))
        covered = False
        for index, part in enumerate(bits):
            leaf = part.lower()
            if leaf.endswith(".semanticmodel") or leaf.endswith(".report"):
                if index > 0 and bits[index - 1].lower() == MIRROR_DIR:
                    covered = True
                    break
        if not covered:
            return True
    return False


def script_bad(text: str, pol: dict) -> bool:
    if hits(text, ENGINE_PATTERNS + DATA_READ_PATTERNS + [re.escape(h) for h in pol.get("sensitive_hosts", [])]):
        return True
    if _definition_outside_mirror(text):
        return True
    return bool(DATA_EXT.search(text))


HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
INTERPRETERS = {"python", "python.exe", "python3", "py", "py.exe", "node", "node.exe", "bash", "sh", "zsh",
                "pwsh", "pwsh.exe", "powershell", "powershell.exe", "cmd", "cmd.exe", "deno", "bun"}


def split_heredocs(cmd: str) -> tuple[str, list[tuple[str, str]]]:
    """Remove heredoc bodies from the command text; return (command, [(introducing line, body)])."""
    lines, kept, bodies, i = cmd.split("\n"), [], [], 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        m = HEREDOC.search(line)
        if m:
            term, body = m.group(2), []
            i += 1
            while i < len(lines) and lines[i].strip() != term:
                body.append(lines[i])
                i += 1
            bodies.append((line, "\n".join(body)))
        i += 1
    return "\n".join(kept), bodies


_POLICY_TEXT = re.compile(r"(?i)\.pbz[\\/]+policy\.json")
_DELETE_VERBS = {"rm", "del", "erase", "rmdir", "rd", "remove-item", "ri"}
_MOVE_VERBS = {"mv", "move", "mi", "move-item", "ren", "rename", "rni", "rename-item"}
_COPY_VERBS = {"cp", "copy", "cpi", "copy-item", "xcopy", "robocopy"}
_RENAME_VERBS = {"ren", "rename", "rni", "rename-item"}
_NEW_VERBS = {"new-item", "ni"}
_CONTENT_VERBS = {
    "set-content", "sc", "add-content", "ac", "out-file", "tee-object", "tee", "clear-content", "clc",
}
_LINK_TYPES = {"hardlink", "symboliclink", "junction"}
_ARG_PREFIX = re.compile(r"(?i)^-[A-Za-z][\w-]*:(.*)$")


def strip_arg(token: str) -> str:
    """Drop one leading `-<name>:` prefix and surrounding quotes.

    Tokenization may leave an opening quote without the closer on colon-bound values
    (`-ItemType:"HardLink"` → `-ItemType:"HardLink`); strip residual quote marks too.
    """
    text = token
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    matched = _ARG_PREFIX.match(text)
    if matched:
        text = matched.group(1)
        if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
            text = text[1:-1]
        else:
            text = text.strip("\"'")
    return text


def _plain_name(part: str) -> str:
    """Windows file APIs ignore a trailing dot or space, and `name:stream` is `name`.

    A drive-relative form such as `C:..` keeps its drive and its `.` or `..`.
    """
    if re.fullmatch(r"[A-Za-z]:", part) or part in {".", ".."}:
        return part
    drive = ""
    body = part
    matched = re.match(r"^([A-Za-z]:)(.*)$", part)
    if matched:
        drive, body = matched.group(1), matched.group(2)
    if body in {".", ".."}:
        return drive + body
    if ":" in body:
        body = body.split(":", 1)[0]
    if body in {".", ".."}:
        return drive + body
    return drive + body.rstrip(". ")


def _path_bits(token: str) -> list[str]:
    return [_plain_name(part) for part in token.replace("\\", "/").split("/") if part not in ("", ".")]


def _is_policy_file(token: str) -> bool:
    bits = _path_bits(token)
    return len(bits) >= 2 and bits[-2].lower() == ".pbz" and bits[-1].lower() == "policy.json"


def _is_pbz_dir(token: str) -> bool:
    bits = _path_bits(token)
    return bool(bits) and bits[-1].lower() == ".pbz"


def _ancestor_dir(path: str, session_root: str | None) -> bool:
    if not session_root:
        return False
    path, root = norm(path), norm(session_root)
    return path != root and root.startswith(path + "/")


def _in_scope(path: str, session_root: str | None) -> bool:
    """At or below an active root, or inside an ancestor of the session root."""
    target = norm(path)
    if active_root(target):
        return True
    if not session_root:
        return False
    root = norm(session_root)
    if target == root or target.startswith(root + "/"):
        return True
    cursor = root
    while True:
        parent = norm(os.path.dirname(cursor))
        if parent == cursor:
            return False
        if target == parent or target.startswith(parent + "/"):
            return True
        cursor = parent


def _lexical_parent(base: str, token: str, index: int) -> str:
    """Parent directory of bits[index] in token. Absolute tokens ignore cwd."""
    parts = token.replace("\\", "/").split("/")
    stripped = token.strip().strip("'\"")
    absolute = (
        bool(re.match(r"^[A-Za-z]:", stripped))
        or stripped.startswith(("/", "\\"))
        or bool(re.match(r"^/[a-zA-Z](/|$)", stripped))
    )
    if absolute:
        prefix = "/".join(parts[:index])
        if re.fullmatch(r"[A-Za-z]:", prefix or ""):
            prefix = prefix + "/"
        return norm(prefix) if prefix else norm(base)
    cur = norm(base)
    for part in parts[:index]:
        if part in ("", "."):
            continue
        cur = norm(os.path.dirname(cur)) if part == ".." else norm(os.path.join(cur, part))
    return cur


def _wild_pbz(token: str, cur: str, session_root: str | None) -> bool:
    """True when a wildcard can name `.pbz` or `.pbz/policy.json` in scope."""
    if not any(ch in token for ch in "*?["):
        return False
    bits = token.replace("\\", "/").split("/")
    visible = [(i, part) for i, part in enumerate(bits) if part not in ("", ".")]
    for pos, (index, part) in enumerate(visible):
        if not fnmatch.fnmatch(".pbz", part):
            continue
        parent = _lexical_parent(cur, token, index)
        holds = os.path.lexists(os.path.join(parent, ".pbz"))
        if not (active_root(parent) == norm(parent) or _ancestor_dir(parent, session_root) or holds):
            continue
        nxt = visible[pos + 1][1] if pos + 1 < len(visible) else None
        if nxt is None or fnmatch.fnmatch("policy.json", nxt):
            return True
    return False


def _rename_operands(raw_args: list[str]) -> tuple[str | None, str | None]:
    """Source and new name for Rename-Item / ren.

    Reads raw tokens (before strip_arg) so `-Path:` / `-NewName:` bindings survive in either order.
    """
    src = new = None
    positional: list[str] = []
    i = 0
    while i < len(raw_args):
        tok = raw_args[i]
        bound = re.match(r"(?i)^-(path|literalpath|newname):(.*)$", tok)
        if bound:
            # strip_arg keeps the binding and clears residual tokenizer quotes
            # (`-NewName:"policy.json` → policy.json).
            name, val = bound.group(1).lower(), strip_arg(tok)
            if name in ("path", "literalpath"):
                src = val
            else:
                new = val
            i += 1
            continue
        key = tok.lower()
        if key in ("-path", "-literalpath") and i + 1 < len(raw_args):
            src = strip_arg(raw_args[i + 1])
            i += 2
            continue
        if key == "-newname" and i + 1 < len(raw_args):
            new = strip_arg(raw_args[i + 1])
            i += 2
            continue
        if key in ("-force", "-whatif", "-confirm", "-passthru", "-verbose", "-debug"):
            i += 1
            continue
        if key in ("-erroraction", "-warningaction", "-informationaction", "-errorvariable",
                   "-warningvariable", "-outvariable", "-outbuffer", "-pipelinevariable") and i + 1 < len(raw_args):
            i += 2
            continue
        if tok.startswith("-") and len(tok) > 1 and tok[1].isalpha():
            i += 1
            continue
        positional.append(strip_arg(tok))
        i += 1
    if src is None and positional:
        src = positional.pop(0)
    if new is None and positional:
        new = positional[0]
    return src, new


def _policy_shell(part: str, toks: list[str], verb: str, cur: str, session_root: str | None) -> str | None:
    """Policy-file shape rules. `pbz` invocations never reach here."""
    if _POLICY_TEXT.search(part):
        return "policy"
    raw_args = toks[verb_index(toks) + 1:]
    stripped = [strip_arg(token) for token in toks]
    args = stripped[verb_index(toks) + 1:]
    cur_bits = _path_bits(norm(cur))
    cwd_pbz = bool(cur_bits) and fnmatch.fnmatch(cur_bits[-1], ".pbz")
    for raw in args:
        shaped = norm(raw, cur)
        if _is_policy_file(raw) or _is_policy_file(shaped):
            return "policy"
        bits = _path_bits(raw)
        wild = any(ch in raw for ch in "*?[")
        if wild and len(bits) >= 2 and fnmatch.fnmatch(".pbz", bits[-2]) and fnmatch.fnmatch("policy.json", bits[-1]):
            return "policy"
        if cwd_pbz and bits and fnmatch.fnmatch("policy.json", bits[-1]):
            return "policy"
        if verb in _DELETE_VERBS | _MOVE_VERBS | _COPY_VERBS and _wild_pbz(raw, cur, session_root):
            return "policy"
    targets = list(args)
    if verb in _RENAME_VERBS:
        src, dest = _rename_operands(raw_args)
        if src is not None and dest is not None and "/" not in dest and "\\" not in dest:
            targets.append(os.path.join(os.path.dirname(src) or ".", dest))
    if verb in _DELETE_VERBS:
        for raw in targets:
            shaped = norm(raw, cur)
            if (_is_pbz_dir(raw) or _is_pbz_dir(shaped)) and _in_scope(shaped, session_root):
                return "policy"
    if verb in _MOVE_VERBS or verb in _COPY_VERBS:
        for raw in targets:
            shaped = norm(raw, cur)
            if _is_pbz_dir(raw) or _is_pbz_dir(shaped) or _is_policy_file(raw) or _is_policy_file(shaped):
                return "policy"
    if verb in _NEW_VERBS and any(item.lower().strip("\"'") in _LINK_TYPES for item in stripped):
        scoped = _in_scope(norm(cur), session_root) or bool(session_root and _in_scope(session_root, session_root))
        scoped = scoped or any(_in_scope(norm(raw, cur), session_root) for raw in args)
        if scoped:
            return "policy"
        for raw in args:
            shaped = norm(raw, cur)
            if _is_pbz_dir(raw) or _is_pbz_dir(shaped) or _is_policy_file(raw) or _is_policy_file(shaped):
                return "policy"
    return None


def _protected_pieces(text: str) -> list[str]:
    low = text.lower().replace("/", "\\")
    low = low.replace('"', " ").replace("'", " ")
    low = re.sub(r"[><|;,]", " ", low)
    low = re.sub(r"(?<=[a-z0-9]):(?=[.\\])", " ", low)
    return [name for part in re.split(r"[\s\\]+", low) if (name := _plain_name(part))]


def _mentions_protected(text: str) -> bool:
    """True for a `.pbz` path segment or `policy.json`, including quoted and redirected forms.

    `.pbzignore` is a different segment and does not match.
    A component containing `~` and a digit is an 8.3 short name (`PBZ~1`, `POLICY~1.JSO`).
    An existing short name is judged by its long name, so `RUNNER~1` (the profile
    directory) is not `.pbz`. A short name that does not exist stays fail-closed.
    """
    parts = _protected_pieces(_expand_cmd_shorts(text))
    if ".pbz" in parts or "policy.json" in parts:
        return True
    return any(re.search(r"~\d", part) for part in parts)


def _wildcard_protected(text: str) -> bool:
    """True when a wildcard segment can select `.pbz`, or `policy.json` inside that directory."""
    parts = _protected_pieces(text)
    for index, part in enumerate(parts):
        if not any(ch in part for ch in "*?["):
            continue
        parent = parts[index - 1] if index else ""
        parent_is_pbz = parent == ".pbz" or (any(ch in parent for ch in "*?[") and fnmatch.fnmatch(".pbz", parent))
        # A bare `*` inside another directory is that directory's children, not `.pbz`.
        if part != "*" and fnmatch.fnmatch(".pbz", part):
            return True
        if part == "*" and parent_is_pbz:
            return True
        if parent_is_pbz and fnmatch.fnmatch("policy.json", part):
            return True
    return False


_CMD_LETTER_SWITCH_VERBS = {"rd", "rmdir", "del", "erase", "copy", "move"}
_CMD_WORD_SWITCH_VERBS = {"robocopy", "xcopy"}


_BRACE_LIMIT = 64


def _brace_one(token: str) -> list[str] | None:
    """Expand the innermost comma-brace, or None when this token has none.

    A `}` whose body has no comma is literal. The opener under it stays open,
    so `{../../ws,{x}}` still splits into `../../ws` and `{x}`.
    """
    stack: list[int] = []
    for index, char in enumerate(token):
        if char == "{":
            stack.append(index)
            continue
        if char != "}" or not stack:
            continue
        opener = stack.pop()
        body = token[opener + 1:index]
        if "," not in body:
            continue
        pre, post = token[:opener], token[index + 1:]
        return [pre + piece + post for piece in body.split(",")]
    return None


def _sequence_brace(token: str) -> bool:
    """True for a bash sequence such as `{z..z}`. A comma list is not a sequence."""
    stack = []
    for index, char in enumerate(token):
        if char == "{":
            stack.append(index)
            continue
        if char != "}" or not stack:
            continue
        body = token[stack.pop() + 1:index]
        if ".." in body and "," not in body:
            return True
    return False


def _brace_expand(token: str) -> list[str] | None:
    """Expand bash `{a,b}` lists, including several groups and nested groups.

    None means the cap was hit, a brace list is still unresolved, or a `{a..b}`
    sequence is present. A token without a comma-brace stays as it is, including
    a literal `{name}`.
    """
    current = [token]
    for _ in range(_BRACE_LIMIT):
        nxt: list[str] = []
        grew = False
        for item in current:
            expanded = _brace_one(item)
            if expanded is None:
                nxt.append(item)
                continue
            grew = True
            nxt.extend(expanded)
            if len(nxt) > _BRACE_LIMIT:
                return None
        current = nxt
        if not grew:
            break
    for item in current:
        if _brace_one(item) is not None or item.count("{") != item.count("}") or _sequence_brace(item):
            return None
    return current


def _switch_name(token: str) -> str:
    """Flag name before a colon value. `-Rec:$true` stays `-Rec`; paths still use strip_arg."""
    text = token.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1]
    if text.startswith("-"):
        return text.split(":", 1)[0]
    return strip_arg(text)


def _cmd_letter_switches(token: str) -> list[str] | None:
    """Split a cmd run of one-letter switches (`/s/q`) into `/s` and `/q`."""
    if not token.startswith("/"):
        return None
    parts = [part for part in token[1:].split("/") if part]
    if len(parts) >= 2 and all(len(part) == 1 and part.isalpha() for part in parts):
        return ["/" + part for part in parts]
    return None


def _shell_operand(token: str, cur: str, *, cmd_switches: bool = False, letter_only: bool = False) -> str | None:
    """A command operand, or None for a switch.

    `/` and MSYS `/c` or `/c/` are the drive root. `/Users` is `<drive>\\Users`.
    A single-letter or one-word `/MIR` switch is dropped only for cmd verbs.
    """
    if token.startswith("-"):
        return None
    if not token.startswith("/"):
        return token
    parts = [part for part in token[1:].split("/") if part != ""]
    drive = os.path.splitdrive(os.path.abspath(cur))[0]
    rooted = (drive + "\\") if drive else "\\"
    if not parts:
        return rooted
    single_letter = len(parts) == 1 and len(parts[0]) == 1 and parts[0].isalpha()
    if single_letter and cmd_switches and not token.endswith("/"):
        return None
    if single_letter:
        return parts[0].upper() + ":\\"
    if len(parts[0]) == 1 and parts[0].isalpha():
        rest = "\\" + "\\".join(parts[1:]) if len(parts) > 1 else "\\"
        return parts[0].upper() + ":" + rest
    if cmd_switches and not letter_only and len(parts) == 1 and parts[0].isalpha():
        return None
    return rooted + "\\".join(parts)


def _win_components(token: str) -> str:
    """Drop trailing dots and spaces from each path component. `.` and `..` stay."""
    cleaned = []
    for part in token.replace("/", "\\").split("\\"):
        if part in {"", ".", ".."} or re.fullmatch(r"[A-Za-z]:", part):
            cleaned.append(part)
        else:
            cleaned.append(_plain_name(part))
    return "\\".join(cleaned)


_HOME_ASSIGN = re.compile(
    r"(?i)(?:^|[\s;&|(])(?:export\s+|set\s+)?(?:\$env:)?(?:home|userprofile)\s*="
)


def _covers_workspace(raw: str, cur: str, root: str, *, home_rebound: bool = False) -> bool:
    """True when a recursive operand is the workspace, an ancestor, or a wildcard over one of those.

    `$` and `%` are not expanded. The guard process and the shell can disagree, and a
    prefix assignment can change the shell's value. `$true`, `$false`, and `$null` are not paths.
    """
    if "`" in raw or "^" in raw or "(" in raw or ")" in raw or "::" in raw:
        return True
    text = raw.replace("/", "\\").strip()
    if "%" in text or "!" in text:
        return True
    if "$" in text and text.lower() not in {"$true", "$false", "$null"}:
        return True
    if home_rebound and (text == "~" or text.startswith("~\\")):
        return True
    if text.startswith("~") and text != "~" and not text.startswith("~\\"):
        return True
    if text == "~" or text.startswith("~\\"):
        text = os.path.expanduser(text)
    drive_rel = re.match(r"^([A-Za-z]):([^\\].*)$", text)
    if drive_rel:
        cur_drive = os.path.splitdrive(os.path.abspath(cur))[0]
        if not cur_drive or cur_drive[0].lower() != drive_rel.group(1).lower():
            return True
        text = drive_rel.group(2)
    for part in text.split("\\"):
        if ":" in part and not re.fullmatch(r"[A-Za-z]:", part):
            return True
    token = _win_components(text)
    if re.fullmatch(r"[A-Za-z]:\\?\*?", token):
        return True
    if token in {".", ".."}:
        return _ancestor_or_self(norm(token, cur), root)
    if any(ch in token for ch in "*?["):
        head = token
        for mark in "*?[":
            head = head.split(mark)[0]
        head = head.rstrip("\\")
        if _ancestor_or_self(norm(head or ".", cur), root):
            return True
        parts = token.split("\\")
        for index, part in enumerate(parts):
            if not any(ch in part for ch in "*?["):
                continue
            directory = "\\".join(parts[:index]) or "."
            if not _ancestor_or_self(norm(directory, cur), root):
                continue
            if (fnmatch.fnmatch("policy.json", part) or fnmatch.fnmatch(".pbz", part)
                    or re.search(r"~\d", part)):
                return True
        patterns = [token]
        if token.endswith("\\*"):
            parent_pat = token[:-2].rstrip("\\")
            if parent_pat:
                patterns.append(parent_pat)
        for candidate in patterns:
            pattern = os.path.normcase(norm(candidate, cur))
            current = os.path.normcase(os.path.abspath(root))
            while True:
                if fnmatch.fnmatch(current, pattern):
                    return True
                parent = os.path.dirname(current)
                if parent == current:
                    break
                current = parent
        return False
    return _ancestor_or_self(norm(token, cur), root)


def _ancestor_or_self(shaped: str, root: str) -> bool:
    """True when `shaped` is the workspace root or a directory that contains it.

    `ValueError` from `commonpath` (a different drive, or a prefix it rejects) returns False.
    `OSError` returns True. Bare drive roots are denied before this helper runs.
    """
    try:
        left = os.path.normcase(os.path.abspath(shaped))
        right = os.path.normcase(os.path.abspath(root))
        return os.path.commonpath([left, right]) == left
    except ValueError:
        return False
    except OSError:
        return True


def _shell_redirects(part: str) -> list[str] | None:
    """Redirect operands, or None when brace expansion must fail closed."""
    names: list[str] = []
    for target in re.findall(r">+\s*(\"[^\"]*\"|'[^']*'|\S+)", part):
        expanded = _brace_expand(strip_arg(target))
        if expanded is None:
            return None
        names.extend(expanded)
    return names


def _redirect_reason(part: str, cur: str, session_root: str | None) -> str | None:
    """Policy paths and, inside a workspace, redirects onto guard/skill files."""
    names = _shell_redirects(part)
    if names is None:
        return "policy"
    for target in names:
        if _mentions_protected(target) or _wildcard_protected(target):
            return "policy"
        if session_root and self_protected(norm(target, cur)):
            return "selfprotect"
    return None


def _r18_shell(part: str, toks: list[str], verb: str, cur: str, session_root: str | None,
               *, home_rebound: bool = False) -> str | None:
    """Round 18 shell gate: protected writes, tree copy/move/delete, and fail-closed operands.

    Sequence braces, unexpanded `$` / `%` / `!`, parentheses, `::`, a rebound `~`, a colon that
    is not a drive prefix, and any bare drive root deny. A recursive delete whose wildcard sits
    in the workspace or an ancestor and can match `policy.json` or `.pbz` denies. Moves are tree
    operations even without a recurse flag.
    """
    if not session_root or not verb:
        return None
    low = part.lower()
    why = _redirect_reason(part, cur, session_root)
    if why:
        return why
    if verb in _CONTENT_VERBS and (_mentions_protected(part) or _wildcard_protected(part)):
        return "policy"
    if verb in _NEW_VERBS and (_mentions_protected(part) or _wildcard_protected(part)) and any(
            strip_arg(t).lower().startswith("-value") for t in toks):
        return "policy"
    if verb in _DELETE_VERBS and (_mentions_protected(part) or _wildcard_protected(part)):
        return "policy"
    has_dash = any(strip_arg(item).startswith("-") for item in toks)
    letter_only = verb in _CMD_LETTER_SWITCH_VERBS
    # A dash flag disables one-letter / switch dropping for every cmd verb class.
    cmd_switches = False
    if not has_dash:
        if letter_only:
            cmd_switches = True
        elif verb in _CMD_WORD_SWITCH_VERBS:
            cmd_switches = True
    flag_tokens: list[str] = []
    switch_verb = letter_only or verb in _CMD_WORD_SWITCH_VERBS
    for token in toks:
        text = _switch_name(token)
        pieces = _cmd_letter_switches(text) if switch_verb else None
        flag_tokens.extend(pieces or [text])
    flags = {item.lower() for item in flag_tokens}
    recurse = bool(flags & {"-recurse", "-r", "-rf", "-fr", "/e", "/s", "/mir", "/purge"}) or any(
        flag in low for flag in ("/mir", "/purge", "-recurse"))
    recurse = recurse or any(
        flag.split("=", 1)[0] in {"--recursive", "--recurse", "--archive"} or flag.startswith("-rec")
        for flag in flags)
    recurse = recurse or any(
        flag.startswith("-") and not flag.startswith("--") and flag[1:].isalpha() and any(ch in flag[1:] for ch in "ra")
        for flag in flags)
    here = os.path.normcase(norm(cur))
    root = os.path.normcase(session_root)
    args = []
    brace_failed = False
    for token in toks[verb_index(toks) + 1:]:
        text = strip_arg(token)
        if cmd_switches and _cmd_letter_switches(text):
            continue
        expanded = _brace_expand(text)
        if expanded is None:
            if verb in _DELETE_VERBS | _MOVE_VERBS | _COPY_VERBS | _CONTENT_VERBS | _NEW_VERBS:
                return "policy"
            brace_failed = True
            continue
        for item in expanded:
            for piece in item.split(","):
                if piece == "":
                    continue
                if piece.count("{") != piece.count("}"):
                    brace_failed = True
                    continue
                if verb in _DELETE_VERBS | _MOVE_VERBS | _COPY_VERBS | _CONTENT_VERBS | _NEW_VERBS and (
                        _mentions_protected(piece) or _wildcard_protected(piece)):
                    return "policy"
                operand = _shell_operand(piece, cur, cmd_switches=cmd_switches, letter_only=letter_only)
                if operand is not None:
                    args.append(operand)
    tree_verbs = _MOVE_VERBS | (_COPY_VERBS if recurse else set()) | (_DELETE_VERBS if recurse else set())
    if verb in tree_verbs and brace_failed:
        return "policy"
    if verb in tree_verbs:
        if _mentions_protected(part):
            return "policy"
        for raw in args:
            if _covers_workspace(raw, cur, session_root, home_rebound=home_rebound):
                return "policy"
        if not args and here == root:
            return "policy"
    if verb == "robocopy" and ("/mir" in low or "/purge" in low):
        for raw in args:
            if _mentions_protected(raw) or _ancestor_or_self(norm(raw, cur), session_root):
                return "policy"
    return None


def check_shell(cmd: str, cwd: str, session_root: str | None = None, _depth: int = 0) -> str | None:
    if _depth == 0 and session_root is None:
        session_root = active_root(norm(cwd))
    cmd, bodies = split_heredocs(cmd)
    if session_root and _mentions_protected(cmd):
        write_verbs = _CONTENT_VERBS | _DELETE_VERBS | _NEW_VERBS
        low = cmd.lower()
        if ">" in cmd or any(re.search(rf"(^|[\s;|&]){re.escape(verb)}($|[\s;|&])", low) for verb in write_verbs):
            return "policy"
    for lead, body in bodies:  # a heredoc is code only when it feeds an interpreter; otherwise it is text
        if _POLICY_TEXT.search(body):
            return "policy"
        lead_root = active_root(cwd)
        for part in PIPE_SPLIT.split(SEG_SPLIT.split(lead)[-1]):
            if verb_of(tokens(part)) in INTERPRETERS:
                if not synthetic(cwd) and hits(body, ENGINE_PATTERNS):
                    return "engine"
                if lead_root and script_bad(body, load_policy(lead_root)):
                    return "script"
    cur = cwd
    dir_stack: list[str] = []
    previous: str | None = None
    made: set[str] = set()

    def _exists(path: str) -> bool:
        return os.path.isdir(path) or os.path.normcase(path) in made
    for seg in segments(cmd):
        for part in seg:
            toks = tokens(part)
            if not toks:
                continue
            v = verb_of(toks)
            trusted = is_pbz(toks, cur)
            # Basename pbz is not the installed CLI. Admin is denied either way.
            # Arguments of both are scanned below; only the installed launcher may
            # carry engine text (pbz synth query).
            if (_pbz_basename(toks) or trusted) and re.search(r"(?i)\badmin\b", part):
                return "admin"
            if v in CD_VERBS or v in POP_VERBS:
                # Policy and self-protected redirects before tracking cwd.
                why = _redirect_reason(part, cur, session_root)
                if why:
                    return why
                if _policy_shell(part, toks, v, cur, session_root):
                    return "policy"
                if v in POP_VERBS:
                    if dir_stack:
                        target = dir_stack.pop()
                        if _exists(target):
                            previous, cur = cur, target
                    continue
                dest = None
                for token in toks[verb_index(toks) + 1:]:
                    value = strip_arg(token)
                    if value == "-":
                        dest = "-"
                        break
                    if value.startswith("-"):
                        continue
                    dest = value
                    break
                if dest == "-":
                    if previous and _exists(previous):
                        if v in PUSH_VERBS:
                            dir_stack.append(cur)
                        previous, cur = cur, previous
                elif dest is not None:
                    target = norm(dest, cur)
                    if _exists(target):
                        if v in PUSH_VERBS:
                            dir_stack.append(cur)
                        previous, cur = cur, target
                continue
            if _policy_shell(part, toks, v, cur, session_root):
                return "policy"
            why = _r18_shell(part, toks, v, cur, session_root, home_rebound=bool(_HOME_ASSIGN.search(cmd)))
            if why:
                return why
            root = active_root(cur)
            pol = load_policy(root)
            if not trusted and not synthetic(cur) and hits(part, ENGINE_PATTERNS):
                return "engine"
            if root:
                if hits(part, DATA_READ_PATTERNS + [re.escape(h) for h in pol.get("sensitive_hosts", [])]):
                    return "engine"
                if any(s.lower() in part.lower() for s in pol.get("deny_scripts", [])):
                    return "script"
            denied, dir_targets, has_path = None, [], False
            for t in toks[verb_index(toks) + 1:]:
                t = strip_arg(t)
                if t.startswith("-"):
                    attached = ""
                    for sep in (":", "="):
                        if sep in t[1:]:
                            attached = strip_arg(t.split(sep, 1)[1])
                            break
                    if not attached or attached.startswith("-"):
                        continue
                    t = attached
                glob_operand = "*" in t or "?" in t
                if glob_operand and DATA_EXT.search(t):
                    base = norm(os.path.dirname(t) or ".", cur)
                    if active_root(base) or root:
                        denied = denied or "data_file"
                    continue
                # Inline -Command/-c payloads are multi-token strings; classify via recursion, not as paths.
                if v in INTERPRETERS and (" " in t or "\t" in t):
                    continue
                # A glob is classified even when the literal pattern is not a file on disk.
                if not glob_operand and not looks_like_path(t) and not (len(t) < 260 and os.path.exists(norm(t, cur))):
                    continue
                has_path = True
                p = norm(t, cur)
                why = classify_path(p)
                # An allow glob does not exempt a real model or report reference.
                if not why and root and _lexical_definition(t):
                    why = "definition"
                if why:
                    denied = denied or why
                elif os.path.isdir(p):
                    dir_targets.append(p)
            if denied and v not in SAFE_PATH_VERBS:
                return denied
            if v in CONTENT_SEARCH_VERBS:
                for d in dir_targets or ([] if has_path else [cur]):
                    if (active_root(d) or root) and dir_has_denied(d):
                        return "search"
            files, inline = interpreter_targets(toks)
            for code in inline:
                if (root or not synthetic(cur)) and script_bad(code, pol) and (root or hits(code, ENGINE_PATTERNS)):
                    return "script"
                if _depth == 0:
                    why = check_shell(code, cur, session_root, 1)
                    if why:
                        return why
            for f in files:
                p = norm(f, cur)
                why = classify_path(p)
                if why:
                    return why
                sroot = active_root(p) or root
                if sroot or not synthetic(p):
                    try:
                        if os.path.getsize(p) > MAX_SCRIPT_BYTES:
                            return "error"
                        with open(p, encoding="utf-8", errors="replace") as fh:
                            text = fh.read()
                    except OSError:
                        return "error"
                    if _POLICY_TEXT.search(text):
                        return "policy"
                    if (sroot and script_bad(text, load_policy(sroot))) or hits(text, ENGINE_PATTERNS):
                        return "script"
            if v in {"mkdir", "md"}:
                for token in toks[verb_index(toks) + 1:]:
                    value = strip_arg(token)
                    if value.startswith("-"):
                        attached = ""
                        for sep in (":", "="):
                            if sep in value[1:]:
                                attached = strip_arg(value.split(sep, 1)[1])
                                break
                        value = attached
                    if value and not value.startswith("-"):
                        made.add(os.path.normcase(norm(value, cur)))
    return None


# ---------------------------------------------------------------- tools

def patch_paths(text: str) -> list[str]:
    return [m.group(1) for m in re.finditer(r"(?m)^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", text)]


def string_values(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in string_values(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in string_values(v)]
    return []


def host_blocked(url: str, pol: dict, in_ws: bool) -> bool:
    m = re.match(r"(?i)^[a-z][a-z0-9+.-]*://([^/:?#]+)", url.strip())
    host = (m.group(1) if m else url.strip().split("/")[0]).lower()
    hosts = list(GLOBAL_SENSITIVE_HOSTS)
    if in_ws:
        hosts += WS_SENSITIVE_HOSTS + [h.lower() for h in pol.get("sensitive_hosts", [])]
    return any(host == h or host.endswith("." + h) for h in hosts)


def self_protected(path: str) -> bool:
    guarded = [norm(SKILL_DIR), norm(os.path.realpath(SKILL_DIR))] + [
        norm(os.path.join(os.path.expanduser("~"), *p)) for p in (
            (".claude", "settings.json"), (".claude", "settings.local.json"), (".claude", "CLAUDE.md"),
            (".codex", "hooks.json"), (".codex", "config.toml"), (".codex", "AGENTS.md"),
            (".agents", "skills", "powerbi-zerodata"), (".claude", "skills", "powerbi-zerodata"),
            (".codex", "skills", "powerbi-zerodata"))]
    return any(path == g or path.startswith(g + "/") for g in guarded) or path.endswith("/.claude/settings.json") \
        or path.endswith("/.claude/settings.local.json") or path.endswith("/.codex/hooks.json")


def is_memory_path(path: str) -> bool:
    return bool(re.search(r"/\.claude/projects/[^/]+/memory/", path)) or "/.codex/memories/" in path


# Same numbers policy.load fills in when a key is absent. Kept here so the hook stays stdlib-only.
_DEFAULT_BUDGETS = {"session_checks": 300, "per_target": 60, "constants_per_target": 3}
_DEFAULT_CATEGORY_MAX = 50


def _as_limit(value):
    if type(value) not in (int, float):
        return None
    return value


def _effective_budgets(pol: dict) -> dict:
    """policy.load's budget map: defaults, then any numeric keys in the file."""
    merged = dict(_DEFAULT_BUDGETS)
    raw = pol.get("budgets")
    if isinstance(raw, dict):
        for key, value in raw.items():
            number = _as_limit(value)
            if number is not None:
                merged[key] = number
    return merged


def _category_max(pol: dict):
    if "category_max" in pol:
        number = _as_limit(pol.get("category_max"))
        if number is not None:
            return number
    return _DEFAULT_CATEGORY_MAX


def policy_loosened(old: dict, new: dict) -> bool:
    if new.get("synthetic") and not old.get("synthetic"):
        return True
    if old.get("disclosure", "shape") == "strict" and new.get("disclosure", "shape") != "strict":
        return True
    if old.get("literals") == "strict" and new.get("literals") != "strict":
        return True
    for key in ("deny", "deny_scripts", "sensitive_hosts", "sensitive_columns"):
        if not set(old.get(key, [])) <= set(new.get(key, [])):
            return True
    for key in ("allow", "category_columns", "reveal_columns", "memory_dirs"):
        if not set(new.get(key, [])) <= set(old.get(key, [])):
            return True
    if _category_max(new) > _category_max(old):
        return True
    before, after = _effective_budgets(old), _effective_budgets(new)
    for key, old_value in before.items():
        new_value = after.get(key)
        if new_value is None or new_value > old_value:
            return True
    return False


def edited_text(path: str, ti: dict) -> str | None:
    if isinstance(ti.get("content"), str):
        return ti["content"]
    old, new = ti.get("old_string"), ti.get("new_string")
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            cur = f.read()
    except OSError:
        cur = ""
    return cur.replace(old, new) if ti.get("replace_all") else cur.replace(old, new, 1)


def _policy_target(path: str) -> bool:
    return path.replace("\\", "/").lower().endswith("/.pbz/policy.json")


def check_write(p: str, tool: str, ti: dict, session_root: str | None) -> str | None:
    if session_root and self_protected(p):
        return "selfprotect"
    if _policy_target(p):
        root = find_root(p)
        pol = load_policy(root) if root else {}
        parent = norm(os.path.dirname(os.path.dirname(p)))
        # Lockdown, an ancestor policy, or a path no active root covers: refuse before reading it.
        if pol.get("_pbz_lockdown") or not active_root(p):
            return "policy"
        if session_root and _ancestor_dir(parent, session_root):
            return "policy"
        if root and _ancestor_dir(parent, root):
            return "policy"
        if tool == "apply_patch":
            return "policy"
        text = edited_text(p, ti)
        try:
            new = json.loads(text) if text is not None else None
        except ValueError:
            new = None
        if not security_values_ok(new) or policy_loosened(pol, new if isinstance(new, dict) else {}):
            return "policy"
    if p.endswith("/.pbz/consumers.json") and active_root(p):
        return "consumers"  # dropping a consumer would loosen protection; pbz writes this file itself
    if session_root and is_memory_path(p):
        chunk = " ".join(s for s in (ti.get("content"), ti.get("new_string")) if isinstance(s, str))
        if tool == "apply_patch":
            chunk = str(ti.get("command") or ti.get("patch") or "")
        if hits(chunk, MEMORY_DATA_PATTERNS):
            return "memory"
    return None


def _grep_name_filter(glob: str) -> re.Pattern | None:
    """Filename filter from the glob itself.

    `*.c*` must stay a glob. Taking the first `\\.([a-z0-9]+)` suffix turns it into `\\.c$`
    and misses denied names the glob would open. A glob that is not a simple filename
    pattern is not narrowed, so every name is checked.
    """
    base = glob.replace("\\", "/").split("/")[-1]
    if not base or not re.fullmatch(r"[*?a-z0-9._-]+", base):
        return None
    return glob_to_regex(base)


def check_grep(ti: dict, cwd: str, session_root: str | None) -> str | None:
    p = norm(ti.get("path") or cwd, cwd)
    why = classify_path(p)
    if why:
        return why
    if not os.path.isdir(p):
        return None
    name_filter = None
    glob = (ti.get("glob") or "").strip().lower()
    gtype = (ti.get("type") or "").strip().lower()
    if glob:
        name_filter = _grep_name_filter(glob)
    elif gtype in GREP_TYPE_EXT:
        name_filter = re.compile(r"\." + GREP_TYPE_EXT[gtype] + r"$")
    if dir_has_denied(p, name_filter, cap_is_deny=bool(active_root(p) or session_root)):
        return "search"
    return None


def decide(event: dict) -> str | None:
    tool = str(event.get("tool_name") or "")
    ti = event.get("tool_input")
    if not isinstance(ti, dict):
        ti = {"value": ti}
    cwd = norm(event.get("cwd") or os.getcwd())
    session_root = active_root(cwd)
    spol = load_policy(session_root)

    if PBI_MCP.match(tool) and not synthetic(cwd):
        return "pbi_mcp"
    if session_root and (UI_TOOLS.search(tool) or BROWSER_READ_TOOLS.match(tool)):
        return "ui"
    if tool == "WebFetch" or re.match(r"^mcp__Claude_Browser__(navigate|preview_start|tabs_create)$", tool) \
            or re.match(r"(?i)^mcp__claude-in-chrome__navigate$", tool):
        url = ti.get("url")
        if isinstance(url, str) and url and host_blocked(url, spol, bool(session_root)):
            return "web"
        return None

    if tool in ("Bash", "PowerShell", "shell", "exec_command", "local_shell", "unified_exec"):
        cmd = ti.get("command") if "command" in ti else ti.get("cmd")
        if isinstance(cmd, list):
            cmd = " ".join(str(c) for c in cmd)
        return check_shell(cmd, cwd) if isinstance(cmd, str) else None

    if tool == "Grep":
        return check_grep(ti, cwd, session_root)
    if tool in ("Glob", "LS") or tool in NON_FILE_TOOLS or tool.startswith("mcp__ccd_"):
        return None

    writes = False
    if tool == "apply_patch":
        text = ti.get("command") or ti.get("patch") or ti.get("input") or ""
        paths = [norm(p, cwd) for p in patch_paths(" ".join(map(str, text)) if isinstance(text, list) else str(text))]
        writes = True
    elif tool in ("Read", "Edit", "Write", "NotebookEdit", "MultiEdit"):
        p = ti.get("file_path") or ti.get("notebook_path") or ti.get("path")
        paths = [norm(p, cwd)] if isinstance(p, str) and p else []
        writes = tool != "Read"
    elif FILEISH_TOOLS.search(tool) or _MUTATING_TOOL.search(tool):
        paths = [norm(s, cwd) for s in string_values(ti) if looks_like_path(s)]
        for key in ("file_path", "notebook_path", "path"):
            val = ti.get(key)
            if isinstance(val, str) and val:
                shaped = norm(val, cwd)
                if shaped not in paths:
                    paths.append(shaped)
        writes = bool(_MUTATING_TOOL.search(tool))
    else:
        return None

    for p in paths:
        why = classify_path(p)
        if why:
            return why
        if writes:
            why = check_write(p, tool, ti, session_root)
            if why:
                return why
    return None


# ---------------------------------------------------------------- io

def log(root: str | None, agent: str, tool: str, verdict: str) -> None:
    try:
        d = os.path.join(root, ".pbz", "logs") if root else os.path.join(PRIVATE_ROOT, "guard-log")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "guard.log"), "a", encoding="utf-8") as f:
            f.write(f"{_dt.datetime.now().isoformat(timespec='seconds')}\t{agent}\t{tool}\t{verdict}\n")
    except OSError:
        pass


def deny_json(reason_key: str) -> str:
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": "[pbz guard] " + REASONS[reason_key]}})


def session_context(root: str) -> str:
    pbz = os.path.join(SKILL_DIR, "bin", "pbz.cmd")
    return (
        f"ZERO-DATA POWER BI WORKSPACE: {os.path.basename(root)}. Company data must never enter your context. "
        f"Load the powerbi-zerodata skill before any Power BI work. CLI: \"{pbz}\". "
        "Edit only the sanitized mirror in pbz-workspace\\ (`pbz pull` first, `pbz push` to apply). "
        "Never open data files, real *.SemanticModel / *.Report folders, .pbi caches or query engines directly. "
        "For external reference files, use pbz consent request; the user must verify no sensitive data before exact-file approval. "
        "The user verifies visuals (`pbz handoff`)."
    )


def _stdin_text() -> str:
    """Hook JSON from stdin. A text stream (tests) has no binary buffer."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return buffer.read().decode("utf-8", errors="replace")
    data = sys.stdin.read()
    return data if isinstance(data, str) else bytes(data).decode("utf-8", errors="replace")


def _fail_closed() -> int:
    """Deny decision plus a non-zero status. Used when the event itself cannot be judged."""
    try:
        sys.stdout.write(deny_json("error"))
    except Exception:
        pass
    return 1


def main() -> int:
    agent = sys.argv[sys.argv.index("--agent") + 1] if "--agent" in sys.argv[:-1] else "claude"
    try:
        raw = _stdin_text()
        event = json.loads(raw) if raw.strip() else {}
    except (ValueError, RecursionError):
        return _fail_closed()
    if not isinstance(event, dict):
        return _fail_closed()
    name = str(event.get("hook_event_name") or "PreToolUse")
    try:
        root = active_root(norm(event.get("cwd") or os.getcwd()))
    except Exception:
        return _fail_closed()
    if name == "SessionStart":
        if root:
            sys.stdout.write(json.dumps({"hookSpecificOutput": {
                "hookEventName": "SessionStart", "additionalContext": session_context(root)}}))
        return 0
    if name != "PreToolUse":
        return 0
    tool = str(event.get("tool_name") or "")
    try:
        verdict = decide(event)
    except Exception:
        verdict = "error"
    if verdict:
        log(root, agent, tool, "deny:" + verdict)
        sys.stdout.write(deny_json(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
