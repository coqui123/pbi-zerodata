"""Decision-table tests for pbz/guard.py, run through the real hook entry point.

Run: python tests/test_guard.py
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(os.path.dirname(HERE), "pbz", "guard.py")


def write(path: str, text: str = "") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def build(tmp: str) -> dict:
    ws = os.path.join(tmp, "ws")
    syn = os.path.join(tmp, "syn")
    other = os.path.join(tmp, "other")
    write(os.path.join(ws, "Demo.pbip"), "{}")
    write(os.path.join(ws, "Demo.SemanticModel", "definition", "tables", "T.tmdl"), "table T")
    write(os.path.join(ws, "Demo.SemanticModel", "definition.pbism"), "{}")
    write(os.path.join(ws, "Demo.SemanticModel", ".pbi", "cache.abf"), "x")
    write(os.path.join(ws, "Demo.Report", "definition", "pages", "p1", "page.json"), "{}")
    write(os.path.join(ws, "pbz-workspace", "Demo.SemanticModel", "definition", "tables", "T.tmdl"), "table T")
    write(os.path.join(ws, "data.xlsx"), "x")
    write(os.path.join(ws, "notes.md"), "notes")
    write(os.path.join(ws, "shot.png"), "x")
    write(os.path.join(ws, "secret-report.md"), "x")
    write(os.path.join(ws, "_diag", "odata_pull.py"), "print('x')")
    write(os.path.join(ws, "scripts", "ok.py"), "print('hi')")
    write(os.path.join(ws, "scripts", "bad.py"), "import openpyxl\n")
    write(os.path.join(ws, "scripts", "defread.py"), "open('Demo.SemanticModel/definition/model.tmdl').read()\n")
    write(os.path.join(ws, ".pbz", "policy.json"), json.dumps({
        "deny": ["secret-report.md"], "deny_scripts": ["odata_pull.py"],
        "sensitive_hosts": ["acme.pvcloud.com"], "budgets": {"session_checks": 300}}))
    write(os.path.join(syn, ".pbz", "policy.json"), json.dumps({"synthetic": True}))
    write(os.path.join(syn, "tests", "fixture", "Canary.pbip"), "{}")
    write(os.path.join(syn, "tests", "fixture", "Canary.SemanticModel", "definition", "x.tmdl"), "table X")
    write(os.path.join(other, "readme.md"), "hi")
    return {"ws": ws, "syn": syn, "other": other}


def run(event: dict, agent: str = "claude") -> dict | None:
    p = subprocess.run([sys.executable, "-I", "-S", GUARD, "--agent", agent],
                       input=json.dumps(event).encode(), capture_output=True, timeout=60)
    assert p.returncode == 0, p.stderr.decode(errors="replace")
    out = p.stdout.decode().strip()
    return json.loads(out) if out else None


def verdict(out: dict | None) -> str | None:
    if not out:
        return None
    hso = out.get("hookSpecificOutput", {})
    if hso.get("permissionDecision") == "deny":
        reason = hso["permissionDecisionReason"]
        return reason
    return None


def pre(tool: str, tool_input: dict, cwd: str) -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": tool_input, "cwd": cwd,
            "session_id": "t", "transcript_path": None}


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="pbzguard-")
    d = build(tmp)
    ws, syn, other = d["ws"], d["syn"], d["other"]
    j = os.path.join
    home = os.path.expanduser("~")
    mem = j(home, ".claude", "projects", "X-test", "memory", "note.md")
    enc = base64.b64encode("Get-Content .\\data.xlsx".encode("utf-16-le")).decode()

    cases = [
        # (name, event, expected reason fragment or None)
        ("read real tmdl", pre("Read", {"file_path": j(ws, "Demo.SemanticModel", "definition", "tables", "T.tmdl")}, ws), "definition files"),
        ("read mirror tmdl", pre("Read", {"file_path": j(ws, "pbz-workspace", "Demo.SemanticModel", "definition", "tables", "T.tmdl")}, ws), None),
        ("read pbism", pre("Read", {"file_path": j(ws, "Demo.SemanticModel", "definition.pbism")}, ws), None),
        ("read pbip", pre("Read", {"file_path": j(ws, "Demo.pbip")}, ws), None),
        ("read xlsx", pre("Read", {"file_path": j(ws, "data.xlsx")}, ws), "Data-bearing"),
        ("read cache.abf", pre("Read", {"file_path": j(ws, "Demo.SemanticModel", ".pbi", "cache.abf")}, ws), "caches"),
        ("read other md", pre("Read", {"file_path": j(other, "readme.md")}, other), None),
        ("read abf anywhere", pre("Read", {"file_path": j(other, "x.abf")}, other), "caches"),
        ("policy deny glob", pre("Read", {"file_path": j(ws, "secret-report.md")}, ws), "Data-bearing"),
        ("read notes", pre("Read", {"file_path": j(ws, "notes.md")}, ws), None),
        ("read png", pre("Read", {"file_path": j(ws, "shot.png")}, ws), "Data-bearing"),
        ("relative real tmdl", pre("Read", {"file_path": "Demo.SemanticModel/definition/tables/T.tmdl"}, ws), "definition files"),
        ("gitbash path", pre("Read", {"file_path": "/" + ws[0].lower() + ws[2:].replace("\\", "/") + "/data.xlsx"}, ws), "Data-bearing"),
        ("grep ws root", pre("Grep", {"pattern": "x", "path": ws}, ws), "Content search"),
        ("grep mirror", pre("Grep", {"pattern": "x", "path": j(ws, "pbz-workspace")}, ws), None),
        ("grep py only", pre("Grep", {"pattern": "x", "path": ws, "glob": "*.py"}, ws), None),
        ("grep json", pre("Grep", {"pattern": "x", "path": ws, "glob": "*.json"}, ws), "Content search"),
        ("grep other", pre("Grep", {"pattern": "x", "path": other}, other), None),
        ("cat real tmdl", pre("Bash", {"command": "cat Demo.SemanticModel/definition/tables/T.tmdl"}, ws), "definition files"),
        ("ls real", pre("Bash", {"command": "ls Demo.SemanticModel/definition"}, ws), None),
        ("python ok", pre("Bash", {"command": "python scripts/ok.py"}, ws), None),
        ("python bad", pre("Bash", {"command": "python scripts/bad.py"}, ws), "Script blocked"),
        ("python defread", pre("Bash", {"command": "python scripts/defread.py"}, ws), "Script blocked"),
        ("deny_scripts", pre("Bash", {"command": "python _diag/odata_pull.py"}, ws), "Script blocked"),
        ("gc xlsx", pre("PowerShell", {"command": "Get-Content .\\data.xlsx"}, ws), "Data-bearing"),
        ("invoke-ascmd", pre("PowerShell", {"command": "Invoke-ASCmd -Query 'x'"}, ws), "engine"),
        ("rest from anywhere", pre("Bash", {"command": "curl https://api.powerbi.com/v1.0/myorg/datasets"}, other), "engine"),
        ("pbz pull", pre("Bash", {"command": "pbz pull"}, ws), None),
        ("pbz cmd path", pre("PowerShell", {"command": "& \"C:/x/powerbi-zerodata/bin/pbz.cmd\" check spec.json"}, ws), None),
        ("pbz cli.py", pre("Bash", {"command": "python C:/x/powerbi-zerodata/pbz/cli.py describe T"}, ws), None),
        ("pbz admin", pre("Bash", {"command": "pbz admin reset"}, ws), "user in their own terminal"),
        ("pbz then cat", pre("Bash", {"command": "pbz pull && cat data.xlsx"}, ws), "Data-bearing"),
        ("synthetic cd", pre("Bash", {"command": f"cd \"{syn}\" && dotnet add package Microsoft.AnalysisServices.AdomdClient"}, ws), None),
        ("engine pkg no cd", pre("Bash", {"command": "dotnet add package Microsoft.AnalysisServices.AdomdClient"}, ws), "engine"),
        ("rg root", pre("Bash", {"command": "rg -n foo"}, ws), "Content search"),
        ("rg mirror", pre("Bash", {"command": "rg -n foo pbz-workspace"}, ws), None),
        ("python -c csv", pre("Bash", {"command": "python -c \"import pandas as pd; pd.read_csv('a.csv')\""}, ws), "engine"),
        ("cat glob csv", pre("Bash", {"command": "cat *.csv"}, ws), "Data-bearing"),
        ("encoded ps", pre("PowerShell", {"command": f"powershell -EncodedCommand {enc}"}, ws), "Script blocked"),
        ("screenshot ws", pre("mcp__computer-use__screenshot", {}, ws), "Screenshots"),
        ("screenshot other", pre("mcp__computer-use__screenshot", {}, other), None),
        ("browser read ws", pre("mcp__Claude_Browser__get_page_text", {}, ws), "Screenshots"),
        ("webfetch service", pre("WebFetch", {"url": "https://app.powerbi.com/groups/x", "prompt": "p"}, other), "service"),
        ("webfetch docs", pre("WebFetch", {"url": "https://learn.microsoft.com/power-bi/", "prompt": "p"}, ws), None),
        ("webfetch policy host", pre("WebFetch", {"url": "https://acme.pvcloud.com/odata/x", "prompt": "p"}, ws), "service"),
        ("pbi mcp", pre("mcp__powerbi-modeling__dax_query_operations", {"query": "x"}, other), "MCP"),
        ("policy loosen", pre("Write", {"file_path": j(ws, ".pbz", "policy.json"), "content": json.dumps({"deny": []})}, ws), "loosen"),
        ("policy tighten", pre("Write", {"file_path": j(ws, ".pbz", "policy.json"), "content": json.dumps({
            "deny": ["secret-report.md", "more.md"], "deny_scripts": ["odata_pull.py"],
            "sensitive_hosts": ["acme.pvcloud.com"], "budgets": {"session_checks": 100}})}, ws), None),
        ("self protect", pre("Edit", {"file_path": GUARD, "old_string": "a", "new_string": "b"}, ws), "read-only"),
        ("self protect off-ws", pre("Edit", {"file_path": GUARD, "old_string": "zzzz-not-there", "new_string": "b"}, other), None),
        ("memory data", pre("Write", {"file_path": mem, "content": "cards showed $1,104 before"}, ws), "Memory notes"),
        ("memory clean", pre("Write", {"file_path": mem, "content": "Card measure repointed to Total Cost"}, ws), None),
        ("agent prompt", pre("Agent", {"prompt": "never read *.xlsx or data.xlsx", "description": "x"}, ws), None),
        ("write real pbir", pre("Write", {"file_path": j(ws, "Demo.Report", "definition", "pages", "p1", "page.json"), "content": "{}"}, ws), "definition files"),
        ("edit mirror", pre("Edit", {"file_path": j(ws, "pbz-workspace", "Demo.SemanticModel", "definition", "tables", "T.tmdl"), "old_string": "T", "new_string": "U"}, ws), None),
        ("synthetic fixture read", pre("Read", {"file_path": j(syn, "tests", "fixture", "Canary.SemanticModel", "definition", "x.tmdl")}, syn), None),
        ("codex argv", pre("Bash", {"command": ["powershell.exe", "-Command", "Get-Content Demo.SemanticModel/definition/tables/T.tmdl"]}, ws), "definition files"),
        ("codex patch real", pre("apply_patch", {"command": "*** Begin Patch\n*** Update File: Demo.SemanticModel/definition/tables/T.tmdl\n@@\n-a\n+b\n*** End Patch"}, ws), "definition files"),
        ("codex patch mirror", pre("apply_patch", {"command": "*** Begin Patch\n*** Update File: pbz-workspace/Demo.SemanticModel/definition/tables/T.tmdl\n@@\n-a\n+b\n*** End Patch"}, ws), None),
        ("codex view_image", pre("view_image", {"path": j(ws, "shot.png")}, ws), "Data-bearing"),
        ("commit msg heredoc is text", pre("Bash", {"command": "git commit -F - <<'EOF'\nPrivate store moved to ~/.pbz-private\nmentions EVALUATE and data.xlsx\nEOF"}, ws), None),
        ("python heredoc is code", pre("Bash", {"command": "python - <<'PY'\nimport pandas as pd\npd.read_csv('a.csv')\nPY"}, ws), "Script blocked"),
        ("python heredoc engine anywhere", pre("Bash", {"command": "python - <<'PY'\nimport pyadomd\nPY"}, other), "engine"),
    ]

    failures = 0
    for name, event, expect in cases:
        for agent in ("claude", "codex"):
            got = verdict(run(event, agent))
            ok = (got is None) if expect is None else (got is not None and expect.lower() in got.lower())
            if not ok:
                failures += 1
                print(f"FAIL [{agent}] {name}: expected {expect!r}, got {got!r}")

    ss = run({"hook_event_name": "SessionStart", "cwd": ws, "source": "startup"})
    if not (ss and "ZERO-DATA" in ss["hookSpecificOutput"]["additionalContext"]):
        failures += 1
        print("FAIL sessionstart ws")
    if run({"hook_event_name": "SessionStart", "cwd": other, "source": "startup"}) is not None:
        failures += 1
        print("FAIL sessionstart other")
    if run({"hook_event_name": "SessionStart", "cwd": syn, "source": "startup"}) is not None:
        failures += 1
        print("FAIL sessionstart synthetic")

    total = len(cases) * 2 + 3
    print(f"{total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
