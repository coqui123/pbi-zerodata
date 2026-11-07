"""pbz guard: PreToolUse / SessionStart hook shared by Claude Code and Codex.

Keeps company data out of the agent's context in Power BI workspaces:
- real *.SemanticModel / *.Report definition folders (the agent edits the pbz-workspace mirror),
- data files, Desktop caches and app data, the pbz private store,
- engine / REST / data-reading commands and scripts,
- screenshots, UI automation and Power BI service pages.

Contract: stdin = hook JSON, stdout = deny JSON (or nothing to allow), exit code 0.
Deny reasons never echo tool input. Stdlib only; run as `python -I -S guard.py --agent <name>`.
"""
from __future__ import annotations

import base64
import datetime as _dt
import json
import os
import re
import sys

POLICY_REL = os.path.join(".pbz", "policy.json")
MIRROR_DIR = "pbz-workspace"
MAX_WALK_ENTRIES = 6000
MAX_SCRIPT_BYTES = 2_000_000

HOME = os.path.normcase(os.path.normpath(os.path.expanduser("~"))).replace("\\", "/")
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

SAFE_PATH_VERBS = {
    "ls", "dir", "gci", "get-childitem", "test-path", "rm", "del", "erase", "rmdir", "rd", "remove-item",
    "ri", "mkdir", "md", "new-item", "ni", "start-process", "start", "saps", "explorer", "explorer.exe",
    "stat", "du", "get-item", "gi", "resolve-path", "rvpa", "split-path", "join-path", "tree", "attrib",
    "cd", "chdir", "set-location", "sl", "pushd", "push-location", "popd", "pop-location",
}
CONTENT_SEARCH_VERBS = {"rg", "grep", "egrep", "fgrep", "findstr", "select-string", "sls", "ag", "ack"}
CD_VERBS = {"cd", "chdir", "set-location", "sl", "pushd", "push-location"}
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
    return os.path.normcase(os.path.normpath(p)).replace("\\", "/")


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


def read_policy_file(d: str) -> dict | None:
    try:
        with open(os.path.join(d, POLICY_REL), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {}


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


def find_root(path: str) -> str | None:
    key = norm(path)
    if key in _ROOT:
        return _ROOT[key]
    root = next((d for d in ancestors(key) if is_workspace_root(d)), None)
    _ROOT[key] = root
    return root


def synthetic(path: str) -> bool:
    key = norm(path)
    if key not in _SYN:
        _SYN[key] = any((read_policy_file(d) or {}).get("synthetic") is True for d in ancestors(key))
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


def global_denied(path: str) -> bool:
    for d in GLOBAL_DENY_DIRS:
        nd = norm(d)
        if path == nd or path.startswith(nd + "/"):
            return True
    return any(re.search(p, path) for p in GLOBAL_DENY_PATTERNS)


def classify_path(path: str) -> str | None:
    """Reason key if content access to path is denied, else None."""
    if global_denied(path):
        return "global"
    root = active_root(path)
    if not root:
        return None
    r = rel(path, root)
    parts = r.split("/")
    if parts[0] == MIRROR_DIR:
        return None
    pol = load_policy(root)
    if any(glob_to_regex(g).match(r) for g in pol.get("allow", [])):
        return None
    if any(p.endswith(".semanticmodel") or p.endswith(".report") for p in parts):
        if parts[-1] not in SAFE_DEFINITION_FILES:
            return "definition"
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


def is_pbz(toks: list[str]) -> bool:
    v = verb_of(toks)
    if v in ("pbz", "pbz.cmd", "pbz.ps1", "pbz.exe"):
        return True
    if v.startswith("python") or v in ("py", "py.exe"):
        for t in toks[verb_index(toks) + 1:]:
            if not t.startswith("-"):
                return t.replace("\\", "/").lower().endswith("powerbi-zerodata/pbz/cli.py")
    return False


def looks_like_path(t: str) -> bool:
    if not t or len(t) > 400 or re.match(r"^[a-z][a-z0-9+.-]*://", t, re.I):
        return False
    if t.startswith("-"):
        return False
    if re.match(r"^[a-zA-Z]:[\\/]", t) or t.startswith(("/", "./", "../", ".\\", "..\\", "~")):
        return True
    return "/" in t or "\\" in t or bool(re.search(r"\.[A-Za-z0-9]{1,6}$", t))


def interpreter_targets(toks: list[str]) -> tuple[list[str], list[str]]:
    """(script files, inline code) run by this pipe part."""
    i = verb_index(toks)
    if i >= len(toks):
        return [], []
    v, rest = verb_of(toks), toks[i + 1:]
    files: list[str] = []
    inline: list[str] = []

    def first_positional(stop_flags=(), code_flags=()):
        for j, t in enumerate(rest):
            tl = t.lower()
            if tl in code_flags and j + 1 < len(rest):
                inline.append(" ".join(rest[j + 1:]))
                return
            if tl in stop_flags:
                return
            if not t.startswith("-") and not (t.startswith("/") and len(t) <= 3):
                files.append(t)
                return

    if v.startswith("python") or v in ("py", "py.exe"):
        first_positional(stop_flags=("-m",), code_flags=("-c",))
    elif v in ("pwsh", "pwsh.exe", "powershell", "powershell.exe"):
        for j, t in enumerate(rest):
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
    elif v in ("node", "node.exe", "deno", "bun"):
        first_positional(code_flags=("-e", "--eval", "-p"))
    elif v in ("bash", "sh", "zsh", "cmd", "cmd.exe"):
        first_positional(code_flags=("-c", "/c", "/k"))
    elif re.search(r"\.(ps1|py|js|bat|cmd|sh)$", v):
        files.append(toks[i])
    return files, inline


def hits(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text) for p in patterns)


def script_bad(text: str, pol: dict) -> bool:
    if hits(text, ENGINE_PATTERNS + DATA_READ_PATTERNS + [re.escape(h) for h in pol.get("sensitive_hosts", [])]):
        return True
    if DEFINITION_REF.search(text) and MIRROR_DIR not in text.lower():
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


def check_shell(cmd: str, cwd: str) -> str | None:
    cmd, bodies = split_heredocs(cmd)
    for lead, body in bodies:  # a heredoc is code only when it feeds an interpreter; otherwise it is text
        lead_root = active_root(cwd)
        for part in PIPE_SPLIT.split(SEG_SPLIT.split(lead)[-1]):
            if verb_of(tokens(part)) in INTERPRETERS:
                if not synthetic(cwd) and hits(body, ENGINE_PATTERNS):
                    return "engine"
                if lead_root and script_bad(body, load_policy(lead_root)):
                    return "script"
    cur = cwd
    for seg in segments(cmd):
        for part in seg:
            toks = tokens(part)
            if not toks:
                continue
            v = verb_of(toks)
            if is_pbz(toks):
                if re.search(r"(?i)\badmin\b", part):
                    return "admin"
                continue
            if v in CD_VERBS and len(toks) > verb_index(toks) + 1:
                cur = norm(toks[-1], cur)
                continue
            root = active_root(cur)
            pol = load_policy(root)
            if not synthetic(cur) and hits(part, ENGINE_PATTERNS):
                return "engine"
            if root:
                if hits(part, DATA_READ_PATTERNS + [re.escape(h) for h in pol.get("sensitive_hosts", [])]):
                    return "engine"
                if any(s.lower() in part.lower() for s in pol.get("deny_scripts", [])):
                    return "script"
            denied, dir_targets, has_path = None, [], False
            for t in toks[verb_index(toks) + 1:]:
                if "*" in t and DATA_EXT.search(t):
                    base = norm(os.path.dirname(t) or ".", cur)
                    if active_root(base) or root:
                        denied = denied or "data_file"
                    continue
                if not looks_like_path(t) and not (len(t) < 260 and os.path.exists(norm(t, cur))):
                    continue
                has_path = True
                p = norm(t, cur)
                why = classify_path(p)
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
            for f in files:
                p = norm(f, cur)
                why = classify_path(p)
                if why:
                    return why
                sroot = active_root(p) or root
                if sroot or not synthetic(p):
                    try:
                        if os.path.getsize(p) <= MAX_SCRIPT_BYTES:
                            with open(p, encoding="utf-8", errors="replace") as fh:
                                text = fh.read()
                            if (sroot and script_bad(text, load_policy(sroot))) or hits(text, ENGINE_PATTERNS):
                                return "script"
                    except OSError:
                        pass
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
            (".agents", "skills", "powerbi-zerodata"), (".claude", "skills", "powerbi-zerodata"))]
    return any(path == g or path.startswith(g + "/") for g in guarded) or path.endswith("/.claude/settings.json") \
        or path.endswith("/.claude/settings.local.json") or path.endswith("/.codex/hooks.json")


def is_memory_path(path: str) -> bool:
    return bool(re.search(r"/\.claude/projects/[^/]+/memory/", path)) or "/.codex/memories/" in path


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
    for key in ("allow", "category_columns", "reveal_columns"):
        if not set(new.get(key, [])) <= set(old.get(key, [])):
            return True
    ob, nb = old.get("budgets", {}) or {}, new.get("budgets", {}) or {}
    return any(isinstance(v, (int, float)) and isinstance(ob.get(k), (int, float)) and v > ob[k] for k, v in nb.items())


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


def check_write(p: str, tool: str, ti: dict, session_root: str | None) -> str | None:
    if session_root and self_protected(p):
        return "selfprotect"
    if p.endswith("/.pbz/policy.json") and active_root(p):
        if tool == "apply_patch":
            return "policy"
        text = edited_text(p, ti)
        try:
            new = json.loads(text) if text is not None else None
        except ValueError:
            new = None
        if not isinstance(new, dict) or policy_loosened(load_policy(find_root(p)), new):
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
        exts = re.findall(r"\.([a-z0-9]+)", glob)
        if exts and re.fullmatch(r"[*/.{},a-z0-9]+", glob):
            name_filter = re.compile(r"\.(" + "|".join(map(re.escape, exts)) + r")$")
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
    elif FILEISH_TOOLS.search(tool):
        paths = [norm(s, cwd) for s in string_values(ti) if looks_like_path(s)]
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


def main() -> int:
    agent = sys.argv[sys.argv.index("--agent") + 1] if "--agent" in sys.argv[:-1] else "claude"
    try:
        raw = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        event = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    if not isinstance(event, dict):
        return 0
    name = str(event.get("hook_event_name") or "PreToolUse")
    try:
        root = active_root(norm(event.get("cwd") or os.getcwd()))
    except Exception:  # cannot tell where we are: never block unrelated work
        return 0
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
        verdict = "error" if root else None
    if verdict:
        log(root, agent, tool, "deny:" + verdict)
        sys.stdout.write(deny_json(verdict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
