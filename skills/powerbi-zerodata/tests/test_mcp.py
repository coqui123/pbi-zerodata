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
import unittest
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import mcp  # noqa: E402

CLI = os.path.join(SKILL, "pbz", "cli.py")


def load_installer():
    spec = importlib.util.spec_from_file_location('pbz_install', os.path.join(SKILL, 'install', 'install.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallerTransactionTests(unittest.TestCase):
    def setUp(self):
        self.inst = load_installer()

    def test_host_keys_and_nested_tables_refused_on_apply_update_uninstall(self):
        clean, _ = self.inst.merge_codex_toml('', False)
        for host in ('extra = "host"\n', '[mcp_servers.pbz.env]\nHOST = "keep"\n',
                     '[mcp_servers.pbz.nested.deep]\nvalue = 1\n'):
            text = clean + host
            for uninstall in (False, True, False):
                with self.subTest(host=host, uninstall=uninstall):
                    new, reason = self.inst.merge_codex_toml(text, uninstall)
                    self.assertEqual(new, text)
                    self.assertIn('conflict mcp_servers.pbz.', reason)
                    with tempfile.TemporaryDirectory() as tmp:
                        path = Path(tmp, 'config.toml')
                        path.write_text(text, encoding='utf-8')
                        before = path.read_bytes(), path.stat().st_mtime_ns
                        self.inst.update_toml(str(path), uninstall)
                        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)
                        self.assertEqual(path.read_text(encoding='utf-8'), text)

    def test_interrupted_writers_leave_original_unchanged(self):
        inst = self.inst
        with tempfile.TemporaryDirectory() as tmp:
            for writer in ('json', 'toml', 'rule'):
                for phase in ('writing', 'replace'):
                    with self.subTest(writer=writer, phase=phase):
                        path = Path(tmp, writer + '-' + phase)
                        original = '{"host": 1}\n' if writer == 'json' else 'host = "keep"\n'
                        path.write_text(original, encoding='utf-8')
                        def partial(handle, text):
                            handle.write(text[:3])
                            raise KeyboardInterrupt('injected interruption')
                        target = patch.object(inst, '_write_content', side_effect=partial) if phase == 'writing' else patch.object(inst.os, 'replace', side_effect=KeyboardInterrupt('before replace'))
                        with target, self.assertRaises(KeyboardInterrupt):
                            if writer == 'json':
                                inst.settings_step(str(path), 'codex', False, False)
                            elif writer == 'toml':
                                inst.update_toml(str(path), False)
                            else:
                                inst.rule_block(str(path), False, False)
                        self.assertEqual(path.read_text(encoding='utf-8'), original)
                        self.assertEqual(list(Path(tmp).glob('*.tmp-pbz-*')), [])

    def test_concurrent_distinct_updates_after_both_reads(self):
        # Two independent installers take the same snapshot before either can write.
        # A stale writer must receive an explicit named conflict, never silently win.
        worker = r'''
import importlib.util, json, pathlib, sys, time
spec = importlib.util.spec_from_file_location('installer', sys.argv[1])
inst = importlib.util.module_from_spec(spec); spec.loader.exec_module(inst)
path, barrier, name = sys.argv[2:]
snapshot = inst.load_json(path)
pathlib.Path(barrier + name).touch()
deadline = time.monotonic() + 20
while not pathlib.Path(barrier + 'go').exists():
    if time.monotonic() > deadline: raise RuntimeError('barrier timeout')
    time.sleep(.02)
try:
    inst.update_json(path, lambda data: ({**data, name: True}, 'updated'), expected=snapshot)
except inst.InstallConflict as error:
    print(str(error)); sys.exit(3)
'''
        with tempfile.TemporaryDirectory() as tmp:
            path, barrier = str(Path(tmp, 'settings.json')), str(Path(tmp, 'barrier-'))
            Path(path).write_text('{}', encoding='utf-8')
            children = [subprocess.Popen([sys.executable, '-c', worker, self.inst.__file__, path, barrier, name],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE) for name in ('alpha', 'beta')]
            try:
                deadline = time.monotonic() + 20
                while not all(Path(barrier + n).exists() for n in ('alpha', 'beta')):
                    if time.monotonic() > deadline:
                        self.fail('installers did not overlap their reads')
                    time.sleep(.02)
                Path(barrier + 'go').touch()
                outputs = [child.communicate(timeout=20) for child in children]
                self.assertEqual(sorted(child.returncode for child in children), [0, 3])
                self.assertIn(b'JSON changed since read', b''.join(out for out, err in outputs))
                data = self.inst.load_json(path)
                self.assertIn(data, ({'alpha': True}, {'beta': True}))
            finally:
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.communicate()

    def test_malformed_rule_markers_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'AGENTS.md')
            path.write_text(self.inst.BEGIN, encoding='utf-8')
            with self.assertRaisesRegex(self.inst.InstallConflict, 'rule block markers'):
                self.inst.rule_block(str(path), False, False)
            self.assertEqual(path.read_text(encoding='utf-8'), self.inst.BEGIN)


def rpc(mid, method, params=None) -> dict:
    m = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        m["params"] = params
    return m


def call(mid, name, args=None) -> dict:
    return rpc(mid, "tools/call", {"name": name, "arguments": args or {}})


class OutputBudgetTests(unittest.TestCase):
    def run_child(self, code, timeout=5):
        return mcp._bounded_output([sys.executable, "-c", code], os.getcwd(), os.environ.copy(), timeout)

    def test_raw_exact_and_over_limit(self):
        with patch.object(mcp, "RAW_LIMIT", 1024):
            code, raw, error = self.run_child("import os; os.write(1, b'x'*1024)")
            self.assertEqual((code, len(raw), error), (0, 1024, None))
            tracemalloc.start()
            try:
                code, raw, error = self.run_child("import os; os.write(1, b'x'*10000000)")
                _current, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            self.assertEqual((raw, error), (b"", "OUTPUT_LIMIT"))
            self.assertLess(peak, 1024*1024)

    def test_exact_wire_limit_during_production(self):
        response = {"jsonrpc": "2.0", "id": 0, "result": {
            "content": [{"type": "text", "text": "x"*1024}], "isError": False}}
        with patch.object(mcp, "WIRE_LIMIT", len(mcp._encode(response))):
            code, raw, error = self.run_child("import os; os.write(1,b'x'*1024)")
            self.assertEqual((code, raw, error), (0, b"x"*1024, None))

    def test_wire_expansion_is_stopped_during_production(self):
        with patch.object(mcp, "WIRE_LIMIT", 1024), patch.object(mcp, "RAW_LIMIT", 10000):
            _code, raw, error = self.run_child("import os,time; os.write(1,bytes([92])*2000); time.sleep(20)")
            self.assertEqual((raw, error), (b"", "OUTPUT_LIMIT"))

    def test_stderr_is_discarded(self):
        with patch.object(mcp, "RAW_LIMIT", 1024):
            code, raw, error = self.run_child("import os; os.write(2,b'x'*10000000); os.write(1,b'ok')")
            self.assertEqual((code, raw, error), (0, b"ok", None))

    def test_wire_exact_limit_and_escaping(self):
        response = {"jsonrpc": "2.0", "id": 1, "result": mcp._result({"value": "x"*1000}, False)}
        size = len(mcp._encode(response))
        with patch.object(mcp, "WIRE_LIMIT", size):
            self.assertEqual(mcp._wire_response(response), response)
            escaped = {**response, "result": {"content": [{"type": "text", "text": "\\"*1000}], "isError": False}}
            out = mcp._wire_response(escaped, mcp.BY_NAME["rename"])
            body = json.loads(out["result"]["content"][0]["text"])
            self.assertEqual(body["error"], "OUTPUT_LIMIT")
            self.assertEqual(body["outcome"], "UNKNOWN")
            self.assertIn("recover", body["recovery"])
            self.assertLessEqual(len(mcp._encode(out)), size)

    def test_timed_out_writer_and_size_limit_have_recovery(self):
        code, raw, error = self.run_child("import time; time.sleep(20)", timeout=0.1)
        self.assertEqual((raw, error), (b"", "TIMEOUT"))
        for error in ("TIMEOUT", "OUTPUT_LIMIT"):
            with patch.object(mcp, "_bounded_output", return_value=(-9, b"", error)):
                result = mcp.call_tool("rename", {"kind": "column", "old": "A", "new": "B"}, os.getcwd(), None)
            body = json.loads(result["content"][0]["text"])
            self.assertTrue(result["isError"])
            self.assertEqual(body["error"], error)
            self.assertEqual(body["outcome"], "UNKNOWN")
            self.assertIn("recover", body["recovery"])

    def test_timeout_kills_descendant_with_inherited_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = str(Path(tmp, "child-survived"))
            child = f"import time; from pathlib import Path; time.sleep(0.4); Path({marker!r}).write_text('survived')"
            parent = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(20)"
            _code, raw, error = self.run_child(parent, timeout=0.15)
            self.assertEqual((raw, error), (b"", "TIMEOUT"))
            time.sleep(0.5)
            self.assertFalse(Path(marker).exists())

    def test_decoded_limit(self):
        with patch.object(mcp, "DECODED_LIMIT", 10), \
             patch.object(mcp, "_bounded_output", return_value=(0, b"x"*11, None)):
            result = mcp.call_tool("status", {}, os.getcwd(), None)
        self.assertEqual(json.loads(result["content"][0]["text"])["error"], "OUTPUT_LIMIT")


class GitHookTests(unittest.TestCase):
    def setUp(self):
        path = Path(SKILL).parents[1] / "scripts" / "install_git_hooks.py"
        spec = importlib.util.spec_from_file_location("git_hooks_installer", path)
        self.inst = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.inst)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        self.hooks = self.root / ".git" / "hooks"
        self.source = self.root / "source"
        self.source.mkdir()
        for name in self.inst.NAMES:
            (self.source / name).write_bytes(b'#!/bin/sh\n# PBZ owned fixture\nprintf "pbz:%s" "$*"\ncat\n')

    def test_split_hooks_behavior_and_staged_content(self):
        # Use the real staged checker on a minimal repository and actual shell hooks.
        repo = Path(SKILL).parents[1]
        scripts = self.root / 'scripts'
        scripts.mkdir()
        shutil.copy2(repo / 'scripts/check_staged.py', scripts)
        tests = self.root / 'skills/powerbi-zerodata/tests'
        tests.mkdir(parents=True)
        (tests / 'run_offline.py').write_text('import sys\nassert "--check" in sys.argv\n', encoding='utf-8')
        (tests / 'test_ci_registration.py').write_text('assert True\n', encoding='utf-8')
        (tests / 'smoke.py').write_text('assert True\n', encoding='utf-8')
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        subprocess.run(['git', 'config', 'pbz.requiredCI', 'main-offline-required'], cwd=self.root, check=True)
        bins = self.root / 'bin'
        bins.mkdir()
        record = self.root / 'dotnet-calls'
        shim = bins / 'dotnet'
        shim.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$PBZ_DOTNET_RECORD"\n', encoding='utf-8')
        shim.chmod(0o755)
        python = bins / 'python'
        # Python path is quoted by the shell wrapper, including spaces on Windows.
        import shlex
        python.write_text('#!/bin/sh\nexec ' + shlex.quote(sys.executable.replace(chr(92), '/')) + ' "$@"\n', encoding='utf-8')
        python.chmod(0o755)
        env = {**os.environ, 'PATH': str(bins) + os.pathsep + os.environ['PATH'], 'PBZ_DOTNET_RECORD': str(record)}
        hook = repo / 'scripts/git-hooks/pre-commit'
        def snapshot():
            return (subprocess.check_output(['git', 'ls-files', '--stage'], cwd=self.root),
                    {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*')
                     if p.is_file() and '.git' not in p.relative_to(self.root).parts})
        before = snapshot()
        out = subprocess.run(['sh', str(hook)], cwd=self.root, env=env, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(record.exists())
        self.assertEqual(snapshot(), before)
        # Working tree passes but the staged script fails.
        (tests / 'smoke.py').write_text('broken syntax (\n', encoding='utf-8')
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        (tests / 'smoke.py').write_text('assert True\n', encoding='utf-8')
        before = snapshot()
        out = subprocess.run(['sh', str(hook)], cwd=self.root, env=env, capture_output=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(snapshot(), before)
        # Full pre-push publishes; a deliberately minimal runner can then refuse.
        out = subprocess.run(['sh', str(repo / 'scripts/git-hooks/pre-push')], cwd=self.root, env=env, capture_output=True)
        self.assertIn('publish', record.read_text(encoding='utf-8'))

    def install(self, chain=False):
        return self.inst.install(str(self.root), str(self.source), chain)

    def snapshot(self):
        return {str(p.relative_to(self.hooks)): p.read_bytes() for p in self.hooks.iterdir() if p.is_file()}

    def test_preflight_second_foreign_hook(self):
        (self.hooks / "pre-push").write_bytes(b"foreign")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(before, self.snapshot())
        self.assertFalse((self.hooks / "pre-commit").exists())

    def test_idempotent_and_user_edit_refused(self):
        self.install()
        before = self.snapshot()
        self.install()
        self.assertEqual(before, self.snapshot())
        with (self.hooks / "pre-push").open("ab") as f:
            f.write(b"# user edit\n")
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(before, self.snapshot())

    def test_chain_bytes_args_stdin_and_status(self):
        host = b'#!/bin/sh\nprintf "host:%s" "$*"\ncat\nexit "${HOST_STATUS:-0}"\n'
        (self.hooks / "pre-push").write_bytes(host)
        self.install(chain=True)
        self.assertEqual((self.hooks / "pre-push.pbz-host").read_bytes(), host)
        for status in (0, 7):
            out = subprocess.run(["sh", str(self.hooks / "pre-push"), "origin", "a b"], input=b"push-input\n",
                                 capture_output=True, env={**os.environ, "HOST_STATUS": str(status)})
            self.assertEqual(out.returncode, status)
            self.assertEqual(out.stdout, b"host:origin a bpush-input\n" +
                             (b"pbz:origin a bpush-input\n" if status == 0 else b""))
        self.install(chain=True)
        self.assertEqual((self.hooks / "pre-push.pbz-host").read_bytes(), host)

    def test_shared_hooks_path_requires_opt_in(self):
        self.hooks = self.root / "shared-hooks"
        subprocess.run(["git", "-C", str(self.root), "config", "core.hooksPath", str(self.hooks)], check=True)
        with self.assertRaises(ValueError):
            self.install()
        self.assertFalse(self.hooks.exists())
        self.install(chain=True)
        before = self.snapshot()
        with self.assertRaises(ValueError):
            self.install()
        self.assertEqual(before, self.snapshot())


def main() -> int:
    fails: list[str] = []

    def check(cond, msg):
        if not cond:
            fails.append(msg)

    suite = unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(cls)
                                for cls in (OutputBudgetTests, GitHookTests, InstallerTransactionTests)])
    check(unittest.TextTestRunner().run(suite).wasSuccessful(), "budget / git hook fixtures")
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
    installer_ownership_checks(check, tmp, inst)

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



def installer_ownership_checks(check, tmp, inst):
    # Hook/deny ownership is independent of MCP registration.
    settings = os.path.join(tmp, "owned-settings.json")
    host = {"type": "command", "command": "host-hook"}
    ours = {"type": "command", "command": inst.hook_cmd("claude"), "timeout": 15}
    originals = [{}, {"permissions": {}}, {"permissions": {"deny": []}},
                 {"hooks": {}}, {"hooks": {"PreToolUse": []}},
                 {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": []}]}},
                 {"permissions": {"deny": [inst.DENY_RULES[1], "host", inst.DENY_RULES[0]]},
                  "hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [host]}]}}]
    for original in originals:
        inst.save_json(settings, original, False)
        check(inst.settings_step(settings, "claude", False, False, True), "install added nothing")
        installed = inst.load_json(settings)
        if original.get("hooks", {}).get("PreToolUse"):
            check(installed["hooks"]["PreToolUse"][0] == original["hooks"]["PreToolUse"][0], "mixed host hooks changed")
        before = os.stat(settings).st_mtime_ns
        sidecar = Path(settings + ".pbz-owned.json")
        side_before = (sidecar.stat().st_mtime_ns, sidecar.read_bytes())
        check(not inst.settings_step(settings, "claude", False, False, True), "reinstall changed settings")
        check((sidecar.stat().st_mtime_ns, sidecar.read_bytes()) == side_before, "reinstall wrote sidecar")
        check(os.stat(settings).st_mtime_ns == before, "reinstall wrote unchanged settings")
        inst.settings_step(settings, "claude", False, True, True)
        check(inst.load_json(settings) == original, f"uninstall lost host settings: {original}")
        check(not sidecar.exists(), "full uninstall retained sidecar")
    os.remove(settings)
    check(not inst.settings_step(settings, "claude", False, True, True) and not os.path.exists(settings),
          "uninstall created absent settings")
    inst.settings_step(settings, "claude", False, False, True)
    inst.settings_step(settings, "claude", False, True, True)
    check(not os.path.exists(settings), "installer-created settings file remained empty")
    inst.save_json(settings, {"permissions": {"deny": inst.DENY_RULES[:]}}, False)
    before = os.stat(settings).st_mtime_ns
    check(not inst.settings_step(settings, "claude", False, True, True), "missing ownership removed denies")
    check(os.stat(settings).st_mtime_ns == before, "missing ownership wrote settings")
    identical = {"permissions": {"deny": inst.DENY_RULES[:]}, "hooks": {
        "PreToolUse": [{"matcher": ".*", "hooks": [ours, host]}],
        "SessionStart": [{"hooks": [ours]}]}}
    inst.save_json(settings, identical, False)
    before = os.stat(settings).st_mtime_ns
    check(not inst.settings_step(settings, "claude", False, False, True), "identical install changed settings")
    check(os.stat(settings).st_mtime_ns == before and os.path.exists(settings + ".pbz-owned.json"),
          "pre-record guard was not adopted")
    check(inst.load_json(settings) == identical, "adoption duplicated guard")
    inst.settings_step(settings, "claude", False, True, True)
    expected = {"permissions": {"deny": inst.DENY_RULES[:]}, "hooks": {
        "PreToolUse": [{"matcher": ".*", "hooks": [host]}], "SessionStart": []}}
    check(inst.load_json(settings) == expected, "adopted guard not removed or host lost")
    check(not os.path.exists(settings + ".pbz-owned.json"), "adoption uninstall retained sidecar")

    narrow = {"hooks": {"PreToolUse": [{"matcher": "Read", "hooks": [ours]}]}}
    inst.save_json(settings, narrow, False)
    inst.settings_step(settings, "claude", False, False)
    installed = inst.load_json(settings)
    check(any(e.get("matcher") == ".*" and ours in e["hooks"] for e in installed["hooks"]["PreToolUse"]),
          "narrow host guard suppressed required guard")
    later = {"matcher": ".*", "hooks": [ours, host], "host_metadata": True}
    installed["hooks"]["PreToolUse"].append(later)
    inst.save_json(settings, installed, False)
    inst.settings_step(settings, "claude", False, True)
    check(inst.load_json(settings) == {"hooks": {"PreToolUse": narrow["hooks"]["PreToolUse"] + [later]}},
          "uninstall chose later host group")
    check(not os.path.exists(settings + ".pbz-owned.json"), "full uninstall retained sidecar")

    stale = {**ours, "command": ours["command"] + " --old"}
    inst.save_json(settings, {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [stale]}]}}, False)
    inst.settings_step(settings, "claude", False, False)
    installed = inst.load_json(settings)
    check(installed["hooks"]["PreToolUse"] == [{"matcher": ".*", "hooks": [ours]}], "stale guard retained")
    inst.settings_step(settings, "claude", False, True)
    check(not os.path.exists(settings + ".pbz-owned.json"), "stale replacement retained sidecar")
    inst.save_json(settings, {"permissions": {"deny": [inst.DENY_RULES[0]]}}, False)
    inst.settings_step(settings, "claude", False, False, True)
    os.remove(settings + ".pbz-owned.json")
    installed = inst.load_json(settings)
    inst.settings_step(settings, "claude", False, True, True)
    check(inst.load_json(settings) == installed, "missing ownership failed safe preservation")
    backups = lambda: {n for n in os.listdir(tmp) if n.startswith("owned-settings.json.bak-pbz-")}
    before_backups = backups()
    with patch.object(inst, "stamp", return_value="fixed-timestamp"):
        inst.backup(settings, False)
        inst.backup(settings, False)
    check(len(backups() - before_backups) == 2, "backups collided")

    for uninstall in (False, True):
        for what_if in (False, True):
            for changed in (False, True):
                argv = ["install.py"] + (["--uninstall"] if uninstall else []) + (["--what-if"] if what_if else [])
                with patch.object(sys, "argv", argv), patch.object(inst, "LINKS", []), \
                        patch.object(inst, "RULE_FILES", []), patch.object(inst, "_refuse_spaced_paths"), \
                        patch.object(inst, "settings_step", return_value=changed), \
                        patch.object(inst, "mcp_step", return_value=[]), patch("builtins.print") as output:
                    inst.main()
                lines = [str(c) for c in output.call_args_list]
                check(any(("hooks + deny rules -> " if changed else "ok ") + inst.CLAUDE_SETTINGS in l for l in lines),
                      "Claude settings status missing")
                check(any(("hooks -> " if changed else "ok ") + inst.CODEX_HOOKS in l for l in lines),
                      "Codex settings status missing")


if __name__ == "__main__":
    if "--installer-transactions" in sys.argv:
        unittest.main(argv=[sys.argv[0]], defaultTest="InstallerTransactionTests")
    if "--output-budget" in sys.argv:
        unittest.main(argv=[sys.argv[0]], defaultTest=("OutputBudgetTests", "GitHookTests", "InstallerTransactionTests"))
    if "--installer-ownership" in sys.argv:
        spec = importlib.util.spec_from_file_location("pbz_install", os.path.join(SKILL, "install", "install.py"))
        inst = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(inst)
        failures = []
        with tempfile.TemporaryDirectory() as tmp:
            installer_ownership_checks(lambda ok, msg: failures.append(msg) if not ok else None, tmp, inst)
        for failure in failures:
            print("FAIL", failure)
        print("ok (installer ownership fixtures)" if not failures else f"{len(failures)} failure(s)")
        sys.exit(bool(failures))
    sys.exit(main())
