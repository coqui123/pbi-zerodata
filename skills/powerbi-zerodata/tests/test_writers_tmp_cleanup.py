"""A failed temp replace removes its `.pbz-tmp` and leaves the target bytes alone.

Run: python tests/test_writers_tmp_cleanup.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz.common import PbzError, commit_bytes, write_bytes, write_text  # noqa: E402


class WriterTmpCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "note.txt")
        with open(self.path, "wb") as f:
            f.write(b"KEEP")
        real = os.replace

        def boom(src, dst):
            if str(src).endswith(".pbz-tmp"):
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


if __name__ == "__main__":
    unittest.main()
