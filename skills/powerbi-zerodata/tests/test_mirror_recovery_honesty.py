"""Pending recovery blocks every pull, and a bad journal is not echoed.
Run: python tests/test_mirror_recovery_honesty.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

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
            self._write(other_path, common.read_checked(other_path, root=self.ws.mirror) + b"\n")
            with patch("pbz.validate.engine_call", return_value={"ok": True}):
                pushed = mirror.push(self.ws, other)
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


if __name__ == "__main__":
    unittest.main()
