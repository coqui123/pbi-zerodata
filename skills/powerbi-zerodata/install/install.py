"""Point Claude, Codex, and ChatGPT at this repo, then register their guard hooks.

  python install/install.py            apply
  python install/install.py --what-if  show what would change, change nothing
  python install/install.py --mcp      also register the pbz MCP server for Claude Code and Codex
  python install/install.py --uninstall  (also removes the MCP registrations)

Skill folders become Windows directory junctions to this skill directory, so `git pull`
updates the files those agents load. Cursor reads the Claude and Codex skill folders, so
the same junctions cover it. Hook entries stay in each host's settings and run
`pbz/guard.py` through the agents junction. Details: docs/install.md.

Every settings file it edits is backed up first.
"""
from __future__ import annotations

import datetime as dt
import tempfile
import json
import os
import shutil
import subprocess
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
NAME = "powerbi-zerodata"
# Junctions to this skill directory. CANON is the path the hook command runs.
# Claude Code reads .claude; Codex and ChatGPT Codex read .codex; both also
# accept the shared .agents folder. Cursor reads the Claude and Codex folders.
SKILL_LINK_DIRS = (
    os.path.join(".agents", "skills", NAME),
    os.path.join(".claude", "skills", NAME),
    os.path.join(".codex", "skills", NAME),
)
LINKS = [os.path.join(HOME, rel) for rel in SKILL_LINK_DIRS]
CANON = LINKS[0]
CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
CODEX_HOOKS = os.path.join(HOME, ".codex", "hooks.json")
RULE_FILES = [os.path.join(HOME, ".claude", "CLAUDE.md"), os.path.join(HOME, ".codex", "AGENTS.md")]
CLAUDE_JSON = os.path.join(HOME, ".claude.json")
CODEX_CONFIG = os.path.join(HOME, ".codex", "config.toml")
MCP_NAME = "pbz"
BEGIN, END = "<!-- powerbi-zerodata:begin -->", "<!-- powerbi-zerodata:end -->"
DENY_RULES = [
    "Read(//**/.pbi/**)", "Read(//**/*.abf)", "Read(//**/*.abf.stale)",
    "Read(~/Microsoft/Power BI Desktop Store App/**)", "Read(~/AppData/Local/pbz/**)", "Read(~/.pbz-private/**)",
    "Read(//**/*.measurekiller)", "Read(~/AppData/Roaming/Measure Killer/**)",
]
RULE_BLOCK = f"""{BEGIN}
# Power BI zero-data rule (always on)

If the workspace or any path you touch holds a Power BI project (*.pbip, *.SemanticModel,
*.Report), company data must never enter your context. Load the `{NAME}` skill first. Edit only
the sanitized mirror in pbz-workspace\\ through `pbz pull` / `pbz push`. Never open data files,
real definition folders, .pbi caches, or query a Power BI engine or REST API directly; use pbz.
Never screenshot Power BI. Never write data values (amounts, counts, names) into memory or notes.
External files need exact-file `pbz consent` approval after the user verifies no sensitive data;
read them only through the consent broker. Real model/report/cache/private paths stay prohibited.
{END}
"""


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def python_exe() -> str:
    return sys.executable.replace("\\", "/")


def hook_cmd(agent: str) -> str:
    guard = os.path.join(CANON, "pbz", "guard.py").replace("\\", "/")
    for p in (python_exe(), guard):
        if " " in p:
            raise SystemExit(f"path contains a space, hook command would need quoting: {p}")
    return f"{python_exe()} -I -S {guard} --agent {agent}"


def _refuse_spaced_paths() -> None:
    """A space in Python or the skill path would make the hook command unquoted. Check before any junction."""
    guard = os.path.join(CANON, "pbz", "guard.py").replace("\\", "/")
    skill = SKILL.replace("\\", "/")
    for p in (python_exe(), skill, guard):
        if " " in p:
            raise SystemExit(f"path contains a space, hook command would need quoting: {p}")


def backup(path: str, what_if: bool) -> None:
    if os.path.exists(path) and not what_if:
        fd, dest = tempfile.mkstemp(prefix=os.path.basename(path) + ".bak-pbz-" + stamp() + "-", dir=os.path.dirname(path))
        os.close(fd)
        shutil.copy2(path, dest)


def load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_json(path: str, data: dict, what_if: bool) -> None:
    if what_if:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp-pbz"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def is_ours(entry: dict) -> bool:
    return any("powerbi-zerodata" in str(h.get("command", "")) and "guard.py" in str(h.get("command", ""))
               for h in entry.get("hooks", []))


def merge_hooks(data: dict, agent: str, uninstall: bool, owned: dict | None = None) -> dict:
    owned = owned if owned is not None else {}
    rec = owned.get("hooks", {})
    if uninstall:
        hooks = data.get("hooks", {})
        for addition in rec.get("additions", []):
            event, child = addition["event"], addition["child"]
            entries = hooks.get(event, [])
            for entry in reversed(entries):
                children = entry.get("hooks", [])
                if child in children:
                    children.remove(child)
                    if not children and entry == addition["shell"]:
                        entries.remove(entry)
                    break
            if not entries and event in rec.get("events", []):
                hooks.pop(event, None)
        if not hooks and rec.get("container"):
            data.pop("hooks", None)
        owned.pop("hooks", None)
        return data
    if not rec:
        rec = {"container": "hooks" not in data, "events": [], "additions": []}
        owned["hooks"] = rec
    hooks = data.setdefault("hooks", {})
    for event, matcher in (("PreToolUse", ".*"), ("SessionStart", None)):
        child = {"type": "command", "command": hook_cmd(agent), "timeout": 15}
        # Existing children, including mixed groups, belong to the host.
        if any(child in e.get("hooks", []) for e in hooks.get(event, [])):
            continue
        if event not in hooks:
            rec["events"].append(event)
        entry = {"hooks": [child]}
        if matcher:
            entry["matcher"] = matcher
        hooks.setdefault(event, []).append(entry)
        rec["additions"].append({"event": event, "child": child,
                                  "shell": {**entry, "hooks": []}})
    if not rec["additions"]:
        owned.pop("hooks", None)
    return data


def merge_denies(data: dict, uninstall: bool, owned: dict | None = None) -> dict:
    owned = owned if owned is not None else {}
    rec = owned.get("denies", {})
    if uninstall:
        perms = data.get("permissions", {})
        deny = perms.get("deny", [])
        for rule in rec.get("additions", []):
            if rule in deny:
                deny.remove(rule)
        if not deny and rec.get("list"):
            perms.pop("deny", None)
        if not perms and rec.get("container"):
            data.pop("permissions", None)
        owned.pop("denies", None)
        return data
    if not rec:
        rec = {"container": "permissions" not in data,
               "list": "deny" not in data.get("permissions", {}), "additions": []}
        owned["denies"] = rec
    deny = data.setdefault("permissions", {}).setdefault("deny", [])
    for rule in DENY_RULES:
        if rule not in deny:
            deny.append(rule)
            rec["additions"].append(rule)
    if not rec["additions"]:
        owned.pop("denies", None)
    return data


def settings_step(path: str, agent: str, what_if: bool, uninstall: bool, denies: bool = False) -> bool:
    """Ownership covers hook children and deny rules only; absent records remove nothing."""
    record_path = path + ".pbz-owned.json"
    existed = os.path.exists(path)
    before = load_json(path)
    previous = load_json(record_path)
    owned = json.loads(json.dumps(previous))
    new = merge_hooks(json.loads(json.dumps(before)), agent, uninstall, owned)
    if denies:
        new = merge_denies(new, uninstall, owned)
    created_file = previous.get("created_file", False)
    if uninstall:
        owned.pop("created_file", None)
    elif not existed and new != before:
        owned["created_file"] = True
    if new != before:
        backup(path, what_if)
        if uninstall and created_file and not new:
            if not what_if:
                os.remove(path)
        else:
            save_json(path, new, what_if)
    if owned != previous and not what_if:
        if owned:
            save_json(record_path, owned, False)
        elif os.path.exists(record_path):
            os.remove(record_path)
    return new != before


def junction(link: str, what_if: bool, uninstall: bool) -> str:
    target = os.path.realpath(SKILL)
    if os.path.lexists(link):
        if os.path.realpath(link) == target:
            if uninstall and not what_if:
                os.rmdir(link)  # removes the junction only
                return f"removed {link}"
            return f"ok {link}"
        if not uninstall:
            return f"SKIP {link} exists and points elsewhere; remove it manually"
        return f"left {link} (not ours)"
    if uninstall:
        return f"absent {link}"
    if not what_if:
        os.makedirs(os.path.dirname(link), exist_ok=True)
        subprocess.run(["cmd", "/c", "mklink", "/J", link, target], check=True, capture_output=True)
    return f"junction {link} -> {target}"


def rule_block(path: str, what_if: bool, uninstall: bool) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    if BEGIN in text and END in text:
        head, rest = text.split(BEGIN, 1)
        tail = rest.split(END, 1)[1].lstrip("\n")
        new = head.rstrip("\n") + ("\n\n" + RULE_BLOCK if not uninstall else "\n") + (("\n" + tail) if tail else "")
    else:
        if uninstall:
            return f"absent {path}"
        new = text.rstrip("\n") + "\n\n" + RULE_BLOCK
    if new == text:
        return f"ok {path}"
    backup(path, what_if)
    if not what_if:
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new)
    return f"{'removed block from' if uninstall else 'rule block ->'} {path}"


def mcp_args() -> list[str]:
    return ["-I", os.path.join(CANON, "pbz", "cli.py").replace("\\", "/"), "mcp"]


def _ours(args) -> bool:
    return isinstance(args, list) and any(str(a).replace("\\", "/").endswith("pbz/cli.py") for a in args) and "mcp" in args


def merge_claude_mcp(data: dict, uninstall: bool) -> tuple[dict, str]:
    """mcpServers.pbz in ~/.claude.json (user scope, like `claude mcp add --scope user`)."""
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    cur = servers.get(MCP_NAME)
    if cur is not None and not _ours(cur.get("args") if isinstance(cur, dict) else None):
        return data, f"SKIP mcpServers.{MCP_NAME} exists and is not ours"
    if uninstall:
        if cur is None:
            return data, "absent"
        servers.pop(MCP_NAME)
        if servers:
            data["mcpServers"] = servers
        else:
            data.pop("mcpServers", None)
        return data, "removed"
    entry = {"type": "stdio", "command": python_exe(), "args": mcp_args(), "env": {}}
    if cur == entry:
        return data, "ok"
    data["mcpServers"] = {**servers, MCP_NAME: entry}
    return data, "registered"


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)  # a JSON string is a valid TOML basic string


def merge_codex_toml(text: str, uninstall: bool) -> tuple[str, str]:
    """[mcp_servers.pbz] in ~/.codex/config.toml, edited as text (stdlib has no TOML writer)."""
    import re
    header = re.compile(r'^\s*\[\s*mcp_servers\s*\.\s*(?:pbz|"pbz")\s*(?:\.[^\]]*)?\]\s*(?:#.*)?$')
    lines = text.splitlines(keepends=True)
    keep, section = [], []
    in_ours = False
    for ln in lines:
        if ln.lstrip().startswith("["):
            in_ours = bool(header.match(ln.rstrip("\r\n")))
        (section if in_ours else keep).append(ln)
    if section:
        body = "".join(section).replace("\\\\", "/").replace("\\", "/")
        if not ("pbz/cli.py" in body and '"mcp"' in body):
            return text, f"SKIP [mcp_servers.{MCP_NAME}] exists and is not ours"
    if uninstall:
        if not section:
            return text, "absent"
        rest = "".join(keep).rstrip("\n")
        return (rest + "\n" if rest else ""), "removed"
    block = (f"[mcp_servers.{MCP_NAME}]\n"
             f"command = {_toml_str(python_exe())}\n"
             f"args = [{', '.join(_toml_str(a) for a in mcp_args())}]\n")
    base = "".join(keep).rstrip("\n")
    new = (base + "\n\n" if base else "") + block
    if section and "".join(section).strip() == block.strip():
        return text, "ok"
    try:
        import tomllib
        tomllib.loads(new)
    except ImportError:
        pass
    except Exception:
        return text, "SKIP the result would not parse as TOML; add the block by hand"
    return new, "registered" if not section else "updated"


def mcp_step(what_if: bool, uninstall: bool) -> list[str]:
    out = []
    data = load_json(CLAUDE_JSON)
    new, how = merge_claude_mcp(json.loads(json.dumps(data)), uninstall)
    if new != data:
        backup(CLAUDE_JSON, what_if)
        save_json(CLAUDE_JSON, new, what_if)
    out.append(f"{how} mcpServers.{MCP_NAME} -> {CLAUDE_JSON}")
    try:
        with open(CODEX_CONFIG, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        text = ""
    new_text, how = merge_codex_toml(text, uninstall)
    if new_text != text:
        backup(CODEX_CONFIG, what_if)
        if not what_if:
            os.makedirs(os.path.dirname(CODEX_CONFIG), exist_ok=True)
            with open(CODEX_CONFIG, "w", encoding="utf-8", newline="\n") as f:
                f.write(new_text)
    out.append(f"{how} [mcp_servers.{MCP_NAME}] -> {CODEX_CONFIG}")
    return out


def main() -> int:
    what_if = "--what-if" in sys.argv
    uninstall = "--uninstall" in sys.argv
    with_mcp = "--mcp" in sys.argv
    if not uninstall:
        _refuse_spaced_paths()
    print(("WHAT-IF: " if what_if else "") + ("uninstall" if uninstall else "install") + f" {NAME} from {SKILL}")
    skipped = False
    for link in LINKS:
        line = junction(link, what_if, uninstall)
        print(" ", line)
        if line.startswith("SKIP "):
            skipped = True
    if skipped and not uninstall:
        return 1

    settings_step(CLAUDE_SETTINGS, "claude", what_if, uninstall, denies=True)
    settings_step(CODEX_HOOKS, "codex", what_if, uninstall)

    for path in RULE_FILES:
        print(" ", rule_block(path, what_if, uninstall))
    if with_mcp or uninstall:
        for line in mcp_step(what_if, uninstall):
            print(" ", line)
    if not uninstall:
        print(f"  hook command: {hook_cmd('claude')}")
        if with_mcp:
            print(f"  mcp server: {python_exe()} {' '.join(mcp_args())}  (Claude Code alternative: "
                  f"claude mcp add --scope user {MCP_NAME} -- {python_exe()} {' '.join(mcp_args())})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
