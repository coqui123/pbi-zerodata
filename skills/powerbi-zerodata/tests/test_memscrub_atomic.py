"""memscrub --apply is human-only and one private commit. A sandbox user name is not isolation.

Run: python tests/test_memscrub_atomic.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import cli, common, memscrub, policy  # noqa: E402
from pbz.common import PbzError  # noqa: E402


class MemscrubAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = common.Workspace(self.tmp.name)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.notes = os.path.join(self.tmp.name, "notes")
        os.makedirs(self.notes)
        self.a = os.path.join(self.notes, "a.md")
        self.b = os.path.join(self.notes, "b.md")
        for path in (self.a, self.b):
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("spent 1000 hours\n")
        policy.save(self.ws, {**policy.DEFAULT, "memory_dirs": [self.notes]})

    def test_nonhuman_apply_writes_nothing(self):
        before = {p: open(p, "rb").read() for p in (self.a, self.b)}
        with self.assertRaises(PbzError) as caught:
            memscrub.run(self.ws, None, self.notes, apply=True, human=False)
        self.assertEqual(caught.exception.code, "HUMAN_ONLY")
        self.assertEqual({p: open(p, "rb").read() for p in before}, before)
        self.assertFalse(os.path.isdir(os.path.join(self.ws.private, "memory-backup")))

    def test_mid_write_restores_notes(self):
        before = {p: open(p, "rb").read() for p in (self.a, self.b)}
        real = common.write_text
        seen = {"n": 0}

        def boom(path, text, bom=b"", *, root=None, **_kw):
            if path.replace("\\", "/").endswith(".md") and "memory-backup" not in path.replace("\\", "/"):
                seen["n"] += 1
                if seen["n"] > 1:
                    raise OSError("mid")
            return real(path, text, bom, root=root)

        with patch("pbz.common.write_text", boom):
            with self.assertRaises(PbzError) as caught:
                memscrub.run(self.ws, None, self.notes, apply=True, human=True)
        self.assertEqual(caught.exception.code, "FAILED")
        self.assertEqual({p: open(p, "rb").read() for p in before}, before)

    def test_short_temp_path_is_inside_approved_roots(self):
        import ctypes
        parent = tempfile.mkdtemp(prefix="longname_memory_")
        self.addCleanup(lambda: __import__("shutil").rmtree(parent, ignore_errors=True))
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetShortPathNameW(parent, buf, len(buf))
        if n <= 0 or os.path.normcase(buf.value) == os.path.normcase(parent):
            self.skipTest("8.3 short names are unavailable")
        notes = os.path.join(buf.value, "notes")
        os.makedirs(notes)
        note = os.path.join(notes, "a.md")
        with open(note, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("spent 1000 hours\n")
        policy.save(self.ws, {**policy.DEFAULT, "memory_dirs": [notes]})
        with self.assertRaises(PbzError) as caught:
            memscrub.run(self.ws, None, notes, apply=True, human=False)
        self.assertEqual(caught.exception.code, "HUMAN_ONLY")
        self.assertEqual(open(note, encoding="utf-8").read(), "spent 1000 hours\n")

    def test_username_alone_does_not_configure_isolation(self):
        with patch.dict(os.environ, {"USERNAME": "WDAGUtilityAccount"}):
            out = cli.cmd_doctor()
        self.assertTrue(all(item["status"] == "unverified" for item in out["isolation"]))
        flags = {spec[0] for spec in __import__("pbz.mcp", fromlist=["mcp"]).BY_NAME["memscrub"].opts.values()}
        self.assertNotIn("--apply", flags)

    def test_substring_and_dotdot_are_not_approved(self):
        trick = os.path.join(self.tmp.name, "not", ".codex", "memories")
        os.makedirs(trick)
        secret = os.path.join(trick, "note.md")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("CANARY-SECRET")
        with self.assertRaises(PbzError) as caught:
            memscrub.run(self.ws, None, trick, show="note.md")
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn("CANARY-SECRET", str(caught.exception))
        with self.assertRaises(PbzError) as caught:
            memscrub.run(self.ws, None, self.notes, show="../note.md")
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")

    def test_symlink_show_is_unsafe_path(self):
        outside = os.path.join(self.tmp.name, "outside.md")
        with open(outside, "w", encoding="utf-8") as f:
            f.write("CANARY-OUTSIDE")
        link = os.path.join(self.notes, "leak.md")
        try:
            os.symlink(outside, link)
        except OSError:
            self.skipTest("file symlink is unavailable")
        with self.assertRaises(PbzError) as caught:
            memscrub.run(self.ws, None, self.notes, show="leak.md", human=True)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn("CANARY-OUTSIDE", str(caught.exception))

    def test_hardlinked_private_note_is_unsafe_path(self):
        other = os.path.join(self.tmp.name, "other.md")
        with open(other, "w", encoding="utf-8") as f:
            f.write("CANARY-LINK")
        linked = os.path.join(self.notes, "linked.md")
        os.link(other, linked)
        with self.assertRaises(PbzError) as caught:
            memscrub.run(self.ws, None, self.notes, show="linked.md", human=True)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn("CANARY-LINK", str(caught.exception))
        self.assertIn("CANARY-LINK", open(other, encoding="utf-8").read())

    def _maps(self) -> dict[str, bytes]:
        out = {}
        for root in (self.notes, self.ws.private):
            for cur, _dirs, files in os.walk(root):
                for name in files:
                    path = os.path.join(cur, name)
                    with open(path, "rb") as f:
                        out[path] = f.read()
        return out

    def test_swap_to_private_link_refuses_before_read(self):
        private = os.path.join(self.ws.private, "secret.md")
        with open(private, "wb") as f:
            f.write(b"CANARY-PRIVATE")
        original = open(self.a, "rb").read()
        real_open = open

        def run_kind(kind: str) -> None:
            done = {"n": 0}
            reads = {"n": 0}

            def watch(path, *args, **kwargs):
                mode = args[0] if args else kwargs.get("mode", "r")
                if not done["n"] and os.path.abspath(str(path)) == os.path.abspath(self.a) and "rb" in str(mode):
                    done["n"] = 1
                    os.remove(path)
                    try:
                        if kind == "hardlink":
                            os.link(private, path)
                        else:
                            os.symlink(private, path)
                    except OSError:
                        with real_open(path, "wb") as restored:
                            restored.write(original)
                        raise
                    handle = real_open(path, "rb")
                    real_read = handle.read

                    def counting(*a, **k):
                        reads["n"] += 1
                        return real_read(*a, **k)

                    handle.read = counting
                    return handle
                return real_open(path, *args, **kwargs)

            before_private = open(private, "rb").read()
            try:
                with patch("builtins.open", watch):
                    with self.assertRaises(PbzError) as caught:
                        memscrub.run(self.ws, None, self.notes, apply=True, human=True)
            except OSError:
                if kind == "symlink":
                    self.skipTest("file symlink is unavailable")
                raise
            finally:
                if os.path.lexists(self.a):
                    os.remove(self.a)
                with real_open(self.a, "wb") as restored:
                    restored.write(original)
            self.assertTrue(done["n"])
            self.assertEqual(reads["n"], 0)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertNotIn("CANARY-PRIVATE", str(caught.exception))
            self.assertFalse(os.path.isdir(os.path.join(self.ws.private, "memory-backup")))
            self.assertEqual(open(private, "rb").read(), before_private)
            self.assertEqual(open(self.a, "rb").read(), original)
            self.assertEqual(open(self.b, "rb").read(), b"spent 1000 hours\n")

        run_kind("hardlink")
        run_kind("symlink")

    def test_intermediate_junction_and_human_gate_order(self):
        outside = os.path.join(self.tmp.name, "outside")
        inner = os.path.join(outside, "inner")
        os.makedirs(inner)
        secret = os.path.join(inner, "note.md")
        with open(secret, "wb") as f:
            f.write(b"CANARY-JUNCTION")
        link = os.path.join(self.notes, "link")
        made = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside], capture_output=True)
        if made.returncode != 0:
            self.skipTest("creating a junction is unavailable")
        memory = os.path.join(link, "inner")
        listed = {"n": 0}
        real_list = os.listdir

        def watch(path):
            if os.path.abspath(str(path)) == os.path.abspath(memory):
                listed["n"] += 1
            return real_list(path)

        with patch("os.listdir", watch):
            with self.assertRaises(PbzError) as caught:
                memscrub.run(self.ws, None, memory, apply=True, human=False)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(listed["n"], 0)
        self.assertEqual(open(secret, "rb").read(), b"CANARY-JUNCTION")
        self.assertFalse(os.path.isdir(os.path.join(self.ws.private, "memory-backup")))
        other = os.path.join(self.ws.private, "early.md")
        with open(other, "wb") as f:
            f.write(b"CANARY-EARLY")
        os.link(other, os.path.join(self.notes, "early.md"))
        with self.assertRaises(PbzError) as caught:
            memscrub.run(self.ws, None, self.notes, apply=True, human=False)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn("CANARY-EARLY", str(caught.exception))

    def test_mcp_memscrub_counts_only_no_note_text(self):
        import json
        from contextlib import redirect_stdout
        from unittest.mock import patch
        note = os.path.join(self.notes, "spent.md")
        with open(note, "w", encoding="utf-8") as f:
            f.write("spent 1000 hours on CANARY-NOTE")

        def fake_run(argv, cwd=None, env=None, stdin=None, capture_output=False, timeout=None):
            out_buf = __import__("io").StringIO()
            with redirect_stdout(out_buf):
                code = cli.main(argv[3:])
            class Proc:
                returncode = code
                stdout = out_buf.getvalue().encode("utf-8")
                stderr = b""
            return Proc()

        with patch("pbz.mcp.subprocess.run", fake_run):
            out = __import__("pbz.mcp", fromlist=["mcp"]).call_tool(
                "memscrub", {"dir": self.notes, "workspace": self.ws.root}, self.ws.root, None)
        body = json.loads(out["content"][0]["text"])
        dumped = json.dumps(body)
        self.assertIn("files_changed", body)
        self.assertNotIn("scrubbed_text", body)
        self.assertNotIn("spent 1000 hours", dumped)
        self.assertNotIn("CANARY-NOTE", dumped)


if __name__ == "__main__":
    unittest.main()
