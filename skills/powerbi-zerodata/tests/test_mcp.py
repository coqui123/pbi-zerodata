"""pbz MCP server: protocol handshake, tool schemas, tool calls on a synthetic workspace, error handling,
no planted secret in any response, and the install.py --mcp registration logic (on temp files only).

Run: python tests/test_mcp.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402

CLI = os.path.join(SKILL, "pbz", "cli.py")


def rpc(mid, method, params=None) -> dict:
    m = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        m["params"] = params
    return m


def call(mid, name, args=None) -> dict:
    return rpc(mid, "tools/call", {"name": name, "arguments": args or {}})


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    tmp = tempfile.mkdtemp(prefix="pbzmcp-")
    root = os.path.join(tmp, "ws")
    test_parity.build(root)
    env = {**os.environ, "PBZ_PRIVATE_ROOT": os.path.join(tmp, "private"), "PYTHONUTF8": "1"}
    msgs = [
        rpc(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        rpc(2, "ping"),
        rpc(3, "tools/list"),
        call(10, "pull"),
        call(11, "status"),
        call(12, "describe"),
        call(13, "describe", {"target": "[Total Sales]"}),
        call(14, "values", {"column": "'Customer'[Name]"}),
        call(15, "unused", {"kinds": ["measure", "table"]}),
        call(16, "lineage", {"target": "[Total Sales]", "depth": 3}),
        call(17, "layout", {"page": "Overview"}),
        call(18, "validate"),
        call(19, "bpa", {"scope": "model", "labels": "measure-killer"}),
        call(20, "unused", {"remove": True, "dry_run": True}),
        call(21, "describe", {"target": "NoSuchThing"}),          # CLI error -> isError
        call(22, "format"),
        call(23, "rename", {"kind": "measure", "old": "--help", "new": "x"}),  # flag injection refused
        call(24, "nope"),                                          # unknown tool
        call(25, "describe", {"bogus": 1}),                        # unknown argument
        call(26, "lineage", {"depth": 3}),                         # missing required
        call(27, "lineage", {"target": "[Total Sales]", "depth": "3"}),   # wrong type
        call(28, "bpa", {"scope": "everything"}),                  # enum violation
        call(29, "status", {"workspace": os.path.join(tmp, "missing")}),
        rpc(30, "resources/list"),                                 # not offered
        [rpc(31, "ping")],                                         # batch
    ]
    lines = "".join(json.dumps(m) + "\n" for m in msgs) + "{not json\n" + json.dumps({"jsonrpc": "2.0", "id": 99}) + "\n"
    p = subprocess.run([sys.executable, "-I", CLI, "--workspace", root, "mcp"], input=lines.encode("utf-8"),
                       capture_output=True, env=env, timeout=900)
    check(p.returncode == 0, f"server exit code {p.returncode}")
    out = p.stdout.decode("utf-8")
    resp = [json.loads(x) for x in out.splitlines() if x.strip()]
    by_id = {r.get("id"): r for r in resp}
    check(all(r.get("jsonrpc") == "2.0" for r in resp), "non JSON-RPC line on stdout")
    check(len(resp) == len([m for m in msgs if not (isinstance(m, dict) and "id" not in m)]) + 2,
          f"one response per request (got {len(resp)})")

    init = by_id.get(1, {}).get("result", {})
    check(init.get("protocolVersion") == "2025-06-18" and init.get("serverInfo", {}).get("name") == "pbz"
          and "tools" in init.get("capabilities", {}), f"initialize: {init}")
    check(by_id.get(2, {}).get("result") == {}, "ping")
    tools = by_id.get(3, {}).get("result", {}).get("tools", [])
    names = {t["name"] for t in tools}
    for want in ("status", "pull", "push", "diff", "validate", "describe", "unused", "lineage", "bpa", "layout",
                 "rename", "move_measure", "new", "relationship", "add_page", "add_visual", "check", "snapshot", "synth"):
        check(want in names, f"tool {want} missing")
    check(not {"admin", "mcp"} & names, "admin or mcp exposed")
    # These audited surfaces return synthetic layouts or content-bound human-approved text.
    reviewed = {"preview", "consent_read"}
    check(not any("powerbi" in n.lower() or any(w in n.lower() for w in ("read", "open", "file", "view")) for n in names - reviewed),
          "a tool name would trip the guard (powerbi / file-ish words)")
    for t in tools:
        sch = t.get("inputSchema", {})
        check(sch.get("type") == "object" and sch.get("additionalProperties") is False and "workspace" in sch.get("properties", {}),
              f"schema shape of {t['name']}")
    req = {t["name"]: set(t["inputSchema"].get("required", [])) for t in tools}
    check(req.get("rename") == {"kind", "old", "new"} and req.get("move_measure") == {"name", "to"}
          and req.get("add_visual") == {"type", "page"}, f"required args: {req.get('rename')} {req.get('move_measure')}")
    ms = next((t for t in tools if t["name"] == "memscrub"), {})
    check("apply" not in ms.get("inputSchema", {}).get("properties", {}), "memscrub --apply exposed")

    def payload(mid):
        r = by_id.get(mid, {}).get("result", {})
        try:
            return json.loads(r["content"][0]["text"]), r.get("isError")
        except (KeyError, IndexError, ValueError):
            return {}, None

    d, err = payload(10)
    check(d.get("verb") == "pull" and err is False, f"pull: {d} {err}")
    d, err = payload(11)
    check(d.get("verb") == "status" and d.get("mirror") == "present" and err is False, f"status: {d}")
    d, _ = payload(12)
    check(d.get("verb") == "describe" and {t["table"] for t in d.get("tables", [])} >= {"Sales", "Customer"}, "describe overview")
    d, _ = payload(13)
    check(d.get("measure") == "Total Sales" and "Sales[Amount]" in d.get("depends_on", []), f"describe measure: {d}")
    d, _ = payload(14)
    check(d.get("class") == "sensitive" and "values" not in d, f"values on a sensitive column: {d}")
    d, _ = payload(15)
    check(d.get("verb") == "unused" and set(d.get("summary", {})) <= {"measure", "table"}, f"unused kinds: {d.get('summary')}")
    d, _ = payload(16)
    check(d.get("verb") == "lineage" and d.get("trees"), "lineage")
    d, _ = payload(17)
    check(d.get("verb") == "layout" and d.get("pages") and d["pages"][0].get("map"), "layout map")
    d, _ = payload(18)
    check(d.get("verb") == "validate", "validate")
    d, _ = payload(19)
    check(d.get("verb") == "bpa" and d.get("model", {}).get("grade") in
          ("Power BI Pro", "Good", "Ok", "Poor", "Awful", "Power BI Criminal"), f"bpa labels: {d.get('model')}")
    d, _ = payload(20)
    check(d.get("result") == "DRY_RUN", f"unused remove dry run: {d.get('result')}")
    d, err = payload(21)
    check(err is True and d.get("error") == "NOT_FOUND", f"CLI error as isError: {d} {err}")
    d, _ = payload(22)
    check(d.get("verb") == "format" and d.get("mode") == "check", "format check")
    for mid, code in ((23, -32602), (24, -32602), (25, -32602), (26, -32602), (27, -32602), (28, -32602),
                      (30, -32601)):
        check(by_id.get(mid, {}).get("error", {}).get("code") == code, f"request {mid}: {by_id.get(mid)}")
    d, err = payload(29)
    check(d.get("verb") == "status" and err is False, f"tool workspace is ignored: {d} {err}")
    check(any(r.get("id") is None and r.get("error", {}).get("code") == -32600 for r in resp), "batch refused")
    check(any(r.get("id") is None and r.get("error", {}).get("code") == -32700 for r in resp), "parse error")
    check(by_id.get(99, {}).get("error", {}).get("code") == -32600, "request without method")
    err_text = p.stderr.decode("utf-8", errors="replace")
    check(err_text.strip().splitlines() == ["pbz mcp: ready", "pbz mcp: stdin closed"], f"stderr chatter: {err_text!r}")
    blob = out + err_text
    for s in test_parity.SECRETS:
        check(s not in blob, f"secret in MCP output: {s!r}")

    # ---------------------------------------------------------------- install.py --mcp (temp files only)
    spec = importlib.util.spec_from_file_location("pbz_install", os.path.join(SKILL, "install", "install.py"))
    inst = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(inst)
    data, how = inst.merge_claude_mcp({"projects": {"x": 1}}, False)
    check(how == "registered" and data["projects"] == {"x": 1} and data["mcpServers"]["pbz"]["args"][-1] == "mcp",
          f"claude register: {how}")
    check(inst.merge_claude_mcp(json.loads(json.dumps(data)), False)[1] == "ok", "claude register not idempotent")
    foreign = {"mcpServers": {"pbz": {"command": "other", "args": ["serve"]}}}
    check(inst.merge_claude_mcp(json.loads(json.dumps(foreign)), False)[1].startswith("SKIP"), "foreign pbz entry overwritten")
    data, how = inst.merge_claude_mcp(data, True)
    check(how == "removed" and "mcpServers" not in data and data["projects"] == {"x": 1}, f"claude remove: {how} {data}")
    toml_in = 'model = "o3"\n\n[mcp_servers.other]\ncommand = "x"\n'
    text, how = inst.merge_codex_toml(toml_in, False)
    check(how == "registered" and "[mcp_servers.other]" in text and "[mcp_servers.pbz]" in text, f"codex register: {how}")
    try:
        import tomllib
        parsed = tomllib.loads(text)
        check(parsed["mcp_servers"]["pbz"]["args"][-1] == "mcp" and parsed["model"] == "o3", "codex TOML content")
    except ImportError:
        pass
    check(inst.merge_codex_toml(text, False)[1] == "ok", "codex register not idempotent")
    check(inst.merge_codex_toml('[mcp_servers.pbz]\ncommand = "other"\n', False)[1].startswith("SKIP"), "foreign codex entry")
    text2, how = inst.merge_codex_toml(text, True)
    check(how == "removed" and "[mcp_servers.pbz]" not in text2 and "[mcp_servers.other]" in text2, f"codex remove: {how}")
    check(inst.merge_codex_toml(toml_in, True) == (toml_in, "absent"), "codex uninstall touched an absent entry")
    home = os.path.join(tmp, "home")
    os.makedirs(home)
    henv = {**os.environ, "USERPROFILE": home, "HOME": home}
    r = subprocess.run([sys.executable, "-I", os.path.join(SKILL, "install", "install.py"), "--what-if", "--mcp"],
                       capture_output=True, env=henv, timeout=120)
    check(r.returncode == 0 and b"mcpServers.pbz" in r.stdout and b"[mcp_servers.pbz]" in r.stdout,
          f"install --what-if --mcp: {r.stdout[-300:]!r}")
    check(os.listdir(home) == [], f"--what-if wrote files: {os.listdir(home)}")

    shutil.rmtree(tmp, ignore_errors=True)
    for f in fails:
        print("FAIL", f)
    print("ok" if not fails else f"{len(fails)} failure(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
