"""Pending recovery blocks every pull, and a bad journal is not echoed.
Run: python tests/test_mirror_recovery_honesty.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from _junction import make_junction  # noqa: E402
from pbz import common, mirror  # noqa: E402

SECRET = "SECRET_JOURNAL_PAYLOAD_9f3a"


def file_map(root):
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            with open(path, "rb") as f:
                out[rel] = f.read()
    return out


class RecoveryHonestyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "ws")
        test_parity.build(root)
        self.ws = common.Workspace(root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)

    def _journal(self, body):
        directory = os.path.join(mirror._transaction_dir(self.ws, self.project), "tx1")
        os.makedirs(directory)
        with open(os.path.join(directory, "journal.json"), "w", encoding="utf-8") as f:
            json.dump(body, f)

    def _restore_with_editor_after_mutation(self, before):
        rel = self.project.name + ".Report/definition/restore.json"
        target = mirror._real_path(self.ws, rel)
        after = b"push bytes"
        editor = b"editor bytes"
        if before is not None:
            common.replace_bytes(target, before, root=self.ws.root)
        directory, journal = mirror._begin_transaction(
            self.ws, self.project, "restore-race", [("workspace", rel, common.sha(after))])
        common.replace_bytes(target, after, root=self.ws.root)
        owner = mirror.os if before is None else mirror
        operation = "remove" if before is None else "_install_before_image"
        real = getattr(owner, operation)

        def mutate(path, *args, **kwargs):
            result = real(path, *args, **kwargs)
            if (path.startswith(target + ".pbz-restore-") if before is None else path == target):
                with open(target, "wb") as handle:
                    handle.write(editor)
            return result

        with patch.object(owner, operation, mutate):
            out = mirror._restore_transaction(self.ws, self.project, directory, journal)
        self.assertEqual(out, {"result": "RECOVERY_CONFLICT", "conflicts": [rel]})
        with open(os.path.join(directory, "journal.json")) as handle:
            self.assertEqual(json.load(handle)["state"], "RECOVERY_REQUIRED")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), editor)

    def test_editor_after_restore_replace_requires_recovery(self):
        self._restore_with_editor_after_mutation(b"before bytes")

    def test_editor_after_restore_remove_requires_recovery(self):
        self._restore_with_editor_after_mutation(None)

    def _restore_with_editor_at_rename(self, before, *, after_rename):
        rel = self.project.name + ".Report/definition/restore.json"
        target = mirror._real_path(self.ws, rel)
        after = b"push bytes"
        editor = b"editor bytes"
        if before is not None:
            common.replace_bytes(target, before, root=self.ws.root)
        directory, journal = mirror._begin_transaction(
            self.ws, self.project, "restore-race", [("workspace", rel, common.sha(after))])
        common.replace_bytes(target, after, root=self.ws.root)
        real = os.rename
        quarantines = []

        def rename(source, destination, *args, **kwargs):
            if source != target or not destination.startswith(target + ".pbz-restore-"):
                return real(source, destination, *args, **kwargs)
            quarantines.append(destination)
            if after_rename:
                result = real(source, destination, *args, **kwargs)
            with open(target, "wb") as handle:
                handle.write(editor)
            if after_rename:
                return result
            return real(source, destination, *args, **kwargs)

        with patch.object(os, "rename", rename):
            out = mirror._restore_transaction(self.ws, self.project, directory, journal)
        self.assertEqual(out, {"result": "RECOVERY_CONFLICT", "conflicts": [rel]})
        with open(os.path.join(directory, "journal.json")) as handle:
            self.assertEqual(json.load(handle)["state"], "RECOVERY_REQUIRED")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), editor)
        self.assertEqual(len(quarantines), 1)
        remaining = [os.path.join(os.path.dirname(target), name)
                     for name in os.listdir(os.path.dirname(target))
                     if name.startswith(os.path.basename(target) + ".pbz-restore-")]
        if not after_rename:
            self.assertEqual(remaining, quarantines)
            with open(quarantines[0], "rb") as handle:
                self.assertEqual(handle.read(), editor)
        elif before is not None:
            self.assertEqual(remaining, quarantines)
            with open(quarantines[0], "rb") as handle:
                self.assertEqual(handle.read(), after)
        else:
            self.assertEqual(remaining, [])

    def test_editor_before_restore_rename_replace_requires_recovery(self):
        self._restore_with_editor_at_rename(b"before bytes", after_rename=False)

    def test_editor_before_restore_rename_remove_requires_recovery(self):
        self._restore_with_editor_at_rename(None, after_rename=False)

    def test_editor_after_restore_rename_replace_requires_recovery(self):
        self._restore_with_editor_at_rename(b"before bytes", after_rename=True)

    def test_editor_after_restore_rename_remove_requires_recovery(self):
        self._restore_with_editor_at_rename(None, after_rename=True)

    def test_second_editor_save_during_quarantine_return_is_preserved(self):
        rel = self.project.name + ".Report/definition/restore.json"
        target = mirror._real_path(self.ws, rel)
        after = b"push bytes"
        first_editor = b"first editor version\n"
        second_editor = b"second editor version\n"
        common.replace_bytes(target, b"before bytes", root=self.ws.root)
        directory, journal = mirror._begin_transaction(
            self.ws, self.project, "restore-return-race",
            [("workspace", rel, common.sha(after))])
        common.replace_bytes(target, after, root=self.ws.root)
        real_rename = os.rename
        real_install = mirror._install_before_image

        def rename(source, destination, *args, **kwargs):
            if source == target and destination.startswith(target + ".pbz-restore-"):
                with open(target, "wb") as handle:
                    handle.write(first_editor)
            return real_rename(source, destination, *args, **kwargs)

        def install_with_second_save(path, content):
            with open(target, "wb") as handle:
                handle.write(second_editor)
            return real_install(path, content)

        with patch.object(os, "rename", rename), patch.object(
                mirror, "_install_before_image", side_effect=install_with_second_save) as attempted:
            out = mirror._restore_transaction(self.ws, self.project, directory, journal)
        self.assertEqual(out, {"result": "RECOVERY_CONFLICT", "conflicts": [rel]})
        with open(os.path.join(directory, "journal.json")) as handle:
            self.assertEqual(json.load(handle)["state"], "RECOVERY_REQUIRED")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), second_editor)
        attempted.assert_called_once_with(target, first_editor)
        remaining = [os.path.join(os.path.dirname(target), name)
                     for name in os.listdir(os.path.dirname(target))
                     if name.startswith(os.path.basename(target) + ".pbz-restore-")]
        self.assertEqual(len(remaining), 1)
        with open(remaining[0], "rb") as handle:
            self.assertEqual(handle.read(), first_editor)

    @unittest.skipUnless(os.name == "posix", "open editor handle follows renamed inode")
    def test_open_editor_save_during_successful_quarantine_return_is_preserved(self):
        rel = self.project.name + ".Report/definition/restore.json"
        target = mirror._real_path(self.ws, rel)
        after = b"push bytes"
        first_editor = b"first editor version\n"
        newest_editor = b"newest editor version through held handle\n"
        common.replace_bytes(target, b"before bytes", root=self.ws.root)
        directory, journal = mirror._begin_transaction(
            self.ws, self.project, "restore-open-handle-race",
            [("workspace", rel, common.sha(after))])
        common.replace_bytes(target, after, root=self.ws.root)
        real_rename = os.rename
        real_install = mirror._install_before_image

        def rename(source, destination, *args, **kwargs):
            if source == target and destination.startswith(target + ".pbz-restore-"):
                with open(target, "wb") as handle:
                    handle.write(first_editor)
            return real_rename(source, destination, *args, **kwargs)

        with open(target, "r+b") as editor_handle:
            def install_with_open_editor_save(path, content):
                editor_handle.seek(0)
                editor_handle.write(newest_editor)
                editor_handle.truncate()
                editor_handle.flush()
                editor_handle.close()
                return real_install(path, content)

            with patch.object(os, "rename", rename), patch.object(
                    mirror, "_install_before_image",
                    side_effect=install_with_open_editor_save) as attempted:
                out = mirror._restore_transaction(self.ws, self.project, directory, journal)
        self.assertEqual(out, {"result": "RECOVERY_CONFLICT", "conflicts": [rel]})
        with open(os.path.join(directory, "journal.json")) as handle:
            self.assertEqual(json.load(handle)["state"], "RECOVERY_REQUIRED")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), first_editor)
        attempted.assert_called_once_with(target, first_editor)
        remaining = [os.path.join(os.path.dirname(target), name)
                     for name in os.listdir(os.path.dirname(target))
                     if name.startswith(os.path.basename(target) + ".pbz-restore-")]
        self.assertEqual(len(remaining), 1)
        with open(remaining[0], "rb") as handle:
            self.assertEqual(handle.read(), newest_editor)

    def test_restore_transaction_deleted_target_uses_no_clobber_install(self):
        rel = self.project.name + ".Report/definition/restore.json"
        target = mirror._real_path(self.ws, rel)
        before = b"before bytes"
        editor = b"editor bytes"
        install = mirror._install_before_image
        for racing in (False, True):
            with self.subTest(racing=racing):
                common.replace_bytes(target, before, root=self.ws.root)
                directory, journal = mirror._begin_transaction(
                    self.ws, self.project, "restore-deleted-" + str(racing),
                    [("workspace", rel, None)])
                os.remove(target)

                def install_with_editor(path, content):
                    if racing:
                        with open(path, "wb") as handle:
                            handle.write(editor)
                    return install(path, content)

                with patch.object(mirror, "_install_before_image",
                                  side_effect=install_with_editor) as attempted:
                    out = mirror._restore_transaction(self.ws, self.project, directory, journal)
                attempted.assert_called_once_with(target, before)
                self.assertEqual(out, {"result": "RECOVERY_CONFLICT", "conflicts": [rel]}
                                 if racing else {"result": "ROLLED_BACK"})
                with open(os.path.join(directory, "journal.json")) as handle:
                    self.assertEqual(json.load(handle)["state"],
                                     "RECOVERY_REQUIRED" if racing else "ROLLED_BACK")
                with open(target, "rb") as handle:
                    self.assertEqual(handle.read(), editor if racing else before)

    def test_restore_transaction_recreates_deleted_target_and_parent(self):
        rel = self.project.name + ".Report/definition/restore-parent/restore.json"
        target = mirror._real_path(self.ws, rel)
        before = b'{"before": "exact bytes"}\n'
        common.replace_bytes(target, before, root=self.ws.root)
        directory, journal = mirror._begin_transaction(
            self.ws, self.project, "restore-deleted-parent", [("workspace", rel, None)])
        os.remove(target)
        os.rmdir(os.path.dirname(target))
        self.assertFalse(os.path.lexists(target))
        self.assertFalse(os.path.lexists(os.path.dirname(target)))

        out = mirror._restore_transaction(self.ws, self.project, directory, journal)

        self.assertEqual(out, {"result": "ROLLED_BACK"})
        with open(os.path.join(directory, "journal.json")) as handle:
            self.assertEqual(json.load(handle)["state"], "ROLLED_BACK")
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), before)

    def test_pending_recovery_blocks_every_pull(self):
        self._journal({"version": 1, "state": "APPLYING", "entries": []})
        before = file_map(self.ws.mirror)
        for kwargs in ({}, {"force": True}, {"rebase": True}):
            out = mirror.pull(self.ws, self.project, **kwargs)
            self.assertEqual(out["result"], "RECOVERY_REQUIRED")
            self.assertEqual(file_map(self.ws.mirror), before)

    def test_malformed_journal_refuses_restore_without_payload(self):
        self._journal({"version": 1, "state": SECRET, "entries": [{"note": SECRET}]})
        before = file_map(self.ws.root)
        pending = mirror.pending_recovery(self.ws, self.project)
        self.assertEqual(pending, [{"id": "tx1", "state": "INVALID"}])
        self.assertNotIn(SECRET, json.dumps(pending))
        out = mirror.recover(self.ws, self.project, "tx1")
        blob = json.dumps(out)
        self.assertEqual(out["result"], "RECOVERY_REQUIRED")
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("note", blob)
        self.assertEqual(file_map(self.ws.root), before)

    def test_pending_recovery_refuses_transaction_junction(self):
        outside = os.path.join(self.tmp.name, "outside-journal")
        os.makedirs(outside)
        with open(os.path.join(outside, "journal.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "state": "APPLYING", "entries": []}, handle)
        link = os.path.join(mirror._transaction_dir(self.ws, self.project), "tx1")
        make_junction(self, link, outside)
        journal = os.path.join(outside, "journal.json")
        with open(journal, encoding="utf-8") as handle:
            before = handle.read()
        try:
            pending = mirror.pending_recovery(self.ws, self.project)
            self.assertEqual(pending, [{"id": "tx1", "state": "UNREADABLE"}])
            self.assertNotIn("APPLYING", json.dumps(pending))
            out = mirror.recover(self.ws, self.project, "tx1")
            self.assertEqual(out["result"], "RECOVERY_REQUIRED")
            with open(journal, encoding="utf-8") as handle:
                self.assertEqual(handle.read(), before)
        finally:
            os.rmdir(link)


class MirrorEditJournalTests(unittest.TestCase):
    def setUp(self):
        from test_mirror_writers_root import _tiny
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.root)
        _tiny(self.root)
        self.ws = common.Workspace(self.root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)
        self.path = os.path.join(self.project.mirror_model, "definition", "tables", "T.tmdl")
        self.before = common.read_checked(self.path, root=self.ws.mirror)

    def _run(self, fn):
        from pbz.mirror_txn import transaction
        return transaction(self.ws.mirror, fn, ws=self.ws, project=self.project)

    def _write(self, path, data):
        common.commit_bytes([("write_bytes", path, data)], root=self.ws.mirror)

    def test_failed_replace_restores_only_own_writes_after_other_project_push(self):
        import shutil
        from unittest.mock import patch
        from pbz import findreplace
        for suffix in ("SemanticModel", "Report"):
            shutil.copytree(os.path.join(self.root, "Canary." + suffix),
                            os.path.join(self.root, "Other." + suffix))
        open(os.path.join(self.root, "Other.pbip"), "w").close()
        other = common.Project(self.ws, "Other")
        with open(os.path.join(other.report_dir, "definition.pbir"), "w") as handle:
            json.dump({"version": "4.0", "datasetReference": {"byPath": {"path": "../Other.SemanticModel"}}}, handle)
        mirror.pull(self.ws, other)
        other_path = os.path.join(other.mirror_model, "definition", "tables", "T.tmdl")
        other_real = os.path.join(other.model_dir, "definition", "tables", "T.tmdl")
        commit = common.commit_bytes
        calls = []
        def fail_after_write(ops, **kwargs):
            commit(ops, **kwargs)
            self.assertNotEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
            # Other creates and modifies real files, pulls, then authors and pushes an actual change.
            extra = os.path.join(other.report_dir, "definition", "extra.json")
            with open(extra, "w") as handle:
                handle.write("{}")
            with open(other_real, "ab") as handle:
                handle.write(b"\n")
            self.assertEqual(mirror.pull(self.ws, other)["result"], "PULLED")
            real_before = common.read_text(other_real)[0]
            # The outsider has its own context, as a separate process would.
            from contextvars import Context
            Context().run(self._write, other_path, common.read_checked(other_path, root=self.ws.mirror) + b"\n")
            with patch("pbz.validate.engine_call", return_value={"ok": True}):
                pushed = Context().run(mirror.push, self.ws, other)
            self.assertEqual(pushed["result"], "PUSHED", pushed)
            self.assertNotEqual(common.read_text(other_real)[0], real_before)
            calls.append(file_map(other.mirror_model))
            raise RuntimeError("inert failure")
        with patch.object(findreplace, "commit_bytes", fail_after_write):
            with self.assertRaises(RuntimeError):
                findreplace.replace(self.ws, self.project, "= 1", "= 2", apply=True)
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        self.assertEqual(file_map(other.mirror_model), calls[0])
        self.assertFalse(any(mirror.mirror_changes(self.ws, other, mirror.load_manifest(self.ws, other)).values()))
        self.assertTrue(os.path.isfile(os.path.join(other.mirror_report, "definition", "extra.json")))

    def test_same_path_conflict_preserves_newer_bytes(self):
        newer = b"table T\n\tmeasure M = 3\n"
        def edit():
            self._write(self.path, b"table T\n\tmeasure M = 2\n")
            common.replace_bytes(self.path, newer, root=self.ws.mirror)
            raise RuntimeError("inert")
        with self.assertRaises(common.PbzError) as caught:
            self._run(edit)
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), newer)
        self.assertTrue(mirror.pending_recovery(self.ws, self.project))
        self.assertEqual(mirror.recover(self.ws, self.project)["result"], "RECOVERY_REQUIRED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), newer)

    def test_create_delete_and_rename_undo(self):
        from pbz import rename
        made = os.path.join(self.project.mirror_model, "definition", "created.tmdl")
        before = file_map(self.ws.mirror)
        def edit():
            self._write(made, b"table Created\n")
            common.commit_bytes([("delete", self.path)], root=self.ws.mirror)
            self.assertTrue(os.path.isfile(made))
            self.assertFalse(os.path.exists(self.path))
            raise RuntimeError("inert")
        with self.assertRaises(RuntimeError):
            self._run(edit)
        self.assertEqual(file_map(self.ws.mirror), before)
        def rename_then_fail():
            rename.rename(self.ws, self.project, "table", "T", "Renamed")
            self.assertFalse(os.path.exists(self.path))
            self.assertTrue(os.path.exists(os.path.join(os.path.dirname(self.path), "Renamed.tmdl")))
            raise RuntimeError("inert")
        with self.assertRaises(RuntimeError):
            self._run(rename_then_fail)
        self.assertEqual(file_map(self.ws.mirror), before)

    def test_same_project_lock_contention(self):
        from pbz import findreplace
        def edit():
            with self.assertRaises(common.PbzError) as caught:
                findreplace.replace(self.ws, self.project, "= 1", "= 2", apply=True)
            self.assertEqual(caught.exception.code, "COMMIT_BUSY")
            self.assertEqual(mirror.push(self.ws, self.project)["result"], "RECOVERY_REQUIRED")
            self.assertEqual(mirror.pull(self.ws, self.project)["result"], "RECOVERY_REQUIRED")
        self._run(edit)
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)

    def test_kill_mid_operation_recovers_durable_before_image(self):
        import subprocess
        code = """
import os, sys
from pbz import common
from pbz.mirror_txn import transaction
ws = common.Workspace(sys.argv[1]); ws.private = sys.argv[2]
project = ws.project()
def edit():
    common.commit_bytes([('write_bytes', sys.argv[3], b'table T\\n\\tmeasure M = 9\\n')], root=ws.mirror)
    os._exit(17)
transaction(ws.mirror, edit, ws=ws, project=project)
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = SKILL + os.pathsep + env.get("PYTHONPATH", "")
        child = subprocess.run([sys.executable, "-c", code, self.ws.root, self.ws.private, self.path], env=env,
                               capture_output=True, text=True)
        self.assertEqual(child.returncode, 17, child.stderr)
        self.assertNotEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        pending = mirror.pending_recovery(self.ws, self.project)
        self.assertEqual(len(pending), 1)
        directory = os.path.join(mirror._transaction_dir(self.ws, self.project), pending[0]["id"])
        with open(os.path.join(directory, "journal.json")) as handle:
            journal = json.load(handle)
        self.assertEqual(len(journal["entries"]), 1)
        self.assertEqual(journal["entries"][0]["before"], common.sha(self.before))
        self.assertEqual(mirror.pull(self.ws, self.project)["result"], "RECOVERY_REQUIRED")
        self.assertEqual(mirror.recover(self.ws, self.project)["result"], "RECOVERED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        self.assertFalse(os.path.exists(directory))

    def test_previous_written_version_is_an_external_conflict(self):
        v1 = b"table T\n\tmeasure M = 2\n"
        v2 = b"table T\n\tmeasure M = 3\n"
        def edit():
            self._write(self.path, v1)
            self._write(self.path, v2)
            self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), v2)
            common.replace_bytes(self.path, v1, root=self.ws.mirror)
            raise RuntimeError("inert")
        with self.assertRaises(common.PbzError) as caught:
            self._run(edit)
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), v1)
        self.assertTrue(mirror.pending_recovery(self.ws, self.project))
        self.assertEqual(mirror.recover(self.ws, self.project)["result"], "RECOVERY_REQUIRED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), v1)

    def test_failed_second_write_undo_uses_successful_version(self):
        from unittest.mock import patch
        write = common.write_bytes
        calls = []
        def fail_second(path, data, **kwargs):
            calls.append(data)
            if len(calls) == 2:
                raise OSError("inert")
            write(path, data, **kwargs)
        def edit():
            with patch.object(common, "write_bytes", fail_second):
                common.commit_bytes([("write_bytes", self.path, b"v1"),
                                     ("write_bytes", self.path, b"v2")], root=self.ws.mirror)
        with self.assertRaises(common.PbzError) as caught:
            self._run(edit)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(calls, [b"v1", b"v2"])
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        self.assertFalse(mirror.pending_recovery(self.ws, self.project))

    def test_edit_directory_lifecycle(self):
        from pbz import findreplace
        parent = mirror._transaction_dir(self.ws, self.project)
        for pattern, result in (("= 1", "CHANGED"), ("ABSENT_INERT", "NOTHING_TO_CHANGE")):
            out = findreplace.replace(self.ws, self.project, pattern, "= 2", apply=True)
            self.assertEqual(out["result"], result)
            self.assertFalse([n for n in os.listdir(parent) if n.startswith("edit-")])
        def edit():
            self._write(self.path, b"owned")
            common.replace_bytes(self.path, b"outsider", root=self.ws.mirror)
            raise RuntimeError("inert")
        with self.assertRaises(common.PbzError) as caught:
            self._run(edit)
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        edits = [n for n in os.listdir(parent) if n.startswith("edit-")]
        self.assertEqual(len(edits), 1)
        with open(os.path.join(parent, edits[0], "journal.json")) as handle:
            self.assertEqual(json.load(handle)["state"], "RECOVERY_REQUIRED")

    def test_enlist_refuses_outside_project_only_when_active(self):
        from pbz import mirror_txn
        path = os.path.join(self.ws.mirror, "Other.Report", "inert.json")
        mirror_txn.enlist(path, None, b"inert")
        def edit():
            with self.assertRaises(common.PbzError) as caught:
                mirror_txn.enlist(path, None, b"inert")
            self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
            self.assertEqual(caught.exception.detail, "Other.Report/inert.json")
        self._run(edit)
        self.assertFalse(os.path.exists(path))

    def _assert_rename_write_set(self, kind, old):
        from pbz import mirror_txn, rename
        # Include a separate file referencing all three rename targets.
        common.replace_bytes(self.path, b"table T\n\tmeasure M = 1\n\tcolumn C\n\t\tdataType: int64\n",
                             root=self.ws.mirror)
        reference = os.path.join(os.path.dirname(self.path), "Uses.tmdl")
        common.replace_bytes(reference, b"table Uses\n\tmeasure Ref = [M] + SUM(T[C])\n", root=self.ws.mirror)
        def edit():
            before = file_map(self.ws.mirror)
            rename.rename(self.ws, self.project, kind, old, "Renamed", table="T")
            after = file_map(self.ws.mirror)
            changed = {os.path.abspath(os.path.join(self.ws.mirror, *rel.split("/")))
                       for rel in before.keys() | after.keys() if before.get(rel) != after.get(rel)}
            _ws, _project, directory, _journal = mirror_txn._active.get()
            with open(os.path.join(directory, "journal.json")) as handle:
                journal = json.load(handle)
            recorded = {os.path.abspath(os.path.join(self.ws.mirror, *e["path"].split("/")))
                        for e in journal["entries"]}
            self.assertTrue(recorded)
            self.assertEqual(changed, recorded)
            for path in recorded:
                self.assertTrue(any(common._path_inside(path, root) for root in
                                    (self.project.mirror_model, self.project.mirror_report)), path)
            self.assertIn(reference, changed)
            if kind == "table":
                self.assertIn(self.path, changed)
                self.assertIn(os.path.join(os.path.dirname(self.path), "Renamed.tmdl"), changed)
        self._run(edit)

    def test_table_rename_write_set_is_inside_project(self):
        self._assert_rename_write_set("table", "T")
        self.assertFalse(os.path.exists(self.path))
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(self.path), "Renamed.tmdl")))

    def test_measure_rename_write_set_is_inside_project(self):
        self._assert_rename_write_set("measure", "M")

    def test_column_rename_write_set_is_inside_project(self):
        self._assert_rename_write_set("column", "C")

    def _killed_child(self, *, write, before_mark=False):
        import subprocess
        code = """
import os, sys
from pbz import common, mirror_txn
ws = common.Workspace(sys.argv[1]); ws.private = sys.argv[2]
project = ws.project()
def edit():
    path = sys.argv[3]
    after = b'table T\\n\\tmeasure M = 9\\n'
    if sys.argv[4] == 'before_mark':
        mirror_txn.mark_applied = lambda *args: os._exit(23)
        common.commit_bytes([('write_bytes', path, after)], root=ws.mirror)
    elif sys.argv[4] == 'write':
        common.commit_bytes([('write_bytes', path, after)], root=ws.mirror)
    else:
        mirror_txn.enlist(path, common.read_checked(path, root=ws.mirror), after)
    os._exit(17)
mirror_txn.transaction(ws.mirror, edit, ws=ws, project=project)
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = SKILL + os.pathsep + env.get("PYTHONPATH", "")
        child = subprocess.run([sys.executable, "-c", code, self.ws.root, self.ws.private,
                                self.path, "before_mark" if before_mark else "write" if write else "enlist"], env=env,
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(child.returncode, 23 if before_mark else 17, child.stderr)

    def test_kill_between_journal_and_write(self):
        self._killed_child(write=False)
        pending = mirror.pending_recovery(self.ws, self.project)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "APPLYING")
        directory = os.path.join(mirror._transaction_dir(self.ws, self.project), pending[0]["id"])
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        self.assertEqual(mirror.recover(self.ws, self.project)["result"], "RECOVERED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        self.assertFalse(mirror.pending_recovery(self.ws, self.project))
        self.assertFalse(os.path.exists(directory))

    def test_two_process_stale_lock_recovery(self):
        import subprocess
        self._killed_child(write=True)
        self.assertNotEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        barrier = os.path.join(self.tmp.name, "recover-barrier")
        code = """
import json, os, sys, time
from pbz import common, mirror
ws = common.Workspace(sys.argv[1]); ws.private = sys.argv[2]
project = ws.project()
ready = sys.argv[3] + '.' + sys.argv[4]
with open(ready, 'w'): pass
while not os.path.exists(sys.argv[3]): time.sleep(.005)
print(json.dumps(mirror.recover(ws, project)))
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = SKILL + os.pathsep + env.get("PYTHONPATH", "")
        children = [subprocess.Popen([sys.executable, "-c", code, self.ws.root, self.ws.private,
                                      barrier, str(i)], env=env, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True) for i in range(2)]
        try:
            import time
            deadline = time.monotonic() + 20
            while not all(os.path.exists(barrier + '.' + str(i)) for i in range(2)):
                if time.monotonic() > deadline:
                    self.fail("recovery children failed to reach barrier")
                time.sleep(.01)
            with open(barrier, "w"):
                pass
            results = []
            for child in children:
                stdout, stderr = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, stderr)
                results.append(json.loads(stdout)["result"])
            self.assertEqual(results.count("RECOVERED"), 1, results)
            self.assertIn(next(r for r in results if r != "RECOVERED"),
                          ("COMMIT_BUSY", "NOTHING_TO_RECOVER"))
            self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
            self.assertFalse(mirror.pending_recovery(self.ws, self.project))
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()

    def test_kill_after_write_before_mark_applied_recovers(self):
        self._killed_child(write=True, before_mark=True)
        pending = mirror.pending_recovery(self.ws, self.project)
        self.assertEqual(len(pending), 1)
        directory = os.path.join(mirror._transaction_dir(self.ws, self.project), pending[0]["id"])
        with open(os.path.join(directory, "journal.json")) as handle:
            entry = json.load(handle)["entries"][0]
        current = common.read_checked(self.path, root=self.ws.mirror)
        self.assertNotEqual(current, self.before)
        self.assertEqual(entry["after"], common.sha(current))
        self.assertEqual(entry["applied"], common.sha(self.before))
        self.assertEqual(mirror.recover(self.ws, self.project)["result"], "RECOVERED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        self.assertFalse(os.path.exists(directory))

    def test_local_undo_resyncs_prior_transaction_write(self):
        from unittest.mock import patch
        other = os.path.join(os.path.dirname(self.path), "Fail.tmdl")
        write = common.write_bytes
        def fail_other(path, data, **kwargs):
            if path == other:
                raise OSError("inert")
            return write(path, data, **kwargs)
        def edit():
            self._write(self.path, b"B")
            with patch.object(common, "write_bytes", fail_other):
                common.commit_bytes([("write_bytes", self.path, b"C"),
                                     ("write_bytes", other, b"fail")], root=self.ws.mirror)
        with self.assertRaises(common.PbzError) as caught:
            self._run(edit)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        self.assertFalse(mirror.pending_recovery(self.ws, self.project))

    def _killed_local_undo(self, interrupted):
        import subprocess
        code = """
import os, sys
from unittest.mock import patch
from pbz import common
from pbz.mirror_txn import transaction
ws = common.Workspace(sys.argv[1]); ws.private = sys.argv[2]
project = ws.project()
path = sys.argv[3]
other = os.path.join(os.path.dirname(path), 'Fail.tmdl')
write, replace = common.write_bytes, common.replace_bytes
def fail_other(target, data, **kwargs):
    if target == other:
        raise OSError('inert')
    return write(target, data, **kwargs)
def interrupt_undo(target, data, **kwargs):
    result = replace(target, data, **kwargs)
    if target == path and data == b'B' and sys.argv[4] == 'True':
        os._exit(94)
    return result
def edit():
    common.commit_bytes([('write_bytes', path, b'B')], root=ws.mirror)
    try:
        with patch.object(common, 'write_bytes', fail_other), patch.object(common, 'replace_bytes', interrupt_undo):
            common.commit_bytes([('write_bytes', path, b'C'),
                                 ('write_bytes', other, b'fail')], root=ws.mirror)
    except common.PbzError as error:
        if error.code != 'FAILED':
            raise
        os._exit(95)
transaction(ws.mirror, edit, ws=ws, project=project)
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = SKILL + os.pathsep + env.get("PYTHONPATH", "")
        child = subprocess.run([sys.executable, "-c", code, self.ws.root, self.ws.private,
                                self.path, str(interrupted)], env=env, capture_output=True, text=True)
        self.assertEqual(child.returncode, 94 if interrupted else 95, child.stderr)
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), b"B")
        self.assertEqual(len(mirror.pending_recovery(self.ws, self.project)), 1)

    def test_interrupted_local_undo_recovers(self):
        self._killed_local_undo(True)
        self.assertEqual(mirror.recover(self.ws, self.project)["result"], "RECOVERED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        self.assertFalse(mirror.pending_recovery(self.ws, self.project))

    def test_superseded_version_after_local_undo_is_external_conflict(self):
        self._killed_local_undo(False)
        common.replace_bytes(self.path, b"C", root=self.ws.mirror)
        self.assertEqual(mirror.recover(self.ws, self.project)["result"], "RECOVERY_REQUIRED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), b"C")
        self.assertTrue(mirror.pending_recovery(self.ws, self.project))

    def _terminal_save_interrupt(self, persisted):
        from pbz import mirror_txn
        save = mirror_txn._save
        def interrupt_commit(directory, journal):
            if journal["state"] == "COMMITTED":
                if persisted:
                    save(directory, journal)
                raise KeyboardInterrupt("terminal save interrupted")
            return save(directory, journal)
        def edit():
            self._write(self.path, b"new")
            return "fn result"
        with patch.object(mirror_txn, "_save", interrupt_commit):
            if persisted:
                self.assertEqual(self._run(edit), "fn result")
            else:
                with self.assertRaisesRegex(KeyboardInterrupt, "terminal save interrupted"):
                    self._run(edit)
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror),
                         b"new" if persisted else self.before)
        self.assertFalse(mirror.pending_recovery(self.ws, self.project))
        parent = mirror._transaction_dir(self.ws, self.project)
        self.assertFalse([n for n in os.listdir(parent) if n.startswith("edit-")])

    def test_terminal_save_interrupted_before_persisting(self):
        self._terminal_save_interrupt(False)

    def test_terminal_save_interrupted_after_persisting(self):
        self._terminal_save_interrupt(True)

    def _terminal_save_failure(self, persisted):
        from pbz import mirror_txn
        from unittest.mock import patch
        save = mirror_txn._save
        def fail_commit(directory, journal):
            if journal["state"] == "COMMITTED":
                if persisted:
                    save(directory, journal)
                raise OSError("terminal save failed")
            return save(directory, journal)
        def edit():
            self._write(self.path, b"new")
            return "fn result"
        with patch.object(mirror_txn, "_save", fail_commit):
            if persisted:
                self.assertEqual(self._run(edit), "fn result")
            else:
                with self.assertRaisesRegex(OSError, "terminal save failed"):
                    self._run(edit)
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror),
                         b"new" if persisted else self.before)
        self.assertFalse(mirror.pending_recovery(self.ws, self.project))
        parent = mirror._transaction_dir(self.ws, self.project)
        self.assertFalse([n for n in os.listdir(parent) if n.startswith("edit-")])

    def test_terminal_save_fails_before_persisting(self):
        self._terminal_save_failure(False)

    def test_terminal_save_fails_after_persisting(self):
        self._terminal_save_failure(True)

    def test_killed_edit_external_conflict_keeps_directory(self):
        self._killed_child(write=True)
        pending = mirror.pending_recovery(self.ws, self.project)
        self.assertEqual(len(pending), 1)
        directory = os.path.join(mirror._transaction_dir(self.ws, self.project), pending[0]["id"])
        common.replace_bytes(self.path, b"outsider", root=self.ws.mirror)
        self.assertEqual(mirror.recover(self.ws, self.project)["result"], "RECOVERY_REQUIRED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), b"outsider")
        self.assertTrue(os.path.isdir(directory))

    def test_unrelatable_paths_raise_recovery_required(self):
        from pbz import mirror_txn
        from unittest.mock import patch
        def edit():
            mirror_txn.enlist(self.path, self.before, b"new")
            with patch.object(os.path, "relpath", side_effect=ValueError("inert")):
                for call in (lambda: mirror_txn.enlist(self.path, self.before, b"new"),
                             lambda: mirror_txn.mark_applied(self.path, self.before)):
                    with self.assertRaises(common.PbzError) as caught:
                        call()
                    self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
                    self.assertEqual(caught.exception.detail, os.path.basename(self.path))
        self._run(edit)

    @unittest.skipIf(os.name == "nt", "fcntl recovery guard seam")
    def test_recovery_guard_excludes_concurrent_recovery(self):
        import subprocess
        import time
        self._killed_child(write=True)
        current = common.read_checked(self.path, root=self.ws.mirror)
        pending = mirror.pending_recovery(self.ws, self.project)
        guard = os.path.join(mirror._transaction_dir(self.ws, self.project), ".commit-lock.recover")
        ready = os.path.join(self.tmp.name, "guard-ready")
        release = os.path.join(self.tmp.name, "guard-release")
        code = """
import fcntl, os, sys, time
with open(sys.argv[1], 'a+b') as handle:
    fcntl.flock(handle, fcntl.LOCK_EX)
    with open(sys.argv[2], 'w'): pass
    while not os.path.exists(sys.argv[3]): time.sleep(.01)
"""
        child = subprocess.Popen([sys.executable, "-c", code, guard, ready, release],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 20
            while not os.path.exists(ready):
                if child.poll() is not None or time.monotonic() > deadline:
                    self.fail("guard child failed to signal readiness")
                time.sleep(.01)
            self.assertEqual(mirror.recover(self.ws, self.project)["result"], "COMMIT_BUSY")
            self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), current)
            self.assertEqual(mirror.pending_recovery(self.ws, self.project), pending)
            with open(release, "w"): pass
            _stdout, stderr = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, stderr)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
        self.assertEqual(mirror.recover(self.ws, self.project)["result"], "RECOVERED")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        self.assertFalse(mirror.pending_recovery(self.ws, self.project))

    def test_recover_propagates_anchor_refusal(self):
        from unittest.mock import patch
        with patch.object(mirror, "anchor_mirror", side_effect=common.PbzError("UNSAFE_PATH", "inert")):
            with self.assertRaises(common.PbzError) as caught:
                mirror.recover(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(common.read_checked(self.path, root=self.ws.mirror), self.before)
        self.assertFalse(os.path.exists(os.path.join(mirror._transaction_dir(self.ws, self.project), ".commit-lock")))

    def test_restore_rejects_completed_journal(self):
        from pbz import mirror_txn
        directory = os.path.join(mirror._transaction_dir(self.ws, self.project), "edit-test")
        os.makedirs(directory)
        for state in ("COMMITTED", "ROLLED_BACK", "PREPARED", "INVALID"):
            result = mirror_txn.restore(self.ws, self.project, directory,
                                        {"state": state, "entries": []})
            self.assertEqual(result["result"], "RECOVERY_REQUIRED")


if __name__ == "__main__":
    unittest.main()
