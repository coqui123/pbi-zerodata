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
    write(os.path.join(ws, "audit.measurekiller"), "x")
    write(os.path.join(tmp, "mk", "m.measurekiller"), "x")
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
        ("read measurekiller anywhere", pre("Read", {"file_path": j(tmp, "mk", "m.measurekiller")}, other), "caches"),
        ("grep measurekiller folder", pre("Grep", {"pattern": "x", "path": j(tmp, "mk")}, other), "Content search"),
        ("read measure killer app data", pre("Read", {"file_path": j(os.environ.get("APPDATA") or j(home, "AppData", "Roaming"), "Measure Killer", "log.db")}, other), "caches"),
        ("gc measurekiller", pre("PowerShell", {"command": "Get-Content .\\audit.measurekiller"}, ws), "caches"),
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
        ("pbz consent request", pre("Bash", {"command": "pbz consent request C:/references/requirements.xlsx --purpose requirements"}, ws), None),
        ("pbz consent read", pre("Bash", {"command": "pbz consent read abc123"}, ws), None),
        ("pbz consent then raw", pre("Bash", {"command": "pbz consent status abc123 && cat data.xlsx"}, ws), "Data-bearing"),
        ("pbz mcp approved broker", pre("mcp__pbz__consent_read", {"id": "abc123"}, ws), None),
        ("pbz cmd path", pre("PowerShell", {"command": "& \"C:/x/powerbi-zerodata/bin/pbz.cmd\" check spec.json"}, ws), None),
        ("pbz cli.py", pre("Bash", {"command": "python C:/x/powerbi-zerodata/pbz/cli.py describe T"}, ws), None),
        ("pbz admin", pre("Bash", {"command": "pbz admin reset"}, ws), "user in their own terminal"),
        ("pbz image add", pre("Bash", {"command": f"pbz image add \"{j(ws, 'shot.png')}\""}, ws), None),
        ("cp png", pre("Bash", {"command": f"cp \"{j(ws, 'shot.png')}\" /tmp/x.png"}, ws), "Data-bearing"),
        ("pbz image then cat", pre("Bash", {"command": f"pbz image list && cat \"{j(ws, 'shot.png')}\""}, ws), "Data-bearing"),
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

    memo_failures, memo_checks = _private_root_memo()
    failures += memo_failures
    total = len(cases) * 2 + 3 + memo_checks
    print(f"{total - failures}/{total} passed")
    return 1 if failures else 0


def _private_root_memo() -> tuple[int, int]:
    """Warm global_denied must not realpath again, and a retarget with the same env still denies.

    Returns (failures, checks_attempted).
    """
    skill = os.path.dirname(HERE)
    sys.path.insert(0, skill)
    from pbz import guard
    from unittest.mock import patch

    tmp = tempfile.mkdtemp(prefix="pbzguard-memo-")
    failures = 0
    checks = 0
    previous = os.environ.get("PBZ_PRIVATE_ROOT")
    try:
        store = os.path.join(tmp, "store")
        os.makedirs(store)
        note = os.path.join(store, "note.txt")
        with open(note, "w", encoding="utf-8") as f:
            f.write("x")
        os.environ["PBZ_PRIVATE_ROOT"] = store
        guard._PRIVATE["stamp"] = None
        checks += 1
        if not guard.global_denied(guard.norm(note)):
            print("FAIL private root did not deny after warm")
            failures += 1
        real = os.path.realpath
        calls = {"n": 0}

        def counting(path):
            calls["n"] += 1
            return real(path)

        with patch("os.path.realpath", counting):
            for _ in range(500):
                guard.global_denied(guard.norm(note))
        checks += 1
        if calls["n"]:
            print(f"FAIL realpath after warm: {calls['n']}")
            failures += 1

        base = os.path.join(tmp, "ancestor")
        real_dir = os.path.join(base, "real")
        store = os.path.join(real_dir, "store")
        os.makedirs(store)
        old_note = os.path.join(store, "note.txt")
        with open(old_note, "w", encoding="utf-8") as f:
            f.write("old")
        parent = os.path.join(base, "parent")
        os.makedirs(parent)
        alias = os.path.join(parent, "alias")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", alias, real_dir], capture_output=True)
        if made.returncode != 0:
            print("SKIP ancestor junction")
        else:
            leaf = os.path.join(alias, "store")
            os.environ["PBZ_PRIVATE_ROOT"] = leaf
            guard._PRIVATE["stamp"] = None
            checks += 1
            if not guard.global_denied(guard.norm(old_note)):
                print("FAIL ancestor alias did not deny")
                failures += 1
            leaf_id = os.lstat(leaf)
            os.rmdir(alias)
            real2 = os.path.join(base, "real2")
            os.rename(real_dir, real2)
            again = subprocess.run(["cmd", "/c", "mklink", "/J", alias, real2], capture_output=True)
            if again.returncode != 0:
                print("SKIP ancestor retarget")
            else:
                after = os.lstat(leaf)
                checks += 1
                if (after.st_dev, after.st_ino) != (leaf_id.st_dev, leaf_id.st_ino):
                    print("FAIL private-root leaf identity changed")
                    failures += 1
                checks += 1
                if os.environ.get("PBZ_PRIVATE_ROOT") != leaf:
                    print("FAIL private-root env changed")
                    failures += 1
                new_note = os.path.join(real2, "store", "later.txt")
                with open(new_note, "w", encoding="utf-8") as f:
                    f.write("new")
                checks += 1
                if not guard.global_denied(guard.norm(new_note)):
                    print("FAIL retargeted ancestor did not deny the new target")
                    failures += 1
                calls2 = {"n": 0}

                def counting2(path):
                    calls2["n"] += 1
                    return real(path)

                with patch("os.path.realpath", counting2):
                    for _ in range(500):
                        guard.global_denied(guard.norm(new_note))
                checks += 1
                if calls2["n"]:
                    print(f"FAIL realpath within unchanged evaluation: {calls2['n']}")
                    failures += 1

        # Two-hop: configured → hop → old; retarget only hop → new with same env.
        chain = os.path.join(tmp, "chain")
        old_t = os.path.join(chain, "old")
        new_t = os.path.join(chain, "new")
        hop = os.path.join(chain, "hop")
        configured = os.path.join(chain, "configured")
        os.makedirs(old_t)
        os.makedirs(new_t)
        with open(os.path.join(old_t, "note.txt"), "w", encoding="utf-8") as f:
            f.write("old")
        hop_made = subprocess.run(["cmd", "/c", "mklink", "/J", hop, old_t], capture_output=True)
        cfg_made = subprocess.run(["cmd", "/c", "mklink", "/J", configured, hop], capture_output=True)
        if hop_made.returncode != 0 or cfg_made.returncode != 0:
            print("SKIP two-hop private-root junctions")
        else:
            os.environ["PBZ_PRIVATE_ROOT"] = configured
            guard._PRIVATE["stamp"] = None
            checks += 1
            if not guard.global_denied(guard.norm(os.path.join(old_t, "note.txt"))):
                print("FAIL two-hop warm did not deny old target")
                failures += 1
            env_before = os.environ.get("PBZ_PRIVATE_ROOT")
            cfg_before = os.lstat(configured)
            os.rmdir(hop)
            hop_again = subprocess.run(["cmd", "/c", "mklink", "/J", hop, new_t], capture_output=True)
            if hop_again.returncode != 0:
                print("SKIP two-hop hop retarget")
            else:
                checks += 1
                if os.environ.get("PBZ_PRIVATE_ROOT") != env_before:
                    print("FAIL two-hop env changed")
                    failures += 1
                checks += 1
                after_cfg = os.lstat(configured)
                if (after_cfg.st_dev, after_cfg.st_ino) != (cfg_before.st_dev, cfg_before.st_ino):
                    print("FAIL two-hop configured identity changed")
                    failures += 1
                new_note = os.path.join(new_t, "note.txt")
                with open(new_note, "w", encoding="utf-8") as f:
                    f.write("new")
                checks += 1
                if not guard.global_denied(guard.norm(new_note)):
                    print("FAIL two-hop hop retarget did not deny the new target")
                    failures += 1
                calls3 = {"n": 0}

                def counting3(path):
                    calls3["n"] += 1
                    return real(path)

                with patch("os.path.realpath", counting3):
                    for _ in range(500):
                        guard.global_denied(guard.norm(new_note))
                checks += 1
                if calls3["n"]:
                    print(f"FAIL realpath after two-hop warm: {calls3['n']}")
                    failures += 1

        # Descendant root: PBZ_PRIVATE_ROOT=configured/store with configured→hop→old.
        # Retarget hop after renaming old→new (leaf store identity preserved).
        chain2 = os.path.join(tmp, "chain2")
        old2 = os.path.join(chain2, "old")
        new2 = os.path.join(chain2, "new")
        hop2 = os.path.join(chain2, "hop")
        configured2 = os.path.join(chain2, "configured")
        store_old = os.path.join(old2, "store")
        os.makedirs(store_old)
        with open(os.path.join(store_old, "note.txt"), "w", encoding="utf-8") as f:
            f.write("old")
        hop2_made = subprocess.run(["cmd", "/c", "mklink", "/J", hop2, old2], capture_output=True)
        cfg2_made = subprocess.run(["cmd", "/c", "mklink", "/J", configured2, hop2], capture_output=True)
        if hop2_made.returncode != 0 or cfg2_made.returncode != 0:
            print("SKIP descendant two-hop private-root junctions")
        else:
            leaf = os.path.join(configured2, "store")
            os.environ["PBZ_PRIVATE_ROOT"] = leaf
            guard._PRIVATE["stamp"] = None
            checks += 1
            if not guard.global_denied(guard.norm(os.path.join(store_old, "note.txt"))):
                print("FAIL descendant two-hop warm did not deny old target")
                failures += 1
            env2 = os.environ.get("PBZ_PRIVATE_ROOT")
            cfg2_before = os.lstat(configured2)
            leaf_before = os.lstat(leaf)
            os.rmdir(hop2)
            os.rename(old2, new2)
            hop2_again = subprocess.run(["cmd", "/c", "mklink", "/J", hop2, new2], capture_output=True)
            if hop2_again.returncode != 0:
                print("SKIP descendant hop retarget")
            else:
                checks += 1
                if os.environ.get("PBZ_PRIVATE_ROOT") != env2:
                    print("FAIL descendant two-hop env changed")
                    failures += 1
                checks += 1
                after_cfg2 = os.lstat(configured2)
                if (after_cfg2.st_dev, after_cfg2.st_ino) != (cfg2_before.st_dev, cfg2_before.st_ino):
                    print("FAIL descendant configured identity changed")
                    failures += 1
                checks += 1
                after_leaf = os.lstat(leaf)
                if (after_leaf.st_dev, after_leaf.st_ino) != (leaf_before.st_dev, leaf_before.st_ino):
                    print("FAIL descendant configured leaf identity changed")
                    failures += 1
                new_note2 = os.path.join(new2, "store", "note.txt")
                checks += 1
                if not guard.global_denied(guard.norm(new_note2)):
                    print("FAIL descendant hop retarget did not deny the new target")
                    failures += 1
                calls4 = {"n": 0}

                def counting4(path):
                    calls4["n"] += 1
                    return real(path)

                with patch("os.path.realpath", counting4):
                    for _ in range(500):
                        guard.global_denied(guard.norm(new_note2))
                checks += 1
                if calls4["n"]:
                    print(f"FAIL realpath after descendant two-hop warm: {calls4['n']}")
                    failures += 1

        # Nested target: configured → hop/leaf, hop → old; root is configured/store.
        # Retarget only hop after rename old→new; hop itself must be in the stamp.
        chain3 = os.path.join(tmp, "chain3")
        old3 = os.path.join(chain3, "old")
        new3 = os.path.join(chain3, "new")
        hop3 = os.path.join(chain3, "hop")
        configured3 = os.path.join(chain3, "configured")
        leaf_old = os.path.join(old3, "leaf")
        store3 = os.path.join(leaf_old, "store")
        os.makedirs(store3)
        with open(os.path.join(store3, "note.txt"), "w", encoding="utf-8") as f:
            f.write("old")
        hop3_made = subprocess.run(["cmd", "/c", "mklink", "/J", hop3, old3], capture_output=True)
        # configured points at hop/leaf (ordinary dir under the hop junction).
        cfg3_target = os.path.join(hop3, "leaf")
        cfg3_made = subprocess.run(["cmd", "/c", "mklink", "/J", configured3, cfg3_target],
                                   capture_output=True)
        if hop3_made.returncode != 0 or cfg3_made.returncode != 0:
            print("SKIP nested-target private-root junctions")
        else:
            leaf3 = os.path.join(configured3, "store")
            os.environ["PBZ_PRIVATE_ROOT"] = leaf3
            guard._PRIVATE["stamp"] = None
            checks += 1
            if not guard.global_denied(guard.norm(os.path.join(store3, "note.txt"))):
                print("FAIL nested-target warm did not deny old target")
                failures += 1
            env3 = os.environ.get("PBZ_PRIVATE_ROOT")
            cfg3_before = os.lstat(configured3)
            leaf3_before = os.lstat(leaf3)
            os.rmdir(hop3)
            os.rename(old3, new3)
            hop3_again = subprocess.run(["cmd", "/c", "mklink", "/J", hop3, new3], capture_output=True)
            if hop3_again.returncode != 0:
                print("SKIP nested-target hop retarget")
            else:
                checks += 1
                if os.environ.get("PBZ_PRIVATE_ROOT") != env3:
                    print("FAIL nested-target env changed")
                    failures += 1
                checks += 1
                after_cfg3 = os.lstat(configured3)
                if (after_cfg3.st_dev, after_cfg3.st_ino) != (cfg3_before.st_dev, cfg3_before.st_ino):
                    print("FAIL nested-target configured identity changed")
                    failures += 1
                checks += 1
                after_leaf3 = os.lstat(leaf3)
                if (after_leaf3.st_dev, after_leaf3.st_ino) != (leaf3_before.st_dev, leaf3_before.st_ino):
                    print("FAIL nested-target leaf identity changed")
                    failures += 1
                new_note3 = os.path.join(new3, "leaf", "store", "note.txt")
                checks += 1
                if not guard.global_denied(guard.norm(new_note3)):
                    print("FAIL nested-target hop retarget did not deny the new target")
                    failures += 1
                calls5 = {"n": 0}

                def counting5(path):
                    calls5["n"] += 1
                    return real(path)

                with patch("os.path.realpath", counting5):
                    for _ in range(500):
                        guard.global_denied(guard.norm(new_note3))
                checks += 1
                if calls5["n"]:
                    print(f"FAIL realpath after nested-target warm: {calls5['n']}")
                    failures += 1

        # Deep nest under an aliased ancestor: 70 ordinary dirs then store.
        # Retarget configured alone; memo must still deny the new canonical target.
        deep = os.path.join(tmp, "deep")
        old_d = os.path.join(deep, "old")
        new_d = os.path.join(deep, "new")
        configured_d = os.path.join(deep, "configured")
        nest = old_d
        for i in range(70):
            nest = os.path.join(nest, f"d{i}")
        store_d = os.path.join(nest, "store")
        os.makedirs(store_d)
        with open(os.path.join(store_d, "note.txt"), "w", encoding="utf-8") as f:
            f.write("old")
        deep_made = subprocess.run(["cmd", "/c", "mklink", "/J", configured_d, old_d],
                                   capture_output=True)
        if deep_made.returncode != 0:
            print("SKIP deep-nest private-root junction")
        else:
            # Mirror the nested path under the junction.
            leaf_d = configured_d
            for i in range(70):
                leaf_d = os.path.join(leaf_d, f"d{i}")
            leaf_d = os.path.join(leaf_d, "store")
            os.environ["PBZ_PRIVATE_ROOT"] = leaf_d
            guard._PRIVATE["stamp"] = None
            checks += 1
            if not guard.global_denied(guard.norm(os.path.join(store_d, "note.txt"))):
                print("FAIL deep-nest warm did not deny old target")
                failures += 1
            env_d = os.environ.get("PBZ_PRIVATE_ROOT")
            leaf_d_before = os.lstat(leaf_d)
            os.rmdir(configured_d)
            os.rename(old_d, new_d)
            deep_again = subprocess.run(["cmd", "/c", "mklink", "/J", configured_d, new_d],
                                        capture_output=True)
            if deep_again.returncode != 0:
                print("SKIP deep-nest retarget")
            else:
                checks += 1
                if os.environ.get("PBZ_PRIVATE_ROOT") != env_d:
                    print("FAIL deep-nest env changed")
                    failures += 1
                checks += 1
                after_leaf_d = os.lstat(leaf_d)
                if (after_leaf_d.st_dev, after_leaf_d.st_ino) != (leaf_d_before.st_dev, leaf_d_before.st_ino):
                    print("FAIL deep-nest leaf identity changed")
                    failures += 1
                # Rebuild nest path under new_d for the canonical note.
                new_nest = new_d
                for i in range(70):
                    new_nest = os.path.join(new_nest, f"d{i}")
                new_note_d = os.path.join(new_nest, "store", "note.txt")
                checks += 1
                if not guard.global_denied(guard.norm(new_note_d)):
                    print("FAIL deep-nest retarget did not deny the new target")
                    failures += 1

        # Incomplete closure must not authorize memo reuse.
        checks += 1
        incomplete_root = os.path.join(tmp, "incomplete-store")
        os.makedirs(incomplete_root, exist_ok=True)
        incomplete_note = os.path.join(incomplete_root, "note.txt")
        with open(incomplete_note, "w", encoding="utf-8") as f:
            f.write("incomplete")

        def exhaust(path, parts, *, budget=256):
            return False

        with patch.object(guard, "_stamp_alias_closure", side_effect=exhaust):
            guard._PRIVATE["stamp"] = ("poison",)
            guard._PRIVATE["roots"] = ["/poison"]
            os.environ["PBZ_PRIVATE_ROOT"] = incomplete_root
            denied = guard.global_denied(guard.norm(incomplete_note))
            if not denied:
                print("FAIL incomplete closure failed to deny private note")
                failures += 1
            if guard._PRIVATE["stamp"] is not None:
                print("FAIL incomplete closure cached a stamp")
                failures += 1
        guard._PRIVATE["stamp"] = None
        if not guard.global_denied(guard.norm(incomplete_note)):
            print("FAIL incomplete-store not denied after restoring closure")
            failures += 1
        checks += 1

        # readlink failure on an alias must not authorize a complete memo.
        chain_rl = os.path.join(tmp, "readlink-fail")
        old_rl = os.path.join(chain_rl, "old")
        new_rl = os.path.join(chain_rl, "new")
        hop_rl = os.path.join(chain_rl, "hop")
        configured_rl = os.path.join(chain_rl, "configured")
        os.makedirs(old_rl)
        os.makedirs(new_rl)
        with open(os.path.join(old_rl, "note.txt"), "w", encoding="utf-8") as f:
            f.write("old")
        with open(os.path.join(new_rl, "note.txt"), "w", encoding="utf-8") as f:
            f.write("new")
        hop_rl_made = subprocess.run(["cmd", "/c", "mklink", "/J", hop_rl, old_rl], capture_output=True)
        cfg_rl_made = subprocess.run(["cmd", "/c", "mklink", "/J", configured_rl, hop_rl],
                                     capture_output=True)
        if hop_rl_made.returncode != 0 or cfg_rl_made.returncode != 0:
            print("SKIP readlink-fail private-root junctions")
        else:
            os.environ["PBZ_PRIVATE_ROOT"] = configured_rl
            guard._PRIVATE["stamp"] = None
            real_readlink = os.readlink
            failing = {"on": True}

            def flaky_readlink(path):
                if failing["on"] and os.path.normcase(os.path.abspath(path)) == os.path.normcase(
                        os.path.abspath(configured_rl)):
                    raise OSError("injected readlink failure")
                return real_readlink(path)

            with patch("os.readlink", flaky_readlink):
                checks += 1
                if not guard.global_denied(guard.norm(os.path.join(old_rl, "note.txt"))):
                    print("FAIL readlink-fail warm did not deny old target")
                    failures += 1
                checks += 1
                if guard._PRIVATE["stamp"] is not None:
                    print("FAIL readlink-fail cached a complete stamp")
                    failures += 1
            failing["on"] = False
            os.rmdir(hop_rl)
            hop_rl_again = subprocess.run(["cmd", "/c", "mklink", "/J", hop_rl, new_rl],
                                          capture_output=True)
            if hop_rl_again.returncode != 0:
                print("SKIP readlink-fail hop retarget")
            else:
                checks += 1
                if not guard.global_denied(guard.norm(os.path.join(new_rl, "note.txt"))):
                    print("FAIL readlink-fail retarget did not deny new target")
                    failures += 1

        missing = os.path.join(tmp, "missing-root")
        os.environ["PBZ_PRIVATE_ROOT"] = missing
        guard.global_denied(guard.norm(note))
        target = os.path.join(tmp, "target")
        os.makedirs(target)
        planted = os.path.join(target, "CANARY-NAME.txt")
        with open(planted, "w", encoding="utf-8") as f:
            f.write("y")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", missing, target], capture_output=True)
        if made.returncode != 0:
            print("SKIP private-root junction")
            return failures, checks
        checks += 1
        if not guard.global_denied(guard.norm(planted)):
            print("FAIL missing root becoming a junction did not deny the new target")
            failures += 1
        other = os.path.join(tmp, "other")
        os.makedirs(other)
        moved = os.path.join(other, "later.txt")
        with open(moved, "w", encoding="utf-8") as f:
            f.write("z")
        os.rmdir(missing)
        again = subprocess.run(["cmd", "/c", "mklink", "/J", missing, other], capture_output=True)
        if again.returncode != 0:
            print("SKIP private-root retarget")
            return failures, checks
        checks += 1
        if not guard.global_denied(guard.norm(moved)):
            print("FAIL retargeted private root did not deny")
            failures += 1
        checks += 1
        if guard.global_denied(guard.norm(planted)):
            print("FAIL old private target stayed denied after retarget")
            failures += 1
    finally:
        if previous is None:
            os.environ.pop("PBZ_PRIVATE_ROOT", None)
        else:
            os.environ["PBZ_PRIVATE_ROOT"] = previous
        guard._PRIVATE["stamp"] = None
    return failures, checks


if __name__ == "__main__":
    sys.exit(main())
