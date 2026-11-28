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

    def test_reinstall_reconciles_repeated_host_sibling_edits(self):
        inst = self.inst
        child = {"type": "command", "command": inst.hook_cmd("claude"), "timeout": 15}
        sibling_a = {"type": "command", "command": "host-a"}
        sibling_b = {"type": "command", "command": "host-b"}
        for event in ("PreToolUse", "SessionStart"):
            with self.subTest(event=event), tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp, "settings.json"))
                sidecar = Path(path + ".pbz-owned.json")
                inst.settings_step(path, "claude", False, False)
                stages = ([sibling_a], [sibling_a, sibling_b], [sibling_b],
                          [{**sibling_b, "command": "host-b-edited", "timeout": 30}])
                for siblings in stages:
                    installed = inst.load_json(path)
                    installed["hooks"][event][0]["hooks"] = [child, *siblings]
                    inst.save_json(path, installed, False)
                    inst.settings_step(path, "claude", False, False)
                    groups = inst.load_json(path)["hooks"][event]
                    self.assertEqual(sum(h == child for g in groups for h in g["hooks"]), 1,
                                     f"duplicate guard after siblings became {siblings}")
                    self.assertEqual(len(groups), 1)
                    self.assertEqual(groups[0]["hooks"], [child, *siblings])
                    recorded = [a for a in inst.load_json(str(sidecar))["hooks"]["additions"]
                                if a["event"] == event]
                    self.assertEqual(len(recorded), 1)
                    self.assertEqual(recorded[0]["shell"], {**groups[0], "hooks": siblings})
                inst.settings_step(path, "claude", False, True)
                shell = {"hooks": stages[-1]}
                if event == "PreToolUse":
                    shell["matcher"] = ".*"
                # Exact equality also excludes guards and orphaned empty groups in either event.
                self.assertEqual(inst.load_json(path), {"hooks": {event: [shell]}})
                self.assertFalse(sidecar.exists())

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

    def test_deferred_sidecar_cleanup_requires_identity_and_absent_target(self):
        inst = self.inst
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'settings.json')
            lock = str(path) + '.pbz-lock'
            with open(lock, 'x+b') as handle:
                descriptor = os.fstat(handle.fileno())
                identity = (descriptor.st_dev, descriptor.st_ino)
            # Exercise the Windows cleanup helper with the handle already closed.
            path.write_text('writer content', encoding='utf-8')
            with patch.object(inst.os, 'unlink', wraps=os.unlink) as unlink:
                inst._remove_created_sidecar(lock, identity, not path.exists())
                unlink.assert_not_called()
            self.assertTrue(Path(lock).exists())
            path.unlink()
            # Keep the old inode alive so the replacement cannot reuse it.
            os.rename(lock, lock + '.old')
            Path(lock).write_bytes(b'host lock')
            replacement = os.stat(lock)
            self.assertNotEqual(identity, (replacement.st_dev, replacement.st_ino))
            with patch.object(inst.os, 'unlink', wraps=os.unlink) as unlink:
                inst._remove_created_sidecar(lock, identity, not path.exists())
                unlink.assert_not_called()
            self.assertEqual(Path(lock).read_bytes(), b'host lock')
            with patch.object(inst.os, 'unlink', wraps=os.unlink) as unlink:
                inst._remove_created_sidecar(lock, (replacement.st_dev, replacement.st_ino), not path.exists())
                unlink.assert_called_once_with(lock)
            self.assertFalse(Path(lock).exists())

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
    def test_absent_noop_holder_removes_parents_before_waiter_retries(self):
        import fcntl
        inst = self.inst
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'first', 'second', 'settings.json')
            self.assertFalse(path.parent.exists())
            entered, opened, holder_finished = (threading.Event() for _ in range(3))
            errors = []
            flock = fcntl.flock
            def gated_flock(fd, operation):
                if threading.current_thread() is waiter and operation & fcntl.LOCK_EX and not opened.is_set():
                    # The waiter has opened the holder's sidecar, but must not
                    # validate its inode until the real holder finishes cleanup.
                    opened.set()
                    if not holder_finished.wait(5):
                        raise RuntimeError('holder cleanup timeout')
                    if path.parent.exists():
                        raise RuntimeError('holder did not remove created parents')
                return flock(fd, operation)
            def noop_holder():
                try:
                    with inst.file_lock(str(path)):
                        entered.set()
                        if not opened.wait(5):
                            raise RuntimeError('waiter open timeout')
                except BaseException as error:
                    errors.append(error)
                finally:
                    holder_finished.set()
            def writer():
                try:
                    with inst.file_lock(str(path)):
                        path.write_text('waiting writer', encoding='utf-8')
                except BaseException as error:
                    errors.append(error)
            holder = threading.Thread(target=noop_holder)
            waiter = threading.Thread(target=writer)
            with patch.object(fcntl, 'flock', side_effect=gated_flock):
                holder.start()
                try:
                    self.assertTrue(entered.wait(5))
                    waiter.start()
                    self.assertTrue(opened.wait(5))
                finally:
                    holder.join(5)
                    if waiter.ident is not None: waiter.join(5)
            self.assertFalse(holder.is_alive())
            self.assertFalse(waiter.is_alive())
            self.assertEqual(errors, [])
            self.assertTrue(path.exists())
            self.assertEqual(path.read_text(encoding='utf-8'), 'waiting writer')

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


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        kernel.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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
            pidfile = str(Path(tmp, "child.pid"))
            child = (
                "import os, time; from pathlib import Path; "
                f"Path({pidfile!r}).write_text(str(os.getpid())); time.sleep(30)"
            )
            parent = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(30)"
            )
            _code, raw, error = self.run_child(parent, timeout=0.15)
            self.assertEqual((raw, error), (b"", "TIMEOUT"))
            deadline = time.time() + 3
            while not Path(pidfile).exists() and time.time() < deadline:
                time.sleep(0.05)
            if not Path(pidfile).exists():
                return
            pid = int(Path(pidfile).read_text())
            gone = time.time() + 3
            while _pid_alive(pid) and time.time() < gone:
                time.sleep(0.05)
            self.assertFalse(_pid_alive(pid))

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


def load_catalog():
    spec = importlib.util.spec_from_file_location(
        'pbz_catalog', os.path.join(SKILL, 'install', 'build_visual_catalog.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstallerEntryPointTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack

        scope = self.enterContext(ExitStack())
        self.root = Path(scope.enter_context(tempfile.TemporaryDirectory()))
        self.inst = load_installer()
        self.settings = self.root / 'settings.json'
        self.hooks = self.root / 'hooks.json'
        self.claude = self.root / 'claude.json'
        self.codex = self.root / 'config.toml'
        self.record = Path(str(self.settings) + '.pbz-owned.json')
        self.journal = Path(str(self.settings) + '.pbz-txn.json')
        for name, value in dict(CANON=SKILL, LINKS=[], RULE_FILES=[],
                                CLAUDE_SETTINGS=str(self.settings), CODEX_HOOKS=str(self.hooks),
                                CLAUDE_JSON=str(self.claude), CODEX_CONFIG=str(self.codex)).items():
            scope.enter_context(patch.object(self.inst, name, value))

    def _run(self, *args):
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with patch.object(sys, 'argv', ['install.py', *args]), redirect_stdout(output):
            try:
                code = self.inst.main()
            except (Exception, SystemExit) as error:
                code = f'raised {type(error).__name__}: {error}'
        return code, output.getvalue()

    def _snapshot(self):
        return {path.relative_to(self.root):
                ('link', os.readlink(path)) if path.is_symlink() else
                ('dir', None) if path.is_dir() else ('file', path.read_bytes())
                for path in self.root.rglob('*')}

    def _refuse(self, args, *names):
        before = self._snapshot()
        code, output = self._run(*args)
        self.assertEqual(code, 1, output)
        self.assertEqual(self._snapshot(), before, 'refusal must precede mutations')
        self.assertIn('REFUSE', output)
        for name in names:
            self.assertIn(str(name), output)
        return output

    def test_r10_1_unknown_tokens(self):
        for token in ('--whatif', '--bogus', 'stray'):
            with self.subTest(token=token), tempfile.TemporaryDirectory(dir=self.root) as tmp:
                with patch.object(self.inst, 'CLAUDE_SETTINGS', str(Path(tmp, 'settings.json'))), \
                     patch.object(self.inst, 'CODEX_HOOKS', str(Path(tmp, 'hooks.json'))):
                    output = self._refuse((token,), token)
                for flag in ('--what-if', '--uninstall', '--mcp', '--report-json'):
                    self.assertIn(flag, output)

    def test_r10_1_documented_invocations(self):
        before = self._snapshot()
        self.assertEqual(self._run('--what-if', '--mcp')[0], 0)
        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self._run('--mcp')[0], 0)
        self.assertEqual(self._run('--uninstall')[0], 0)

    def _settings(self, uninstall=False):
        return self.inst.settings_step(str(self.settings), 'claude', False, uninstall, denies=True)

    def test_r10_2_interrupted_install(self):
        user = {'permissions': {'deny': ['Read(user-only)']}}
        self.settings.write_text(json.dumps(user), encoding='utf-8')
        save = self.inst.save_json

        def interrupt(path, data, what_if):
            if str(path) == str(self.record):
                raise KeyboardInterrupt('before ownership save')
            return save(path, data, what_if)

        with patch.object(self.inst, 'save_json', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self._settings()
        code, output = self._run('--uninstall')
        self.assertEqual(code, 0, output)
        self.assertEqual(self.inst.load_json(str(self.settings)), user,
                         'uninstall must remove interrupted-install hooks and denies only')
        self.assertFalse(self.record.exists())
        self.assertFalse(self.journal.exists())

    def test_r10_2_interrupted_uninstall(self):
        self.settings.write_bytes(b'{"permissions":{"deny":["Read(user-only)"]}}')
        self._settings()
        self._settings(True)
        self._settings()
        expected = (self.settings.read_bytes(), self.record.read_bytes())
        remove = os.remove

        def interrupt(path, *args, **kwargs):
            if str(path) == str(self.record):
                raise KeyboardInterrupt('before ownership removal')
            return remove(path, *args, **kwargs)

        with patch.object(self.inst.os, 'remove', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self._settings(True)
        self._settings()
        self.assertEqual((self.settings.read_bytes(), self.record.read_bytes()), expected,
                         'reinstall must reproduce uninterrupted settings and ownership bytes')
        self.assertFalse(self.journal.exists())

    def _journal(self, settings, record, observed, target=None):
        import base64

        def image(contents):
            return None if contents is None else base64.b64encode(contents).decode('ascii')

        target = target or self.settings
        journal = Path(str(target) + '.pbz-txn.json')
        data = {'version': 1,
                'settings': dict(zip(('before', 'after'), map(image, settings))),
                'record': dict(zip(('before', 'after'), map(image, record)))}
        journal.write_text(json.dumps(data), encoding='utf-8')
        for path, contents in zip((target, Path(str(target) + '.pbz-owned.json')), observed):
            if contents is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(contents)
        return data

    def _recover(self, boundary):
        self.settings.write_bytes(b'{"permissions":{"deny":["Read(user-only)"]}}')
        before = self.settings.read_bytes()
        self._settings()
        after, record = self.settings.read_bytes(), self.record.read_bytes()
        observed = (before, None) if boundary == 'journal' else (after, record)
        self._journal((after if boundary == 'record-only' else before, after), (None, record), observed)
        snapshot = self._snapshot()
        code, output = self._run('--what-if')
        self.assertEqual(code, 0, output)
        self.assertEqual(self._snapshot(), snapshot)
        preview = output
        code, output = self._run()
        self.assertEqual(code, 0, output)
        self.assertFalse(self.journal.exists(), 'reconciliation must retire the journal')
        self.assertEqual((self.settings.read_bytes(), self.record.read_bytes()), (after, record))
        self.assertRegex(preview.lower(), r'would[^\n]*reconcil|planned[^\n]*reconcil')

    def test_r10_2_after_journal_write(self):
        self._recover('journal')

    def test_r10_2_after_record_write(self):
        self._recover('record')

    def test_r10_2_completed_record_only_pair(self):
        self._recover('record-only')

    def test_r10_2_unreachable_pair(self):
        self._journal((b'{}', b'{"x":1}'), (None, b'{}'), (b'{}', b'{}'))
        self._refuse(('--what-if',), self.journal)

    def test_r10_2_recovery_instructions(self):
        self._journal((b'{}', b'{"x":1}'), (None, b'{}'), (b'{"user":1}', None))
        output = self._refuse(('--uninstall',), self.settings).lower()
        for wording in (r'preserv|cop(y|ies)', r'before', r'after', r'null',
                        r'verif', r'(delet|remov).*journal', r're.appl'):
            self.assertRegex(output, wording)

    def test_r10_2_edited_settings_image(self):
        self._journal((b'{}', b'{"x":1}'), (None, b'{}'), (b'{"edited":1}', None))
        self._refuse(('--uninstall',), self.settings)

    def test_r10_2_edited_record_image(self):
        self._journal((b'{}', b'{"x":1}'), (None, b'{}'), (b'{"x":1}', b'{"edited":1}'))
        self._refuse(('--uninstall',), self.record)

    def test_r10_2_corrupt_journal(self):
        self.journal.write_bytes(b'{')
        output = self._refuse((), self.journal).lower()
        for wording in (r'preserv|cop(y|ies)', r'aside', r'ownership.*unknown'):
            self.assertRegex(output, wording)

    def test_r10_2_invalid_journal_images(self):
        for reason in ('version', 'missing image', 'base64'):
            with self.subTest(reason=reason):
                data = self._journal((None, b'{}'), (None, b'{}'), (None, None))
                if reason == 'version':
                    data['version'] = 2
                elif reason == 'missing image':
                    del data['settings']['after']
                else:
                    data['settings']['after'] = '***'
                self.journal.write_text(json.dumps(data), encoding='utf-8')
                self._refuse((), self.journal)

    def test_r10_2_user_denies_survive_cycle(self):
        rules = ['Read(user-only)', self.inst.DENY_RULES[0]]
        user = {'permissions': {'deny': rules}}
        self.settings.write_text(json.dumps(user), encoding='utf-8')
        self._settings()
        self.assertEqual(self.inst.load_json(str(self.settings))['permissions']['deny'][:2], rules)
        self._settings(True)
        self.assertEqual(self.inst.load_json(str(self.settings)), user)

    def _entry(self, **fields):
        return {'type': 'stdio', 'command': self.inst.python_exe(),
                'args': self.inst.mcp_args(), 'env': {}, **fields}

    def _claude_conflict(self, servers, wording=''):
        for uninstall in (False, True):
            with self.subTest(uninstall=uninstall):
                before = json.dumps({'mcpServers': servers})
                merged, reason = self.inst.merge_claude_mcp(json.loads(before), uninstall)
                self.assertEqual(json.dumps(merged), before, reason)
                self.assertTrue(reason.startswith('SKIP'), reason)
                self.assertIn('mcpServers.pbz' if isinstance(servers, dict) else 'mcpServers', reason)
                self.assertIn(wording, reason)

    def test_r10_3_foreign_claude_installation(self):
        self._claude_conflict({'pbz': self._entry(args=['-I', '/other/pbz/cli.py', 'mcp'])}, 'not ours')

    def test_r10_3_extra_claude_arguments(self):
        self._claude_conflict({'pbz': self._entry(args=[*self.inst.mcp_args(), '--extra'])}, 'not ours')

    def test_r10_3_custom_claude_env(self):
        self._claude_conflict({'pbz': self._entry(env={'HOST_OPTION': 'fixture'})})

    def test_r10_3_non_dict_claude_servers(self):
        self._claude_conflict(['host'])

    def test_r10_3_non_dict_claude_entry(self):
        self._claude_conflict({'pbz': 'host'})

    def test_r10_3_string_claude_env(self):
        self._claude_conflict({'pbz': self._entry(env='host')})

    def test_r10_3_extra_claude_key(self):
        self._claude_conflict({'pbz': self._entry(host_owned=True)})

    def test_r10_3_case_changed_path(self):
        args = self.inst.mcp_args()
        args[1] = args[1].replace('/pbz/', '/PBZ/')
        self._claude_conflict({'pbz': self._entry(args=args)}, 'not ours')

    def _block(self, header='mcp_servers.pbz', command=None, args=None):
        command = self.inst.python_exe() if command is None else command
        args = self.inst.mcp_args() if args is None else args
        return f'[{header}]\ncommand = {json.dumps(command)}\nargs = {json.dumps(args)}\n'

    def _codex_conflict(self, original, wording=''):
        for uninstall in (False, True):
            with self.subTest(uninstall=uninstall):
                text, reason = self.inst.merge_codex_toml(original, uninstall)
                self.assertEqual(text, original, reason)
                self.assertTrue(reason.startswith('SKIP'), reason)
                self.assertIn('mcp_servers.pbz', reason)
                self.assertIn(wording, reason)

    def test_r10_3_extra_codex_arguments(self):
        self._codex_conflict(self._block(args=[*self.inst.mcp_args(), '--extra']), 'not ours')

    def test_r10_3_codex_env(self):
        self._codex_conflict(self._block() + '[mcp_servers.pbz.env]\nHOST = "fixture"\n')

    def test_r10_3_extra_codex_key(self):
        self._codex_conflict(self._block() + 'host_owned = true\n')

    def test_r10_3_foreign_codex_installation(self):
        self._codex_conflict(self._block(args=['-I', '/other/pbz/cli.py', 'mcp']), 'not ours')

    def _removed(self, original, unrelated=''):
        import tomllib

        text, reason = self.inst.merge_codex_toml(original, True)
        self.assertEqual(reason, 'removed')
        self.assertEqual(text, unrelated, 'removal must preserve unrelated TOML bytes')
        self.assertEqual(tomllib.loads(text), tomllib.loads(unrelated))

    def test_r10_4_quoted_parent_uninstall(self):
        for header in ('"mcp_servers".pbz', "'mcp_servers'.pbz"):
            with self.subTest(header=header):
                self._removed(self._block(header))

    def test_r10_4_quoted_parent_updates_in_place(self):
        import tomllib

        for header in ('"mcp_servers".pbz', "'mcp_servers'.pbz"):
            with self.subTest(header=header):
                unrelated = '[host]\nvalue = 1\n'
                text, reason = self.inst.merge_codex_toml(self._block(header, 'stale-python') + unrelated, False)
                self.assertEqual(reason, 'updated')
                self.assertTrue(text.endswith(unrelated))
                self.assertEqual(tomllib.loads(text), {
                    'host': {'value': 1},
                    'mcp_servers': {'pbz': {'command': self.inst.python_exe(), 'args': self.inst.mcp_args()}},
                })

    def test_r10_4_multiline_string(self):
        unrelated = '[host]\ntext = """\n[mcp_servers.pbz]\nkeep this text\n"""\n'
        self._removed(unrelated + self._block(), unrelated)

    def test_r10_4_quoted_child_uninstall(self):
        self._removed(self._block('mcp_servers."pbz"'))

    def test_r10_4_noncontiguous_subtable(self):
        self._codex_conflict(self._block() + '[host]\nx = 1\n[mcp_servers.pbz.env]\nX = "host"\n')

    def test_r10_4_inline_registration(self):
        fields = f'command = "python", args = {json.dumps(self.inst.mcp_args())}'
        self._codex_conflict('[mcp_servers]\npbz = {' + fields + '}\n', 'cannot be located')

    def test_r10_4_dotted_registration(self):
        import tomllib

        args = self.inst.mcp_args()
        text = f'[mcp_servers]\npbz.command = "python"\npbz.args = {json.dumps(args)}\n'
        self.assertEqual(tomllib.loads(text)['mcp_servers']['pbz'], {'command': 'python', 'args': args})
        self._codex_conflict(text, 'cannot be located')

    def test_r10_4_multiline_array(self):
        unrelated = '[host]\nvalues = [\n  [1, 2],\n  [3],\n]\n'
        self._removed(unrelated + self._block(), unrelated)

    def test_r10_6_malformed_claude_preflight(self):
        self.claude.write_bytes(b'{')
        self._refuse(('--mcp',), self.claude)

    def test_r10_6_bad_second_ownership_record(self):
        record = Path(str(self.hooks) + '.pbz-owned.json')
        record.write_bytes(b'{')
        self._refuse((), record)

    def test_r10_6_combined_preflight_problems(self):
        rule = self.root / 'rules.md'
        rule.write_text(self.inst.BEGIN, encoding='utf-8')
        self.codex.write_bytes(b'invalid = [\n')
        with patch.object(self.inst, 'RULE_FILES', [str(rule)]):
            output = self._refuse(('--mcp',), self.codex, rule)
        self.assertIn('markers', output)

    def test_r10_6_second_target_divergence(self):
        self._journal((b'{}', b'{"x":1}'), (None, b'{}'), (b'{"host":1}', None), self.hooks)
        self._refuse((), str(self.hooks) + '.pbz-txn.json')

    def test_r10_6_uninstall_bad_toml(self):
        self.assertEqual(self._run('--mcp')[0], 0)
        self.codex.write_bytes(b'invalid = [\n')
        self._refuse(('--uninstall',), self.codex)

    def test_r10_6_settings_merge_conflict(self):
        self.hooks.write_bytes(b'{"hooks":{"PreToolUse":{"host_owned":true}}}')
        self._refuse((), self.hooks)

    def test_r10_8_spaced_paths(self):
        python = str(self.root / 'python directory' / 'python3')
        canon = str(self.root / 'skill directory')
        guard = str(Path(canon, 'pbz', 'guard.py'))
        if os.name == 'nt':
            python, guard = python.replace('\\', '/'), guard.replace('\\', '/')
        with patch.object(self.inst, 'python_exe', return_value=python), patch.object(self.inst, 'CANON', canon):
            try:
                command = self.inst.hook_cmd('claude')
            except SystemExit as error:
                self.fail(f'safe spaced paths must be quoted: {error}')
            self.assertEqual(command, f'"{python}" -I -S "{guard}" --agent claude')
            self.assertTrue(self.inst.is_ours({'hooks': [{'command': command}]}))

    def test_r10_8_legacy_unicode_uninstall(self):
        canon = str(self.root / 'skill-é')
        python = self.inst.python_exe()
        guard = str(Path(canon, 'pbz', 'guard.py')).replace(os.sep, '/')
        child = {'type': 'command', 'command': f'{python} -I -S {guard} --agent claude', 'timeout': 15}
        settings = {'hooks': {'PreToolUse': [{'matcher': '.*', 'hooks': [child]}]}}
        record = {'hooks': {'container': True, 'events': ['PreToolUse'],
                            'additions': [{'event': 'PreToolUse', 'child': child,
                                           'shell': {'matcher': '.*', 'hooks': []}}]}}
        self.settings.write_text(json.dumps(settings), encoding='utf-8')
        self.record.write_text(json.dumps(record), encoding='utf-8')
        with patch.object(self.inst, 'CANON', canon), patch.object(self.inst, 'python_exe', return_value=python):
            code, output = self._run('--uninstall')
        self.assertEqual(code, 0, output)
        self.assertEqual(self.inst.load_json(str(self.settings)), {})
        self.assertFalse(self.record.exists())

    def test_r10_8_refused_characters(self):
        for char in ('$', '`', '%', ';', '"', '\n', ',', '&', '|', '<', '>', '^', "'", '=', '#', '[', ']', 'é'):
            with self.subTest(char=char), patch.object(self.inst, 'python_exe', return_value=f'/p{char}y/python3'):
                with self.assertRaisesRegex(SystemExit, 'hook command would need quoting'):
                    self.inst.hook_cmd('claude')

    def test_r10_8_refused_backslashes(self):
        for path in (r'\\server\share\python3', 'python3\\'):
            with self.subTest(path=path), patch.object(self.inst, 'python_exe', return_value=path):
                with self.assertRaisesRegex(SystemExit, 'hook command would need quoting'):
                    self.inst.hook_cmd('claude')

    def test_r10_8_parentheses_quoted(self):
        path = '/opt/(local)/python3'
        with patch.object(self.inst, 'python_exe', return_value=path):
            self.assertTrue(self.inst.hook_cmd('claude').startswith(f'"{path}" -I -S '))

    def test_r10_9_owned_junction_what_if(self):
        target = self.root / 'skill'
        target.mkdir()
        link = self.root / 'linked-skill'
        if os.name == 'posix':
            link.symlink_to(target, target_is_directory=True)
        else:
            # An existing directory exercises the same realpath ownership check on Windows.
            link = target
        before = self._snapshot()
        with patch.object(self.inst, 'SKILL', str(target)), patch.object(self.inst, 'LINKS', [str(link)]):
            code, output = self._run('--what-if', '--uninstall')
        self.assertEqual(code, 0, output)
        self.assertEqual(self._snapshot(), before)
        self.assertIn(f'would remove {link}', output)

    def _mklink_failure(self, error, *details):
        link = self.root / 'profile' / 'skills' / 'pbz'
        link.parent.mkdir(parents=True)
        with patch.object(self.inst, 'LINKS', [str(link)]), \
             patch.object(self.inst.subprocess, 'run', side_effect=error) as run:
            code, output = self._run()
        run.assert_called_once()
        self.assertEqual(code, 1, output)
        self.assertIn('FAILED', output)
        for detail in (*details, str(link)):
            self.assertIn(detail, output)
        return output

    def test_r10_9_mklink_nonzero_exit(self):
        error = subprocess.CalledProcessError(23, ['cmd'], output=b'other output', stderr=b'junction denied')
        output = self._mklink_failure(error, os.path.realpath(SKILL), '23', 'junction denied')
        self.assertNotIn('other output', output)

    def test_r10_9_missing_cmd_executable(self):
        self._mklink_failure(FileNotFoundError(2, 'No such file or directory', 'cmd'), 'cmd')

    def test_r10_9_ownership_record_only(self):
        self._settings()
        before = self.settings.read_bytes()
        self.record.unlink()
        code, output = self._run()
        self.assertEqual(code, 0, output)
        self.assertEqual(self.settings.read_bytes(), before)
        self.assertTrue(self.record.exists())
        self.assertIn(f'ownership record -> {self.settings}', output)

    def _report(self, args, exit_code, result):
        code, output = self._run(*args, '--report-json')
        self.assertEqual(code, exit_code, output)
        try:
            report = json.loads(output)
        except ValueError as error:
            self.fail(f'stdout must contain one JSON object: {error}; output={output!r}')
        self.assertIsInstance(report, dict)
        self.assertEqual(set(report), {'operations', 'result', 'errors'})
        self.assertEqual(report['result'], result)
        self.assertIsInstance(report['errors'], list)
        self.assertIsInstance(report['operations'], list)
        for operation in report['operations']:
            self.assertEqual(set(operation), {'step', 'state', 'detail'})
            self.assertIn(operation['state'], {'planned', 'changed', 'unchanged', 'removed',
                                                'absent', 'conflict', 'failed'})
        return report

    def _operation(self, report, state, *fragments):
        for operation in report['operations']:
            if operation['state'] == state and all(fragment in str(operation) for fragment in fragments):
                return operation
        self.fail(f'expected {state} operation containing {fragments!r}: {report!r}')

    def test_r10_9_what_if_settings_report(self):
        before = self._snapshot()
        code, output = self._run('--what-if')
        self.assertEqual(code, 0, output)
        self.assertIn(f'hooks + deny rules -> {self.settings}', output)
        report = self._report(('--what-if',), 0, 'ok')
        self._operation(report, 'planned', self.settings.name)
        self.assertEqual(self._snapshot(), before)

    def test_r10_9_foreign_registration_uninstall(self):
        text = json.dumps({'mcpServers': {'pbz': self._entry(args=['-I', '/other/pbz/cli.py', 'mcp'])}})
        self.claude.write_bytes(text.encode())
        report = self._report(('--uninstall',), 1, 'conflict')
        self.assertEqual(self.claude.read_bytes(), text.encode())
        self._operation(report, 'conflict', 'mcpServers.pbz')

    def test_r10_9_json_success(self):
        report = self._report(('--mcp',), 0, 'ok')
        settings = self._operation(report, 'changed', self.settings.name)
        registration = self._operation(report, 'changed', 'mcpServers.pbz')
        self.assertNotEqual(settings['step'], registration['step'])
        self.assertEqual(report['errors'], [])

    def test_r10_9_json_fatal_preflight(self):
        self.claude.write_bytes(b'{')
        before = self._snapshot()
        report = self._report(('--mcp',), 1, 'conflict')
        self._operation(report, 'conflict', self.claude.name)
        self.assertIn(self.claude.name, str(report['errors']))
        self.assertEqual(self._snapshot(), before)

    def test_r10_9_json_unknown_argument(self):
        before = self._snapshot()
        report = self._report(('--bogus',), 1, 'conflict')
        self.assertEqual(report['operations'], [])
        self.assertIn('--bogus', str(report['errors']))
        self.assertEqual(self._snapshot(), before)
        code, output = self._run('--report-json', '--bogus')
        self.assertEqual(code, 1, output)
        self.assertEqual(json.loads(output), report)
        self.assertEqual(self._snapshot(), before)

    def test_r10_9_json_apply_conflict_retains_completed_operation(self):
        step = self.inst.settings_step

        def conflict(path, *args, **kwargs):
            if path == str(self.hooks):
                raise self.inst.InstallConflict(f'{path}: concurrent edit')
            return step(path, *args, **kwargs)

        with patch.object(self.inst, 'settings_step', side_effect=conflict):
            report = self._report((), 1, 'conflict')
        self._operation(report, 'changed', self.settings.name)
        self.assertIn('concurrent edit', str(report['errors']))

    def test_r10_9_json_failed_operation_takes_precedence(self):
        first = self.root / 'first-link'
        second = self.root / 'second-link'
        foreign = {'mcpServers': {'pbz': self._entry(args=['-I', '/other/pbz/cli.py', 'mcp'])}}
        self.claude.write_text(json.dumps(foreign), encoding='utf-8')

        def mklink(command, **kwargs):
            if command[-2] == str(second):
                raise subprocess.CalledProcessError(23, command, stderr=b'junction denied')
            return subprocess.CompletedProcess(command, 0)

        with patch.object(self.inst, 'LINKS', [str(first), str(second)]), \
             patch.object(self.inst.subprocess, 'run', side_effect=mklink):
            report = self._report(('--mcp',), 1, 'failed')
        self._operation(report, 'changed', first.name)
        self._operation(report, 'failed', second.name, '23', 'junction denied')

    def test_r10_9_json_unexpected_exception(self):
        with patch.object(self.inst, 'settings_step', side_effect=OSError('injected apply error')):
            report = self._report((), 1, 'failed')
        self.assertIn('OSError', str(report['errors']))
        self.assertIn('injected apply error', str(report['errors']))

    def test_r10_9_json_path_refusal(self):
        path = '/unsafe%path with spaces/python3'
        before = self._snapshot()
        with patch.object(self.inst, 'python_exe', return_value=path):
            report = self._report((), 1, 'conflict')
        self.assertIn(path, str(report['errors']))
        self.assertEqual(self._snapshot(), before)

    if os.name == 'posix':
        def _launcher(self, directory='tree', with_cli=True):
            tree = self.root / directory
            launcher = tree / 'bin' / 'pbz'
            launcher.parent.mkdir(parents=True)
            shutil.copyfile(Path(SKILL, 'bin', 'pbz'), launcher)
            launcher.chmod(0o755)
            if with_cli:
                cli = tree / 'pbz' / 'cli.py'
                cli.parent.mkdir()
                cli.write_text('import json, sys\nprint(json.dumps(sys.argv[1:]))\nraise SystemExit(3)\n',
                               encoding='utf-8')
            return launcher

        def _bash(self):
            bash = shutil.which('bash')
            self.assertIsNotNone(bash, 'POSIX launcher tests require bash')
            return bash

        def _launch(self, args, path=None):
            env = {**os.environ}
            if path is not None:
                env['PATH'] = str(path)
            return subprocess.run(args, cwd=self.root, env=env, text=True, capture_output=True, timeout=10)

        def _launcher_error(self, result, code):
            self.assertEqual(result.returncode, code, result.stderr)
            self.assertTrue(result.stderr.startswith('pbz launcher:'), result.stderr)
            self.assertEqual(result.stdout, '')

        def test_r10_7_missing_cli(self):
            launcher = self._launcher(with_cli=False)
            result = self._launch([self._bash(), str(launcher)])
            self._launcher_error(result, 1)
            self.assertIn('pbz/cli.py', result.stderr)
            self.assertIn(str(launcher.parent), result.stderr)

        def test_r10_7_missing_python(self):
            launcher = self._launcher()
            empty = self.root / 'empty'
            empty.mkdir()
            result = self._launch([self._bash(), str(launcher)], empty)
            self._launcher_error(result, 127)
            self.assertIn('python', result.stderr.lower())

        def test_r10_7_portable_readlink(self):
            launcher = self._launcher()
            link = self.root / 'linked-pbz'
            link.symlink_to(launcher.relative_to(self.root))
            tools = self.root / 'tools'
            tools.mkdir()
            readlink = shutil.which('readlink')
            dirname = shutil.which('dirname')
            self.assertIsNotNone(readlink)
            self.assertIsNotNone(dirname)
            (tools / 'python3').symlink_to(sys.executable)
            (tools / 'dirname').symlink_to(dirname)
            stub = tools / 'readlink'
            import shlex

            stub.write_text(f'#!{self._bash()}\n'
                            f'[[ $# == 1 ]] || exit 64\nexec {shlex.quote(readlink)} "$1"\n',
                            encoding='utf-8')
            stub.chmod(0o755)
            self._forwarded(self._launch([self._bash(), str(link), 'space argument'], tools), ['space argument'])

        def _forwarded(self, result, args):
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertEqual(json.loads(result.stdout), args)

        def test_r10_7_invocation_forms(self):
            launcher = self._launcher()
            spaced = self._launcher('tree with spaces')
            link = self.root / 'linked-pbz'
            link.symlink_to(launcher.relative_to(self.root))
            arguments = ['mcp', 'space argument', '', '$literal', 'a"b']
            for command in ([str(launcher)], [str(link)], ['./tree/bin/pbz'],
                            [self._bash(), '-c', 'exec pbz "$@"', 'bash'], [str(spaced)]):
                with self.subTest(command=command):
                    self._forwarded(self._launch(command + arguments,
                                                 f'{launcher.parent}{os.pathsep}{os.environ["PATH"]}'), arguments)

        def _source_launcher(self, launcher, zero, path=None):
            return self._launch([self._bash(), '-c', '. "$1" "$2"', str(zero), str(launcher), 'forwarded'], path)

        def test_r10_7_resolver_cycle(self):
            launcher = self._launcher()
            first, second = self.root / 'cycle-a', self.root / 'cycle-b'
            first.symlink_to(second.name)
            second.symlink_to(first.name)
            self._launcher_error(self._source_launcher(launcher, first), 1)

        def test_r10_7_resolver_dangling(self):
            launcher = self._launcher()
            dangling = self.root / 'dangling'
            dangling.symlink_to('missing')
            self._launcher_error(self._source_launcher(launcher, dangling), 1)

        def test_r10_7_resolver_bare_name(self):
            launcher = self._launcher()
            path = f'{launcher.parent}{os.pathsep}{os.environ["PATH"]}'
            self._forwarded(self._source_launcher(launcher, 'pbz', path), ['forwarded'])
            self._launcher_error(self._source_launcher(launcher, 'missing-pbz', path), 1)

        def test_r10_8_posix_hook_round_trip(self):
            for name in ('skill with spaces', r'skill\interior'):
                with self.subTest(name=name):
                    canon = self.root / name
                    guard = canon / 'pbz' / 'guard.py'
                    guard.parent.mkdir(parents=True)
                    guard.write_text('import json, sys\nprint(json.dumps(sys.argv))\n', encoding='utf-8')
                    with patch.object(self.inst, 'CANON', str(canon)):
                        try:
                            command = self.inst.hook_cmd('claude')
                        except SystemExit as error:
                            self.fail(f'representable guard path refused: {error}')
                    result = self._launch([self._bash(), '-c', command])
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(json.loads(result.stdout), [str(guard), '--agent', 'claude'])


class CatalogBuilderTests(unittest.TestCase):
    def setUp(self):
        from contextlib import ExitStack

        scope = self.enterContext(ExitStack())
        root = Path(scope.enter_context(tempfile.TemporaryDirectory()))
        self.catalog = load_catalog()
        self.package = root / 'package.json'
        self.data = root / 'data'
        self.data.mkdir()
        self.package.write_bytes(b'{"version":"1.0.0"}')
        (self.data / 'capabilities.json').write_bytes(b'{"fixture":{}}')
        (self.data / 'vco-capabilities.json').write_bytes(b'{}')
        (self.data / 'instance-selectors.json').write_bytes(b'{}')
        self.outputs = (root / 'visuals.json', root / 'visual-objects.json')
        for name, value in zip(('PKG', 'OUT', 'OUT_OBJECTS'), (root, *self.outputs)):
            scope.enter_context(patch.object(self.catalog, name, str(value)))

    def _run(self, *args):
        from contextlib import redirect_stdout
        from io import StringIO

        output = StringIO()
        with patch.object(sys, 'argv', ['build_visual_catalog.py', *args]), redirect_stdout(output):
            try:
                code = self.catalog.main()
            except (Exception, SystemExit) as error:
                code = f'raised {type(error).__name__}: {error}'
        try:
            report = json.loads(output.getvalue())
        except ValueError:
            report = output.getvalue()
        return code, report

    def _seed(self):
        code, report = self._run()
        self.assertEqual(code, 0, report)

    def _bytes(self):
        return tuple(path.read_bytes() if path.exists() else None for path in self.outputs)

    def _input_error(self, filename, *args):
        self._seed()
        before = self._bytes()
        self.package.write_bytes(b'{"version":"2.0.0"}')
        (self.data / filename).write_bytes(b'{')
        code, report = self._run(*args)
        self.assertEqual(self._bytes(), before, 'invalid input must leave both outputs byte-identical')
        self.assertEqual(code, 1, report)
        self.assertEqual(set(report), {'error'})
        self.assertIn(filename, report['error'])

    def test_r10_5_malformed_instance_selectors(self):
        self._input_error('instance-selectors.json')

    def test_r10_5_malformed_vco(self):
        self._input_error('vco-capabilities.json')

    def test_r10_11_check_malformed_input(self):
        self._input_error('instance-selectors.json', '--check')

    def _assert_paths(self, report, key, *paths):
        self.assertIsInstance(report, dict)
        self.assertIn(key, report)
        self.assertEqual([Path(name).name for name in report[key]], [path.name for path in paths])

    def test_r10_5_publish_failure_then_check_mixed_generation(self):
        self._seed()
        first, second = self.outputs
        before = self._bytes()
        listing = set(second.parent.iterdir())
        self.package.write_bytes(b'{"version":"2.0.0"}')
        replace = os.replace

        def fail_second(source, destination, *args, **kwargs):
            if Path(destination) == second:
                raise OSError('injected publish failure')
            return replace(source, destination, *args, **kwargs)

        with patch.object(os, 'replace', side_effect=fail_second):
            code, report = self._run()
        self.assertEqual(code, 1, report)
        self.assertEqual(set(report), {'error', 'output'})
        self.assertIn('publish', report['error'].lower())
        self.assertEqual(Path(report['output']).name, second.name)
        self.assertEqual(set(second.parent.iterdir()), listing, 'failed publication must clean its temp file')
        mixed = self._bytes()
        self.assertNotEqual(mixed[0], before[0])
        self.assertEqual(mixed[1], before[1])
        code, report = self._run('--check')
        self.assertEqual(code, 1, report)
        self._assert_paths(report, 'stale', second)
        self.assertEqual(self._bytes(), mixed)

    def _old_mtimes(self):
        for path in self.outputs:
            os.utime(path, ns=(1_000_000_000, 1_000_000_000))
        return self._mtimes()

    def _mtimes(self):
        return tuple(path.stat().st_mtime_ns if path.exists() else None for path in self.outputs)

    def test_r10_10_identical_run_does_not_rewrite(self):
        self._seed()
        before = self._old_mtimes()
        code, report = self._run()
        self.assertEqual(code, 0, report)
        self.assertEqual(self._mtimes(), before, 'identical outputs must retain their mtimes')
        self._assert_paths(report, 'written')
        self._assert_paths(report, 'unchanged', *self.outputs)

    def _check(self, exit_code, stale):
        before, mtimes = self._bytes(), self._mtimes()
        code, report = self._run('--check')
        self.assertEqual(self._bytes(), before, '--check must leave both artifact bytes unchanged')
        self.assertEqual(self._mtimes(), mtimes, '--check must never rewrite an artifact')
        self.assertEqual(code, exit_code, report)
        self.assertNotIn('written', report)
        self.assertNotIn('error', report)
        self.assertIn('checked', report)
        self._assert_paths(report, 'stale', *stale)

    def test_r10_11_check_writes_nothing(self):
        self._seed()
        self._old_mtimes()
        self._check(0, ())

    def test_r10_11_check_fresh_report(self):
        self._seed()
        code, report = self._run('--check')
        self.assertEqual(code, 0, report)
        self._assert_paths(report, 'checked', *self.outputs)
        self.assertNotIn('written', report)

    def test_r10_11_check_stale(self):
        self._seed()
        self.outputs[1].write_bytes(b'{}')
        self._check(1, (self.outputs[1],))

    def test_r10_11_check_missing(self):
        self._seed()
        self.outputs[0].unlink()
        self._check(1, (self.outputs[0],))

    def test_r10_11_unknown_argument(self):
        self._seed()
        before = self._bytes()
        self.package.write_bytes(b'{"version":"2.0.0"}')
        code, report = self._run('--bogus')
        self.assertEqual(self._bytes(), before, 'unknown arguments must leave both artifacts unchanged')
        self.assertEqual(code, 1, report)
        self.assertIn('--bogus', report['error'])


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
