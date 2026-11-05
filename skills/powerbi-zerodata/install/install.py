"""Install powerbi-zerodata for Claude Code and Codex (idempotent).

  python install/install.py            apply
  python install/install.py --what-if  show what would change, change nothing
  python install/install.py --uninstall

Steps: junctions into ~/.agents/skills and ~/.claude/skills; guard hooks in ~/.claude/settings.json
and ~/.codex/hooks.json; Read deny rules for Power BI caches; a short rule block in
~/.claude/CLAUDE.md and ~/.codex/AGENTS.md. Every file it edits is backed up first.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOME = os.path.expanduser("~")
NAME = "powerbi-zerodata"
LINKS = [os.path.join(HOME, ".agents", "skills", NAME), os.path.join(HOME, ".claude", "skills", NAME)]
CANON = LINKS[0]
CLAUDE_SETTINGS = os.path.join(HOME, ".claude", "settings.json")
CODEX_HOOKS = os.path.join(HOME, ".codex", "hooks.json")
RULE_FILES = [os.path.join(HOME, ".claude", "CLAUDE.md"), os.path.join(HOME, ".codex", "AGENTS.md")]
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
{END}
"""


def stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def python_exe() -> str:
    return sys.executable.replace("\\", "/")


def hook_cmd(agent: str) -> str:
    guard = os.path.join(CANON, "pbz", "guard.py").replace("\\", "/")
    for p in (python_exe(), guard):
        if " " in p:
            raise SystemExit(f"path contains a space, hook command would need quoting: {p}")
    return f"{python_exe()} -I -S {guard} --agent {agent}"


def backup(path: str, what_if: bool) -> None:
    if os.path.exists(path) and not what_if:
        shutil.copy2(path, f"{path}.bak-pbz-{stamp()}")


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


def merge_hooks(data: dict, agent: str, uninstall: bool) -> dict:
    hooks = data.setdefault("hooks", {})
    for event, matcher in (("PreToolUse", ".*"), ("SessionStart", None)):
        entries = [e for e in hooks.get(event, []) if not is_ours(e)]
        if not uninstall:
            entry = {"hooks": [{"type": "command", "command": hook_cmd(agent), "timeout": 15}]}
            if matcher:
                entry = {"matcher": matcher, **entry}
            entries.append(entry)
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if not hooks:
        data.pop("hooks", None)
    return data


def merge_denies(data: dict, uninstall: bool) -> dict:
    perms = data.setdefault("permissions", {})
    deny = [d for d in perms.get("deny", []) if d not in DENY_RULES]
    if not uninstall:
        deny += DENY_RULES
    if deny:
        perms["deny"] = deny
    else:
        perms.pop("deny", None)
    return data


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


def main() -> int:
    what_if = "--what-if" in sys.argv
    uninstall = "--uninstall" in sys.argv
    print(("WHAT-IF: " if what_if else "") + ("uninstall" if uninstall else "install") + f" {NAME} from {SKILL}")
    for link in LINKS:
        print(" ", junction(link, what_if, uninstall))

    data = load_json(CLAUDE_SETTINGS)
    new = merge_denies(merge_hooks(json.loads(json.dumps(data)), "claude", uninstall), uninstall)
    if new != data:
        backup(CLAUDE_SETTINGS, what_if)
        save_json(CLAUDE_SETTINGS, new, what_if)
        print(f"  hooks + deny rules -> {CLAUDE_SETTINGS}")
    else:
        print(f"  ok {CLAUDE_SETTINGS}")

    data = load_json(CODEX_HOOKS)
    new = merge_hooks(json.loads(json.dumps(data)), "codex", uninstall)
    if new != data:
        backup(CODEX_HOOKS, what_if)
        save_json(CODEX_HOOKS, new, what_if)
        print(f"  hooks -> {CODEX_HOOKS}  (trust them once in Codex with /hooks)")
    else:
        print(f"  ok {CODEX_HOOKS}")

    for path in RULE_FILES:
        print(" ", rule_block(path, what_if, uninstall))
    if not uninstall:
        print(f"  hook command: {hook_cmd('claude')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
