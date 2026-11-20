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
import threading
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

    def test_reinstall_preserves_later_identical_host_hook(self):
        inst = self.inst
        child = {"type": "command", "command": inst.hook_cmd("claude"), "timeout": 15}
        host = {"matcher": ".*", "hooks": [child, {"type": "command", "command": "host-hook"}],
                "host_metadata": True}
        host_bytes = json.dumps(host).encode()
        for remove_recorded in (False, True):
            with self.subTest(remove_recorded=remove_recorded), tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp, "settings.json"))
                inst.settings_step(path, "claude", False, False)
                installed = inst.load_json(path)
                if remove_recorded:
                    installed["hooks"]["PreToolUse"].clear()
                installed["hooks"]["PreToolUse"].append(json.loads(host_bytes))
                inst.save_json(path, installed, False)
                inst.settings_step(path, "claude", False, False)
                reinstalled = inst.load_json(path)["hooks"]["PreToolUse"]
                self.assertEqual(len(reinstalled), 2)
                self.assertIn({"matcher": ".*", "hooks": [child]}, reinstalled)
                inst.settings_step(path, "claude", False, True)
                remaining = inst.load_json(path)["hooks"]["PreToolUse"]
                self.assertEqual(remaining, [host])
                self.assertEqual(json.dumps(remaining[0]).encode(), host_bytes)

    def test_unrelated_guard_mentions_survive_install_and_uninstall(self):
        inst = self.inst
        for command in ("echo powerbi-zerodata && python /some/other/guard.py",
                        "python -I -S /unrelated/powerbi-zerodata/pbz/guard.py --agent claude",
                        "python -I -S /some/other/guard.py --agent claude # powerbi-zerodata"):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp, "settings.json"))
                original = {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [
                    {"type": "command", "command": command}]}]}}
                self.assertFalse(inst.is_ours(original["hooks"]["PreToolUse"][0]))
                inst.save_json(path, original, False)
                before = Path(path).read_bytes()
                inst.settings_step(path, "claude", False, False)
                self.assertEqual(inst.load_json(path)["hooks"]["PreToolUse"][0], original["hooks"]["PreToolUse"][0])
                inst.settings_step(path, "claude", False, True)
                self.assertEqual(Path(path).read_bytes(), before)

    def test_host_keys_and_nested_tables_refused_on_apply_update_uninstall(self):
        clean, _ = self.inst.merge_codex_toml('', False)
        for host in ('extra = "host"\n', '[mcp_servers.pbz.env]\nHOST = "keep"\n',
                     '[mcp_servers.pbz.nested.deep]\nvalue = 1\n'):
            text = clean.replace(self.inst._toml_str(self.inst.python_exe()), '"old-python"') + host
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

    def test_production_settings_transactions_serialize_distinct_updates(self):
        inst = self.inst
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp, 'settings.json'))
            entered, release, second_started, finished = (threading.Event() for _ in range(4))
            errors = []
            original = inst.merge_claude_mcp
            def blocked(data, uninstall):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('merge release timeout')
                return original(data, uninstall)
            def worker(agent):
                try:
                    if agent == 'codex': second_started.set()
                    if agent == 'claude':
                        inst.mcp_step(False, False)
                    else:
                        inst.settings_step(path, 'codex', False, False, denies=True)
                except BaseException as error:
                    errors.append(error)
                finally:
                    if agent == 'codex': finished.set()
            with patch.object(inst, 'merge_claude_mcp', side_effect=blocked), patch.object(inst, 'CLAUDE_JSON', path), patch.object(inst, 'CODEX_CONFIG', str(Path(tmp, 'config.toml'))):
                first = threading.Thread(target=worker, args=('claude',))
                second = threading.Thread(target=worker, args=('codex',))
                first.start()
                try:
                    self.assertTrue(entered.wait(5))
                    second.start()
                    self.assertTrue(second_started.wait(5))
                    self.assertFalse(finished.wait(.2))
                finally:
                    release.set()
                    first.join(5)
                    if second.ident is not None: second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            data = inst.load_json(path)
            commands = [h['command'] for entries in data['hooks'].values() for entry in entries for h in entry['hooks']]
            self.assertEqual(data['mcpServers']['pbz']['args'], inst.mcp_args())
            self.assertTrue(any('--agent codex' in c for c in commands))
            self.assertTrue(all(rule in data['permissions']['deny'] for rule in inst.DENY_RULES))

    def test_absent_noop_lock_cleans_only_created_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'new-parent', 'settings.json')
            self.inst.settings_step(str(path), 'codex', False, True)
            self.assertFalse(path.parent.exists())
            path.parent.mkdir()
            lock = Path(str(path) + '.pbz-lock')
            lock.write_bytes(b'host lock')
            with self.inst.file_lock(str(path)):
                pass
            self.assertEqual(lock.read_bytes(), b'host lock')
            path.write_text('{}')
            with self.inst.file_lock(str(path)):
                pass
            self.assertTrue(lock.exists())

    def test_absent_noop_lock_removes_all_created_ancestors(self):
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp, 'existing')
            existing.mkdir()
            path = existing / 'first' / 'second' / 'settings.json'
            self.inst.settings_step(str(path), 'codex', False, True)
            self.assertFalse((existing / 'first').exists())
            self.assertTrue(existing.is_dir())

    def test_absent_noop_lock_tolerates_unlink_refusal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'settings.json')
            with patch.object(self.inst.os, 'unlink', side_effect=PermissionError('open sidecar')):
                with self.inst.file_lock(str(path)):
                    pass
            self.assertTrue(Path(str(path) + '.pbz-lock').exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX flock and unlink of open file')
    def test_absent_noop_lock_unlinks_before_unlocking(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'settings.json')
            lock = str(path) + '.pbz-lock'
            unlink = os.unlink
            def checked_unlink(name):
                with open(name, 'a+b') as probe:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                unlink(name)
            with patch.object(self.inst.os, 'unlink', side_effect=checked_unlink):
                with self.inst.file_lock(str(path)):
                    pass
            self.assertFalse(Path(lock).exists())

    @unittest.skipIf(os.name == 'nt', 'POSIX flock and unlink of open file')
    def test_absent_noop_lock_waiter_retries_replaced_inode(self):
        import fcntl
        inst = self.inst
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'settings.json')
            lock = str(path) + '.pbz-lock'
            opened, replaced, entered, third_entered, release = (threading.Event() for _ in range(5))
            errors = []
            flock = fcntl.flock
            def gated_flock(fd, operation):
                if threading.current_thread() is waiter and operation & fcntl.LOCK_EX and not opened.is_set():
                    opened.set()
                    if not replaced.wait(5):
                        raise RuntimeError('replacement timeout')
                return flock(fd, operation)
            def writer(is_waiter):
                try:
                    with inst.file_lock(str(path)):
                        (entered if is_waiter else third_entered).set()
                        if not release.wait(5):
                            raise RuntimeError('writer release timeout')
                        path.write_text('{}')
                except BaseException as error:
                    errors.append(error)
            waiter = threading.Thread(target=writer, args=(True,))
            third = threading.Thread(target=writer, args=(False,))
            with open(lock, 'x+b') as holder, patch.object(fcntl, 'flock', side_effect=gated_flock):
                flock(holder.fileno(), fcntl.LOCK_EX)
                waiter.start()
                try:
                    self.assertTrue(opened.wait(5))
                    # Model an absent no-op holder's cleanup explicitly: the
                    # waiter already opened its inode, then cleanup unlinks it.
                    # Gate acquisition so the third writer holds the replacement
                    # before the waiter acquires and validates the stale inode.
                    os.unlink(lock)
                    flock(holder.fileno(), fcntl.LOCK_UN)
                    third.start()
                    self.assertTrue(third_entered.wait(5))
                    replaced.set()
                    self.assertFalse(entered.wait(.2), 'writers entered on different lock inodes')
                finally:
                    replaced.set()
                    release.set()
                    waiter.join(5)
                    if third.ident is not None: third.join(5)
            self.assertFalse(waiter.is_alive())
            self.assertFalse(third.is_alive())
            self.assertTrue(entered.is_set())
            self.assertEqual(errors, [])

    @unittest.skipIf(os.name == 'nt', 'POSIX flock')
    def test_posix_lock_timeout_is_named_conflict(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp, patch.object(fcntl, 'flock', side_effect=BlockingIOError()), patch.object(self.inst.time, 'monotonic', side_effect=[0, 61]):
            with self.assertRaisesRegex(self.inst.InstallConflict, 'settings.json: installer lock timeout'):
                with self.inst.file_lock(str(Path(tmp, 'settings.json'))):
                    self.fail('lock acquired')

    @unittest.skipIf(os.name == 'nt', 'POSIX modes and symlink')
    def test_atomic_text_preserves_mode_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, 'target')
            target.write_text('original')
            target.chmod(0o751)
            link = Path(tmp, 'link')
            link.symlink_to(target)
            self.inst.atomic_text(str(link), 'updated')
            self.assertTrue(link.is_symlink())
            self.assertEqual(target.read_text(), 'updated')
            self.assertEqual(target.stat().st_mode & 0o777, 0o751)

    def test_single_quoted_pbz_apply_update_uninstall(self):
        import tomllib
        inst = self.inst
        clean, _ = inst.merge_codex_toml('', False)
        quoted = clean.replace('[mcp_servers.pbz]', "[mcp_servers.'pbz']")
        for text in ('', quoted, quoted.replace(inst._toml_str(inst.python_exe()), '"old-python"')):
            updated, reason = inst.merge_codex_toml(text, False)
            self.assertFalse(reason.startswith('SKIP'), reason)
            self.assertEqual(tomllib.loads(updated)['mcp_servers']['pbz']['command'], inst.python_exe())
            removed, reason = inst.merge_codex_toml(updated.replace('[mcp_servers.pbz]', "[mcp_servers.'pbz']"), True)
            self.assertEqual(reason, 'removed')
            self.assertNotIn('mcp_servers', tomllib.loads(removed))
        for suffix in (".'nested'", '."nested]key"'):
            text = quoted + "[mcp_servers.'pbz'" + suffix + "]\nvalue = 1\n"
            self.assertEqual(inst.merge_codex_toml(text, False)[0], text)

    def test_main_reports_conflicts_and_mcp_skips(self):
        inst = self.inst
        with tempfile.TemporaryDirectory() as tmp, patch.object(inst, 'LINKS', []), patch.object(inst, 'RULE_FILES', []), patch.object(inst, 'CLAUDE_SETTINGS', str(Path(tmp, 'claude-settings'))), patch.object(inst, 'CODEX_HOOKS', str(Path(tmp, 'codex-hooks'))), patch.object(inst, 'CLAUDE_JSON', str(Path(tmp, 'claude.json'))), patch.object(inst, 'CODEX_CONFIG', str(Path(tmp, 'config.toml'))), patch.object(sys, 'argv', ['install.py', '--mcp']):
            path = Path(inst.CODEX_CONFIG)
            path.write_text('[mcp_servers.pbz]\ncommand = "host"\n')
            before = path.read_bytes()
            self.assertEqual(inst.main(), 1)
            self.assertEqual(path.read_bytes(), before)
            for target in ('junction', 'settings_step', 'rule_block', 'mcp_step'):
                with self.subTest(target=target), patch.object(inst, 'LINKS', ['link']), patch.object(inst, 'RULE_FILES', ['rules']), patch.object(inst, 'junction', return_value='ok'), patch.object(inst, 'settings_step', return_value=False), patch.object(inst, 'rule_block', return_value='ok'), patch.object(inst, target, side_effect=inst.InstallConflict('named-file refusal')):
                    self.assertEqual(inst.main(), 1)

    def test_main_uninstall_refuses_mcp_conflicts_without_changing_bytes(self):
        inst = self.inst
        clean, _ = inst.merge_codex_toml('', False)
        conflicts = ('[mcp_servers.pbz]\ncommand = "host"\n',
                     clean + 'host_owned = true\n', 'invalid = [\n')
        with tempfile.TemporaryDirectory() as tmp, patch.object(inst, 'LINKS', []), patch.object(inst, 'RULE_FILES', []), patch.object(inst, 'CLAUDE_SETTINGS', str(Path(tmp, 'claude-settings'))), patch.object(inst, 'CODEX_HOOKS', str(Path(tmp, 'codex-hooks'))), patch.object(inst, 'CLAUDE_JSON', str(Path(tmp, 'claude.json'))), patch.object(inst, 'CODEX_CONFIG', str(Path(tmp, 'config.toml'))), patch.object(sys, 'argv', ['install.py', '--uninstall']):
            for text in conflicts:
                with self.subTest(text=text):
                    path = Path(inst.CODEX_CONFIG)
                    path.write_text(text, encoding='utf-8')
                    before = path.read_bytes()
                    self.assertNotEqual(inst.main(), 0)
                    self.assertEqual(path.read_bytes(), before)
            Path(inst.CODEX_CONFIG).unlink()
            self.assertEqual(inst.main(), 0)
            self.assertFalse(Path(inst.CODEX_CONFIG).exists())
            self.assertFalse(Path(inst.CLAUDE_JSON).exists())

    def test_interrupting_settings_file_write_preserves_original_bytes(self):
        self._interrupt_settings_transaction(False)

    def test_interrupting_ownership_record_write_preserves_original_bytes(self):
        self._interrupt_settings_transaction(True)

    def _interrupt_settings_transaction(self, ownership):
        inst = self.inst
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'settings.json')
            inst.settings_step(str(path), 'codex', False, False)
            record = Path(str(path) + '.pbz-owned.json')
            target = record if ownership else path
            before = target.read_bytes()
            original = inst.atomic_text
            def interrupt(dest, text, what_if=False):
                if str(dest) == str(target):
                    def partial(handle, text):
                        handle.write(text[:3])
                        raise KeyboardInterrupt('injected write interruption')
                    with patch.object(inst, '_write_content', side_effect=partial):
                        original(dest, text, what_if)
                else:
                    original(dest, text, what_if)
            with patch.object(inst, 'atomic_text', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
                inst.settings_step(str(path), 'claude', False, False, denies=True)
            self.assertEqual(target.read_bytes(), before)

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
    def run_child(self, code, timeout=5, mid=0):
        return mcp._bounded_output([sys.executable, "-c", code], os.getcwd(), os.environ.copy(), timeout, mid)

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
        # A fresh interpreter avoids inherited RSS high-water marks. tracemalloc
        # detects even a fully released stderr buffer, independently of stdout.
        code = """
import os, sys, tracemalloc
sys.path.insert(0, sys.argv[1])
from pbz import mcp
tracemalloc.start()
result = mcp._bounded_output(
    [sys.executable, "-c", "import os; os.write(2,b'x'*10000000); os.write(1,b'ok')"],
    os.getcwd(), os.environ.copy(), 10)
_, peak = tracemalloc.get_traced_memory()
assert result == (0, b"ok", None), result
assert peak < 2000000, peak
"""
        result = subprocess.run([sys.executable, "-c", code, SKILL],
                                capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

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
        with patch.object(mcp, "DECODED_LIMIT", 10):
            self.assertEqual(self.run_child("import os; os.write(1, 'é'.encode()*10)"), (0, 'é'.encode()*10, None))
            _code, raw, error = self.run_child("import os; os.write(1, 'é'.encode()*11)")
            self.assertEqual((raw, error), (b"", "OUTPUT_LIMIT"))
            for count in (10, 11):
                with patch.object(mcp, "_bounded_output", return_value=(0, b"x"*count, None)):
                    result = mcp.call_tool("status", {}, os.getcwd(), None)
                self.assertEqual(result["isError"], count > 10)
                if count > 10:
                    self.assertEqual(json.loads(result["content"][0]["text"])["error"], "OUTPUT_LIMIT")
                else:
                    self.assertEqual(result["content"][0]["text"], "x" * 10)

    def test_wire_fallback_discards_original_error_and_huge_id(self):
        with patch.object(mcp, "WIRE_LIMIT", 1024):
            response = {"jsonrpc": "2.0", "id": "i" * 10000, "error": {"message": "e" * 10000},
                        "result": mcp._result({"value": "x" * 10000}, False)}
            result = mcp._wire_response(response, mcp.BY_NAME["rename"])
            self.assertLessEqual(len(mcp._encode(result)), 1024)
            self.assertNotIn("error", result)
            self.assertIsNone(result["id"])
            self.assertEqual(json.loads(result["result"]["content"][0]["text"])["error"], "OUTPUT_LIMIT")

    def test_timeout_detail_and_read_only_outcome(self):
        for name, args in (("status", {}), ("rename", {"kind": "column", "old": "A", "new": "B"})):
            with patch.object(mcp, "_bounded_output", return_value=(-9, b"", "TIMEOUT")):
                result = mcp.call_tool(name, args, os.getcwd(), None)
            body = json.loads(result["content"][0]["text"])
            self.assertEqual(body["detail"], f"{name} took longer than {mcp.BY_NAME[name].timeout}s")
            self.assertEqual("outcome" in body, name == "rename")
            self.assertEqual("recovery" in body, name == "rename")

    def test_stdout_eof_waits_until_tool_deadline(self):
        # More than the former fixed 5 second wait, but within the tool deadline.
        self.assertEqual(self.run_child("import os,time; os.write(1,b'ok'); os.close(1); time.sleep(5.3)", timeout=10),
                         (0, b"ok", None))

    def test_read_error_refuses_partial_output(self):
        read = os.read
        def broken(fd, size):
            if threading.current_thread() is not threading.main_thread():
                raise OSError("read failed")
            return read(fd, size)
        with patch.object(mcp.os, "read", side_effect=broken):
            _code, raw, error = self.run_child("import time; time.sleep(10)")
        self.assertEqual((raw, error), (b"", "READ_ERROR"))
        with patch.object(mcp, "_bounded_output", return_value=(-9, b"partial", "READ_ERROR")):
            result = mcp.call_tool("status", {}, os.getcwd(), None)
        self.assertEqual(json.loads(result["content"][0]["text"])["error"], "READ_ERROR")
        self.assertTrue(result["isError"])

    def test_production_budget_uses_real_request_id(self):
        mid = "id" * 100
        envelope = {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": "x" * 20}], "isError": False}}
        with patch.object(mcp, "WIRE_LIMIT", len(mcp._encode(envelope))):
            self.assertEqual(self.run_child("import os; os.write(1,b'x'*20)", mid=mid), (0, b"x"*20, None))
            _code, raw, error = self.run_child("import os; os.write(1,b'x'*21)", mid=mid)
            self.assertEqual((raw, error), (b"", "OUTPUT_LIMIT"))
            bounded = mcp._bounded_output
            for count in (20, 21):
                def child(*args):
                    return bounded([sys.executable, "-c", f"import os; os.write(1,b'x'*{count})"],
                                   *args[1:])
                with patch.object(mcp, "_bounded_output", side_effect=child):
                    response = mcp.handle(rpc(mid, "tools/call", {"name": "status", "arguments": {}}),
                                          os.getcwd(), None)
                if count == 20:
                    self.assertEqual(response, envelope)
                    self.assertEqual(len(mcp._encode(response)), mcp.WIRE_LIMIT)
                else:
                    self.assertTrue(response["result"]["isError"])
                    self.assertEqual(json.loads(response["result"]["content"][0]["text"])["error"], "OUTPUT_LIMIT")
        with patch.object(mcp, "_bounded_output", return_value=(0, b"ok", None)) as bounded:
            mcp.handle(rpc(mid, "tools/call", {"name": "status", "arguments": {}}), os.getcwd(), None)
        self.assertEqual(bounded.call_args.args[-1], mid)


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
        (tests / 'run_offline.py').write_text('import sys, pathlib\nif "--check" not in sys.argv: pathlib.Path("runner-called").write_text("full")\n', encoding='utf-8')
        (tests / 'test_ci_registration.py').write_text('import smoke\n', encoding='utf-8')
        (tests / 'test_plan_pure.py').write_text('assert True\n', encoding='utf-8')
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
        # An inherited attestation cannot enable fast mode.
        global_config = self.root.parent / (self.root.name + '-gitconfig')
        global_config.write_text('[pbz]\nrequiredCI = main-offline-required\n')
        self.addCleanup(lambda: global_config.unlink(missing_ok=True))
        env['GIT_CONFIG_GLOBAL'] = str(global_config)
        # Default mode must publish and run the full gate, with no content mutation.
        subprocess.run(['git', 'config', '--local', '--unset', 'pbz.requiredCI'], cwd=self.root, check=True)
        runner_record = tests.parent / 'runner-called'
        # Put instrumentation outside the snapshot while preserving fixture content.
        original_runner = (tests / 'run_offline.py').read_text()
        (tests / 'run_offline.py').write_text(original_runner.replace('pathlib.Path("runner-called")', 'pathlib.Path(' + repr(str(self.root.parent / (self.root.name + '-runner'))) + ')'))
        runner_record = self.root.parent / (self.root.name + '-runner')
        self.addCleanup(lambda: runner_record.unlink(missing_ok=True))
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        before = snapshot()
        out = subprocess.run(['sh', str(hook)], cwd=self.root, env=env, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('publish', record.read_text())
        self.assertTrue(runner_record.exists())
        # Remove the instrumentation output before comparing fixture bytes.
        record.unlink()
        self.assertEqual(snapshot(), before)
        subprocess.run(['git', 'config', '--local', 'pbz.requiredCI', 'main-offline-required'], cwd=self.root, check=True)
        # A tampered working-tree checker cannot hide a staged runtime failure.
        (scripts / 'check_staged.py').write_text('raise SystemExit(0)\n')
        (tests / 'smoke.py').write_text('raise RuntimeError("staged smoke runtime failure")\n')
        subprocess.run(['git', 'add', 'skills'], cwd=self.root, check=True)
        (tests / 'smoke.py').write_text('assert True\n')
        before = snapshot()
        out = subprocess.run(['sh', str(hook)], cwd=self.root, env=env, capture_output=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(b'staged smoke runtime failure', out.stderr)
        self.assertEqual(snapshot(), before)
        # Working tree passes but the staged script fails.
        shutil.copy2(repo / 'scripts/check_staged.py', scripts / 'check_staged.py')
        (tests / 'smoke.py').write_text('broken syntax (\n', encoding='utf-8')
        subprocess.run(['git', 'add', '.'], cwd=self.root, check=True)
        (tests / 'smoke.py').write_text('assert True\n', encoding='utf-8')
        before = snapshot()
        out = subprocess.run(['sh', str(hook)], cwd=self.root, env=env, capture_output=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(snapshot(), before)
        # Full pre-push publishes; a deliberately minimal runner can then refuse.
        out = subprocess.run(['sh', str(repo / 'scripts/git-hooks/pre-push')], cwd=self.root, env=env, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('publish', record.read_text(encoding='utf-8'))
        (tests / 'run_offline.py').write_text('raise SystemExit(17)\n')
        out = subprocess.run(['sh', str(repo / 'scripts/git-hooks/pre-push')], cwd=self.root, env=env, capture_output=True)
        self.assertEqual(out.returncode, 17)

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

    @unittest.skipIf(os.name == "nt", "direct shell hook execution is POSIX-only")
    def test_chain_bytes_args_stdin_and_status(self):
        host = b'#!/bin/sh\nprintf "host\\n"\nprintf "%s\\n" "$@"\ncat\nexit "${HOST_STATUS:-0}"\n'
        for name in self.inst.NAMES:
            (self.hooks / name).write_bytes(host)
            (self.hooks / name).chmod(0o751)
        self.install(chain=True)
        for name in self.inst.NAMES:
            self.assertTrue(os.access(self.hooks / name, os.X_OK))
        for name in self.inst.NAMES:
            self.assertEqual((self.hooks / (name + ".pbz-host")).read_bytes(), host)
            self.assertEqual((self.hooks / (name + ".pbz-host")).stat().st_mode & 0o777, 0o751)
            for status in (0, 7):
                out = subprocess.run([str(self.hooks / name), "origin", "a b"], input=b"push-input\n",
                                     capture_output=True, env={**os.environ, "HOST_STATUS": str(status)})
                self.assertEqual(out.returncode, status)
                self.assertEqual(out.stdout, b"host\norigin\na b\npush-input\n" +
                                 ((b"pbz:origin a b" + (b"push-input\n" if name == "pre-push" else b"")) if status == 0 else b""))
        self.install(chain=True)
        for name in self.inst.NAMES:
            self.assertEqual((self.hooks / (name + ".pbz-host")).read_bytes(), host)

    @unittest.skipIf(os.name == "nt", "executable mode semantics require POSIX")
    def test_non_executable_host_is_ignored(self):
        for name in self.inst.NAMES:
            host = self.hooks / name
            host.write_bytes(b'#!/bin/sh\nprintf "HOST-MUST-NOT-RUN"\nexit 7\n')
            host.chmod(0o640)
        self.install(chain=True)
        for name in self.inst.NAMES:
            self.assertEqual((self.hooks / (name + ".pbz-host")).stat().st_mode & 0o777, 0o640)
            out = subprocess.run([str(self.hooks / name), "origin", "a b"], input=b"input\n", capture_output=True)
            self.assertEqual(out.returncode, 0)
            self.assertEqual(out.stdout, b"pbz:origin a binput\n")

    def test_chain_runs_through_git_commit_and_push(self):
        subprocess.run(["git", "config", "user.name", "Hook Test"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "hooks@example.test"], cwd=self.root, check=True)
        for name in self.inst.NAMES:
            host = self.hooks / name
            host.write_bytes(b'#!/bin/sh\nprintf "host-run\\n"\nprintf "%s\\n" "$@"\ncat\n')
            host.chmod(0o755)
        self.install(chain=True)
        commit = subprocess.run(["git", "commit", "--allow-empty", "-m", "hook test"], cwd=self.root, capture_output=True)
        self.assertEqual(commit.returncode, 0, commit.stderr)
        self.assertIn(b"host-run", commit.stderr + commit.stdout)
        self.assertIn(b"pbz:", commit.stderr + commit.stdout)
        remote = self.root / "local remote.git"
        subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
        subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=self.root, check=True)
        pushed = subprocess.run(["git", "push", "origin", "HEAD:refs/heads/main"], cwd=self.root, capture_output=True)
        self.assertEqual(pushed.returncode, 0, pushed.stderr)
        output = pushed.stderr + pushed.stdout
        self.assertIn(b"host-run\norigin\n" + str(remote).encode() + b"\n", output)
        self.assertIn(b"pbz:origin " + str(remote).encode(), output)
        self.assertGreaterEqual(output.count(b"refs/heads/main"), 2)

    def test_replace_failure_rolls_back_sidecars_and_can_retry(self):
        host = self.hooks / "pre-commit"
        host.write_bytes(b"#!/bin/sh\nexit 0\n")
        host.chmod(0o640)
        before_mode = host.stat().st_mode & 0o777
        before = self.snapshot()
        replace = self.inst.os.replace
        calls = 0
        def fail_once(src, dest):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("replace failed")
            return replace(src, dest)
        with patch.object(self.inst.os, "replace", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.install(chain=True)
        self.assertEqual(self.snapshot(), before)
        if os.name == "posix":
            self.assertEqual(host.stat().st_mode & 0o777, before_mode)
        self.install(chain=True)

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

    for executable in ("/fixture/python3.13t.exe", "/fixture/pythonw.exe"):
        with patch.object(inst, "python_exe", return_value=executable):
            for agent in ("claude", "codex"):
                check(inst.is_ours({"hooks": [{"command": inst.hook_cmd(agent)}]}),
                      f"exact emitted {agent} command unrecognized: {executable}")
            cycle = os.path.join(tmp, "executable-settings.json")
            inst.settings_step(cycle, "claude", False, False)
            inst.settings_step(cycle, "claude", False, False)
            groups = inst.load_json(cycle)["hooks"]["PreToolUse"]
            command = inst.hook_cmd("claude")
            check(sum(h.get("command") == command for g in groups for h in g["hooks"]) == 1,
                  "unusual executable duplicated guard on reinstall")
            inst.settings_step(cycle, "claude", False, True)
            check(not os.path.exists(cycle), "unusual executable guard survived uninstall")
    with patch.object(inst, "python_exe", return_value="/path with spaces/python.exe"):
        check(not inst.is_ours({"hooks": [host]}), "spaced hook command raised or recognized host")
        check(inst.is_ours({"hooks": [ours]}), "spaced current executable disabled legacy recognition")

    guests = os.path.join(tmp, "guest-settings.json")
    inst.settings_step(guests, "claude", False, False)
    installed = inst.load_json(guests)
    installed["hooks"]["PreToolUse"][0]["hooks"].append(host)
    inst.save_json(guests, installed, False)
    inst.settings_step(guests, "claude", False, False)
    groups = inst.load_json(guests)["hooks"]["PreToolUse"]
    check(sum(h == ours for g in groups for h in g["hooks"]) == 1, "host guest duplicated recorded guard")
    inst.settings_step(guests, "claude", False, True)
    check(inst.load_json(guests) == {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [host]}]}},
          "uninstall lost host guest or retained guard")

    for i, extra in enumerate(({}, {"host_metadata": True})):
        separate = os.path.join(tmp, f"separate-settings-{i}.json")
        inst.settings_step(separate, "claude", False, False)
        installed = inst.load_json(separate)
        host_group = {"matcher": ".*", "hooks": [host], **extra}
        host_bytes = json.dumps(host_group).encode()
        installed["hooks"]["PreToolUse"].append(host_group)
        inst.save_json(separate, installed, False)
        inst.settings_step(separate, "claude", False, False)
        check(json.dumps(inst.load_json(separate)["hooks"]["PreToolUse"][-1]).encode() == host_bytes,
              "reinstall changed separate host group bytes")
        inst.settings_step(separate, "claude", False, True)
        remaining = inst.load_json(separate)["hooks"]["PreToolUse"]
        check(len(remaining) == 1 and json.dumps(remaining[0]).encode() == host_bytes,
              "uninstall changed separate host group bytes")

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
                        patch.object(inst, "CLAUDE_SETTINGS", r"C:\Users\fixture\.claude\settings.json"), \
                        patch.object(inst, "CODEX_HOOKS", r"C:\Users\fixture\.codex\hooks.json"), \
                        patch.object(inst, "settings_step", return_value=changed), \
                        patch.object(inst, "mcp_step", return_value=[]), patch("builtins.print") as output:
                    inst.main()
                    lines = [c.args[0] for c in output.call_args_list if c.args]
                    # Status messages use print(" ", message): inspect those real arguments too.
                    lines.extend(arg for c in output.call_args_list for arg in c.args[1:])
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
