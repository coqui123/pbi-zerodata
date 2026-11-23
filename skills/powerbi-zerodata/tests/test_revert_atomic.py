"""Revert plans every file, then one commit. Run: python tests/test_revert_atomic.py
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


def file_map(root):
    out = {}
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            with open(path, "rb") as f:
                out[rel] = f.read()
    return out


class RevertAtomicTests(unittest.TestCase):
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
        self.paths = []
        for dirpath, _dirs, names in os.walk(self.project.mirror_model):
            for name in names:
                if name.endswith(".tmdl"):
                    self.paths.append(os.path.join(dirpath, name))
        self.paths = sorted(self.paths)[:2]
        self.assertGreaterEqual(len(self.paths), 2)
        for path in self.paths:
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n")

    def test_pending_recovery_refuses_before_planning(self):
        directory = os.path.join(mirror._transaction_dir(self.ws, self.project), "edit-pending")
        os.makedirs(directory)
        with open(os.path.join(directory, "journal.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "kind": "mirror_edit", "state": "APPLYING", "entries": []}, handle)
        self.assertTrue(mirror.pending_recovery(self.ws, self.project))
        before = file_map(self.ws.mirror)
        private = file_map(self.ws.private)
        for obj in (None, "measure Sales[Total Sales]"):
            with self.subTest(obj=obj):
                with self.assertRaises(common.PbzError) as caught:
                    mirror.revert(self.ws, self.project, obj=obj)
                self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
                self.assertEqual(caught.exception.detail, self.project.name)
                self.assertEqual(file_map(self.ws.mirror), before)
                self.assertEqual(file_map(self.ws.private), private)

    def test_held_lock_preserves_owner_writes(self):
        directory = mirror._transaction_dir(self.ws, self.project)
        os.makedirs(directory, exist_ok=True)
        lock = os.path.join(directory, ".commit-lock")
        with open(lock, "wb") as handle:
            handle.write(b"concurrent owner")
        with open(self.paths[0], "ab") as handle:
            handle.write(b"// concurrent owner save\n")
        before = file_map(self.ws.mirror)
        private = file_map(self.ws.private)
        for obj in (None, "measure Sales[Total Sales]"):
            with self.subTest(obj=obj):
                with self.assertRaises(common.PbzError) as caught:
                    mirror.revert(self.ws, self.project, obj=obj)
                self.assertEqual(caught.exception.code, "COMMIT_BUSY")
                self.assertEqual(caught.exception.detail, "project commit lock is held")
                self.assertEqual(file_map(self.ws.mirror), before)
                self.assertEqual(file_map(self.ws.private), private)

    def test_whole_file_result_control(self):
        result = mirror.revert(self.ws, self.project)
        files = sorted(os.path.relpath(p, self.ws.mirror).replace("\\", "/") for p in self.paths)
        self.assertEqual(result, {"verb": "revert", "project": self.project.name,
                                  "files": files, "result": "REVERTED"})
        self.assertEqual(mirror.revert(self.ws, self.project),
                         {"verb": "revert", "project": self.project.name,
                          "files": [], "result": "NOTHING_TO_REVERT"})

    def test_object_result_control(self):
        path = os.path.join(self.project.mirror_model, "definition", "tables", "Sales.tmdl")
        text, bom = common.read_text(path)
        common.write_text(path, text.replace("SUM(Sales[Amount])", "42", 1), bom)
        result = mirror.revert(self.ws, self.project, obj="measure Sales[Total Sales]")
        self.assertEqual(result, {"verb": "revert", "project": self.project.name,
                                  "object": "measure Sales[Total Sales]", "result": "REVERTED",
                                  "files": [self.project.name + ".SemanticModel/definition/tables/Sales.tmdl"]})
        self.assertEqual(common.read_text(path), (text, bom))

    def test_object_target_swap_refuses_stale_replacement(self):
        for seam in ("apply_block", "commit_bytes"):
            with self.subTest(seam=seam):
                path = os.path.join(self.project.mirror_model, "definition", "tables", "Sales.tmdl")
                before = file_map(self.ws.mirror)
                external = before[os.path.relpath(path, self.ws.mirror).replace("\\", "/")] + b"// external save\n"
                real = getattr(mirror, seam)

                def swapped(*args, **kwargs):
                    with open(path, "wb") as handle:
                        handle.write(external)
                    return real(*args, **kwargs)

                with patch("pbz.mirror." + seam, swapped):
                    with self.assertRaises(common.PbzError) as caught:
                        mirror.revert(self.ws, self.project, obj="measure Sales[Total Sales]")
                self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
                before[os.path.relpath(path, self.ws.mirror).replace("\\", "/")] = external
                self.assertEqual(file_map(self.ws.mirror), before)
                self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])

    def test_deleted_target_recreated_before_commit_refuses_whole_edit(self):
        path = self.paths[0]
        os.remove(path)
        added = os.path.join(self.project.mirror_model, "definition", "tables", "Added.tmdl")
        with open(added, "wb") as handle:
            handle.write(b"table Added\n")
        before = file_map(self.ws.mirror)
        external = b"\xef\xbb\xbf// recreated by editor\r\n"
        real = mirror.commit_bytes

        def swapped(*args, **kwargs):
            with open(path, "wb") as handle:
                handle.write(external)
            return real(*args, **kwargs)

        with patch("pbz.mirror.commit_bytes", swapped):
            with self.assertRaises(common.PbzError) as caught:
                mirror.revert(self.ws, self.project)
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        before[os.path.relpath(path, self.ws.mirror).replace("\\", "/")] = external
        self.assertEqual(file_map(self.ws.mirror), before)
        self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])

    def test_deleted_target_recreated_after_classification_refuses_whole_edit(self):
        path = self.paths[0]
        os.remove(path)
        added = os.path.join(self.project.mirror_model, "definition", "tables", "Added.tmdl")
        with open(added, "wb") as handle:
            handle.write(b"table Added\n")
        before = file_map(self.ws.mirror)
        external = b"\xef\xbb\xbf// recreated after classification\r\n"
        real = mirror._select

        def swapped(changes, only):
            rel = os.path.relpath(path, self.ws.mirror).replace("\\", "/")
            self.assertIn(rel, changes["deleted"])
            with open(path, "wb") as handle:
                handle.write(external)
            return real(changes, only)

        with patch("pbz.mirror._select", swapped):
            with self.assertRaises(common.PbzError) as caught:
                mirror.revert(self.ws, self.project)
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        before[os.path.relpath(path, self.ws.mirror).replace("\\", "/")] = external
        self.assertEqual(file_map(self.ws.mirror), before)
        self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])

    def test_changed_target_swapped_before_commit_refuses_whole_edit(self):
        path = self.paths[0]
        before = file_map(self.ws.mirror)
        external = b"\xef\xbb\xbf// swapped by editor\r\n"
        real = mirror.commit_bytes

        def swapped(*args, **kwargs):
            with open(path, "wb") as handle:
                handle.write(external)
            return real(*args, **kwargs)

        with patch("pbz.mirror.commit_bytes", swapped):
            with self.assertRaises(common.PbzError) as caught:
                mirror.revert(self.ws, self.project)
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        before[os.path.relpath(path, self.ws.mirror).replace("\\", "/")] = external
        self.assertEqual(file_map(self.ws.mirror), before)
        self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])

    def test_uninspected_manifest_target_refuses_whole_edit(self):
        rel = self.project.name + ".SemanticModel/../Outside.tmdl"
        path = os.path.join(self.ws.mirror, *rel.split("/"))
        self.assertTrue(mirror._path_outside_project(path, rel, self.ws, self.project))
        self.assertFalse(os.path.exists(path))
        man = mirror.load_manifest(self.ws, self.project)
        man["files"][rel] = {"mirror": common.sha("table Outside\n")}
        mirror.save_manifest(self.ws, self.project, man)
        base = os.path.join(mirror.base_dir(self.ws, self.project), *rel.split("/"))
        common.write_text(base, "table Outside\n")
        before = file_map(self.ws.mirror)

        with patch("pbz.mirror.commit_bytes", wraps=mirror.commit_bytes) as commit, \
                patch("pbz.common.write_text", wraps=common.write_text) as write:
            with self.assertRaises(common.PbzError) as caught:
                mirror.revert(self.ws, self.project)
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        self.assertIn(rel, caught.exception.detail)
        self.assertIn("not inspected", caught.exception.detail)
        commit.assert_not_called()
        write.assert_not_called()
        self.assertEqual(file_map(self.ws.mirror), before)
        self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])

        selected = os.path.relpath(self.paths[0], self.ws.mirror).replace("\\", "/")
        result = mirror.revert(self.ws, self.project, paths=[selected])
        self.assertEqual(result["files"], [selected])
        self.assertFalse(os.path.exists(path))

    def test_second_write_rolls_back(self):
        before = file_map(self.ws.mirror)
        real = common.write_text
        seen = {"n": 0}

        def wrapped(path, text, bom=b"", *, root=None, **_kw):
            seen["n"] += 1
            if seen["n"] >= 2:
                raise OSError("second write")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", wrapped):
            with self.assertRaises(common.PbzError) as caught:
                mirror.revert(self.ws, self.project)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual(file_map(self.ws.mirror), before)
        self.assertFalse(any(name.endswith(".pbz-tmp") for name in os.listdir(self.ws.mirror)))
        for dirpath, _dirs, names in os.walk(self.ws.root):
            for name in names:
                self.assertFalse(name.endswith(".pbz-tmp"))

    def test_unreadable_target_refuses_before_write(self):
        before = file_map(self.ws.mirror)
        real = mirror.read_text

        def wrapped(path):
            if path.startswith(mirror.base_dir(self.ws, self.project)):
                raise UnicodeError("bad")
            return real(path)

        with patch("pbz.mirror.read_text", wrapped), patch("pbz.common.write_text", side_effect=AssertionError("write")):
            with self.assertRaises(common.PbzError) as caught:
                mirror.revert(self.ws, self.project)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIn("bad", caught.exception.detail)
        self.assertEqual(file_map(self.ws.mirror), before)

    def test_private_base_junction_refuses_before_read(self):
        before = file_map(self.ws.mirror)
        mirror_file = self.paths[0]
        rel = os.path.relpath(mirror_file, self.ws.mirror).replace("\\", "/")
        base = os.path.abspath(os.path.join(mirror.base_dir(self.ws, self.project), *rel.split("/")))
        self.assertTrue(os.path.isfile(base))
        private = os.path.abspath(self.ws.private)
        ancestor = os.path.dirname(base)
        self.assertNotEqual(os.path.normcase(ancestor), os.path.normcase(private))
        self.assertTrue(os.path.normcase(ancestor).startswith(os.path.normcase(private) + os.sep))
        outside = os.path.join(self.tmp.name, "outside")
        outside_file = os.path.join(outside, os.path.relpath(base, ancestor))
        os.makedirs(os.path.dirname(outside_file), exist_ok=True)
        with open(outside_file, "wb") as handle:
            handle.write(b"OUTSIDE")
        os.rename(ancestor, os.path.join(self.tmp.name, "parked-base"))
        if not make_junction(None, ancestor, outside):
            self.skipTest("creating a junction is unavailable")
        self.addCleanup(os.rmdir, ancestor)
        with self.assertRaises(common.PbzError) as caught:
            mirror.revert(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(file_map(self.ws.mirror), before)
        with open(outside_file, "rb") as handle:
            self.assertEqual(handle.read(), b"OUTSIDE")


if __name__ == "__main__":
    unittest.main()
