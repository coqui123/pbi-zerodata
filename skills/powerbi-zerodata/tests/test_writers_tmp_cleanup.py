"""A failed temp replace removes its `.pbz-tmp` and leaves the target bytes alone.

Run: python tests/test_writers_tmp_cleanup.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

from pbz import images, mirror  # noqa: E402
from pbz.common import PbzError, commit_bytes, write_bytes, write_text  # noqa: E402


class WriterTmpCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "note.txt")
        with open(self.path, "wb") as f:
            f.write(b"KEEP")
        real = os.replace
        state = {"n": 0}

        def boom(src, dst):
            if str(src).endswith(".pbz-tmp"):
                state["n"] += 1
                if state["n"] == 1:
                    raise OSError("replace")
            return real(src, dst)

        self.boom = boom

    def _assert_clean(self):
        self.assertEqual(open(self.path, "rb").read(), b"KEEP")
        self.assertEqual(list(Path(self.tmp.name).rglob("*.pbz-tmp")), [])

    def test_write_text_replace_failure_drops_tmp(self):
        with patch("os.replace", self.boom):
            with self.assertRaises(OSError):
                write_text(self.path, "NEW")
        self._assert_clean()

    def test_write_bytes_replace_failure_drops_tmp(self):
        with patch("os.replace", self.boom):
            with self.assertRaises(OSError):
                write_bytes(self.path, b"NEW")
        self._assert_clean()

    def test_commit_bytes_replace_failure_drops_tmp(self):
        with patch("os.replace", self.boom):
            with self.assertRaises(PbzError) as caught:
                commit_bytes([("write", self.path, "NEW", b"")])
        self.assertEqual(caught.exception.code, "FAILED")
        self._assert_clean()

    def _junction(self, link: str, target: str) -> None:
        from _junction import make_junction
        make_junction(self, link, target)

    def test_missing_mklink_skips_instead_of_erroring(self):
        import _junction
        with patch.object(_junction.subprocess, "run", side_effect=FileNotFoundError("cmd")):
            with self.assertRaises(unittest.SkipTest):
                _junction.make_junction(self, os.path.join(self.tmp.name, "missing-link"), self.tmp.name)

    def test_junction_destination_and_temp_are_not_followed(self):
        real = os.path.join(self.tmp.name, "real")
        os.makedirs(real)
        kept = os.path.join(real, "kept.txt")
        with open(kept, "wb") as f:
            f.write(b"KEEP")
        dest = os.path.join(self.tmp.name, "dest-link")
        self._junction(dest, real)
        with self.assertRaises(PbzError) as caught:
            write_text(dest, "NEW")
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(open(kept, "rb").read(), b"KEEP")
        sibling = self.path + ".pbz-tmp"
        self._junction(sibling, real)
        write_text(self.path, "NEW")
        self.assertEqual(open(self.path, "rb").read(), b"NEW")
        self.assertTrue(os.path.isdir(sibling))
        self.assertEqual(open(kept, "rb").read(), b"KEEP")

    def test_close_failure_still_drops_owned_temp(self):
        real_close = os.close
        seen = {"fd": -1, "failed": False, "closed": False}

        def boom(fd):
            seen["fd"] = fd
            if not seen["failed"]:
                seen["failed"] = True
                raise OSError("close")
            seen["closed"] = True
            return real_close(fd)

        with patch("os.close", boom):
            with self.assertRaises(OSError):
                write_text(self.path, "NEW", root=self.tmp.name)
        self.assertTrue(seen["failed"])
        self.assertTrue(seen["closed"])
        with self.assertRaises(OSError):
            os.fstat(seen["fd"])
        self.assertEqual(open(self.path, "rb").read(), b"KEEP")
        self._assert_clean()

    def test_metadata_failure_does_not_delete_swapped_temp(self):
        foreign = os.path.join(self.tmp.name, "foreign.pbz-tmp")
        with open(foreign, "wb") as f:
            f.write(b"FOREIGN")
        real_fstat = os.fstat
        names = {}

        def swap(fd):
            info = real_fstat(fd)
            tmp = names.get("tmp")
            if not tmp or not os.path.basename(tmp).startswith(".pbz-"):
                return info
            os.replace(tmp, tmp + ".away")
            os.replace(foreign, tmp)
            raise OSError("fstat")

        real_mkstemp = __import__("tempfile").mkstemp

        def named(*args, **kwargs):
            fd, tmp = real_mkstemp(*args, **kwargs)
            names["tmp"] = tmp
            return fd, tmp

        removed = []
        real_remove = os.remove

        def watch(path):
            removed.append(os.path.abspath(path))
            return real_remove(path)

        with patch("tempfile.mkstemp", named), patch("os.fstat", swap), patch("os.remove", watch):
            with self.assertRaises(OSError):
                write_text(self.path, "NEW", root=self.tmp.name)
        self.assertEqual(open(self.path, "rb").read(), b"KEEP")
        held = [p for p in Path(self.tmp.name).rglob("*") if p.is_file() and p.read_bytes() == b"FOREIGN"]
        self.assertTrue(held)
        self.assertFalse(any(os.path.abspath(p) in removed for p in held))

    def test_metadata_failure_without_swap_drops_owned_temp(self):
        def fail(_fd):
            raise OSError("fstat")

        with patch("os.fstat", fail):
            with self.assertRaises(OSError):
                write_text(self.path, "NEW", root=self.tmp.name)
        self.assertEqual(open(self.path, "rb").read(), b"KEEP")
        self.assertEqual(list(Path(self.tmp.name).rglob("*.pbz-tmp")), [])

    def test_malformed_ops_leave_the_file_map_unchanged(self):
        before = open(self.path, "rb").read()
        opened = []
        real_open = open

        def watch(path, *args, **kwargs):
            opened.append(str(path))
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", watch):
            for op in (("write", self.path), ("write", self.path, 1), ("write_bytes", self.path, "x"),
                       ("delete", self.path, "extra"), ("nope", self.path)):
                with self.assertRaises(PbzError) as caught:
                    commit_bytes([op], root=self.tmp.name)
                self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertEqual(open(self.path, "rb").read(), before)
        self.assertEqual(opened, [])

    def test_parent_junction_below_root_refuses_before_read(self):
        root = os.path.join(self.tmp.name, "root")
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(root)
        os.makedirs(outside)
        secret = os.path.join(outside, "note.txt")
        with open(secret, "wb") as f:
            f.write(b"CANARY-SECRET")
        link = os.path.join(root, "link")
        self._junction(link, outside)
        target = os.path.join(link, "note.txt")
        opened = []
        real_open = open
        secret_id = (os.stat(secret).st_dev, os.stat(secret).st_ino)

        def watch(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            try:
                info = os.fstat(handle.fileno())
                opened.append((info.st_dev, info.st_ino))
            except OSError:
                opened.append(os.path.realpath(str(path)))
            return handle

        with patch("builtins.open", watch):
            with self.assertRaises(PbzError) as caught:
                commit_bytes([("write", target, "NEW", b"")], root=root)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn(secret_id, opened)
        self.assertEqual(open(secret, "rb").read(), b"CANARY-SECRET")

    def test_callers_refuse_parent_junction_before_content(self):
        root = os.path.join(self.tmp.name, "caller-root")
        outside = os.path.join(self.tmp.name, "caller-out")
        os.makedirs(os.path.join(outside, "RegisteredResources"))
        report = os.path.join(root, "report")
        os.makedirs(report)
        secret = os.path.join(outside, "RegisteredResources", "a.png")
        with open(secret, "wb") as f:
            f.write(b"CANARY-IMAGE")
        copied = os.path.join(outside, "copied.bin")
        with open(copied, "wb") as f:
            f.write(b"CANARY-COPY")
        link = os.path.join(report, "StaticResources")
        self._junction(link, outside)
        src = os.path.join(root, "safe.png")
        with open(src, "wb") as f:
            f.write(b"SAFE")
        backup = os.path.join(root, "backup")
        os.makedirs(backup)

        def dump(path):
            out = {}
            if not os.path.isdir(path):
                return out
            for cur, _dirs, files in os.walk(path):
                for name in files:
                    full = os.path.join(cur, name)
                    with open(full, "rb") as f:
                        out[os.path.relpath(full, path)] = f.read()
            return out

        before_out, before_src = dump(outside), open(src, "rb").read()
        opened = []
        real_open = open
        secret_ids = {(os.stat(secret).st_dev, os.stat(secret).st_ino),
                      (os.stat(copied).st_dev, os.stat(copied).st_ino)}

        def watch(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            try:
                info = os.fstat(handle.fileno())
                opened.append((info.st_dev, info.st_ino))
            except OSError:
                opened.append(os.path.realpath(str(path)))
            return handle

        with patch("builtins.open", watch):
            with self.assertRaises(PbzError) as caught:
                images.delete(["a.png"], report, backup_dir=None, root=root)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            with self.assertRaises(PbzError) as caught:
                images.install({"a.png": src}, report, backup_dir=backup, root=root)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            with self.assertRaises(PbzError) as caught:
                mirror._copy_atomic(src, os.path.join(link, "copied.bin"), root=root)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertFalse(secret_ids & set(opened))
        self.assertEqual(dump(outside), before_out)
        self.assertEqual(open(src, "rb").read(), before_src)
        self.assertFalse(any(b"CANARY" in blob for blob in dump(backup).values()))

    def test_junction_above_workspace_root_still_writes(self):
        real = os.path.join(self.tmp.name, "real-parent")
        os.makedirs(real)
        via = os.path.join(self.tmp.name, "via")
        self._junction(via, real)
        root = os.path.join(via, "ws")
        os.makedirs(root)
        path = os.path.join(root, "note.txt")
        commit_bytes([("write", path, "OK", b"")], root=root)
        self.assertEqual(open(path, encoding="utf-8").read(), "OK")

    def test_snapshot_rechecks_later_path_before_read(self):
        root = os.path.join(self.tmp.name, "snap-root")
        first_dir = os.path.join(root, "a")
        second_dir = os.path.join(root, "b")
        os.makedirs(first_dir)
        os.makedirs(second_dir)
        first = os.path.join(first_dir, "one.txt")
        second = os.path.join(second_dir, "two.txt")
        with open(first, "wb") as f:
            f.write(b"ONE")
        with open(second, "wb") as f:
            f.write(b"TWO")
        outside = os.path.join(self.tmp.name, "snap-out")
        os.makedirs(outside)
        # Canary at the basename the later snapshot path would resolve through the junction.
        secret = os.path.join(outside, "two.txt")
        with open(secret, "wb") as f:
            f.write(b"CANARY-SNAP")
        secret_id = (os.lstat(secret).st_dev, os.lstat(secret).st_ino)

        def dump(base):
            out = {}
            for dirpath, _dirs, names in os.walk(base):
                for name in names:
                    path = os.path.join(dirpath, name)
                    rel = os.path.relpath(path, base).replace("\\", "/")
                    with open(path, "rb") as f:
                        out[rel] = f.read()
            return out

        before_root = dump(root)
        before_out = dump(outside)
        real_open = open
        external_opens = []
        external_reads = []
        swapped = {"done": False}
        held = second_dir + ".held"

        def watch(path, *args, **kwargs):
            text = str(path)
            mode = args[0] if args else kwargs.get("mode", "r")
            if ("rb" in str(mode) and os.path.abspath(text) == os.path.abspath(first)
                    and not swapped["done"]):
                swapped["done"] = True
                os.rename(second_dir, held)
                self._junction(second_dir, outside)
            handle = real_open(path, *args, **kwargs)
            try:
                info = os.fstat(handle.fileno())
                if (info.st_dev, info.st_ino) == secret_id:
                    external_opens.append(text)
                    orig_read = handle.read

                    def tracked(*a, **k):
                        external_reads.append(b"read")
                        raise AssertionError("external snapshot bytes were read")

                    handle.read = tracked
            except OSError:
                pass
            return handle

        # Positive control: after the same retarget, opening `second` hits the canary.
        os.rename(second_dir, held)
        self._junction(second_dir, outside)
        try:
            with patch("builtins.open", watch):
                with open(second, "rb") as probe:
                    with self.assertRaises(AssertionError):
                        probe.read()
            self.assertEqual(len(external_opens), 1)
            self.assertEqual(external_reads, [b"read"])
        finally:
            os.rmdir(second_dir)
            os.rename(held, second_dir)
        external_opens.clear()
        external_reads.clear()
        swapped["done"] = False

        try:
            with patch("builtins.open", watch):
                with self.assertRaises(PbzError) as caught:
                    commit_bytes([("write", first, "NEW1", b""), ("write", second, "NEW2", b"")],
                                 root=root)
            self.assertTrue(swapped["done"])
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertEqual(external_opens, [])
            self.assertEqual(external_reads, [])
            self.assertEqual(dump(outside), before_out)
            self.assertEqual(list(Path(root).rglob("*.pbz-tmp")), [])
            self.assertEqual(list(Path(outside).rglob("*.pbz-tmp")), [])
        finally:
            if os.path.isdir(held):
                if os.path.isdir(second_dir):
                    os.rmdir(second_dir)
                os.rename(held, second_dir)
        # After undoing only the test junction, destination map (incl. original two.txt) matches.
        self.assertEqual(dump(root), before_root)
        self.assertEqual(dump(outside), before_out)

    def test_snapshot_rejects_swapped_external_handle(self):
        root = os.path.join(self.tmp.name, "id-root")
        os.makedirs(root)
        path = os.path.join(root, "note.txt")
        with open(path, "wb") as f:
            f.write(b"KEEP")
        outside = os.path.join(self.tmp.name, "id-out")
        os.makedirs(outside)
        secret = os.path.join(outside, "note.txt")
        with open(secret, "wb") as f:
            f.write(b"CANARY-PRIVATE")
        before = open(path, "rb").read()
        before_out = open(secret, "rb").read()
        real_open = open
        external_reads = []

        def swap(p, *args, **kwargs):
            mode = args[0] if args else kwargs.get("mode", "r")
            if "rb" in str(mode) and os.path.abspath(str(p)) == os.path.abspath(path):
                # Return an open handle to the external file while the pathname still
                # names the workspace leaf. Identity mismatch must refuse before read.
                handle = real_open(secret, "rb")

                def watch(*a, **k):
                    external_reads.append(b"read")
                    raise AssertionError("external snapshot bytes were read")

                handle.read = watch
                return handle
            return real_open(p, *args, **kwargs)

        with patch("builtins.open", swap):
            with self.assertRaises(PbzError) as caught:
                commit_bytes([("write", path, "NEW", b"")], root=root)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(external_reads, [])
        self.assertEqual(open(path, "rb").read(), before)
        self.assertEqual(open(secret, "rb").read(), before_out)
        self.assertEqual(list(Path(root).rglob("*.pbz-tmp")), [])
        self.assertEqual(list(Path(outside).rglob("*.pbz-tmp")), [])


if __name__ == "__main__":
    unittest.main()
