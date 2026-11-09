"""Round 18 Item 1: mirror readers go through open_mirror_file_readonly.
Run: python tests/test_mirror_reader_links.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz.common import PbzError, open_mirror_file_readonly  # noqa: E402

# Committed inventory. GATED call sites must use the helper. DEFERRED stays residual #8.
READER_INVENTORY = [
    "pbz/interactiontest.py:_load_json",
    "pbz/preview.py:_read",
    "pbz/validate.py:field_refs",
    "pbz/describe.py:field_usage",
    "pbz/describe.py:overview",
    "pbz/docs.py:_report.load",
    "pbz/handoff.py:page.json via read_checked",
    "pbz/recipes.py:read_checked",
    "pbz/evidence.py:read_checked",
    "pbz/mirror.py:rebase mine read_text_checked",
    "pbz/synth.py:ownership marker read_text_checked",
    "pbz/status.py:no mirror file read",
    "pbz/freshness.py:compare loads a spec, not mirror JSON",
    "pbz/filters.py:_load_page",
    "pbz/layout.py:_load",
    "pbz/style.py:page and visual JSON",
    "pbz/a11y.py:_require_json",
    "pbz/author.py:page and visual JSON",
    "pbz/smoke.py:_read_pbir",
    "pbz/images.py:_load",
    "pbz/repair.py:_load",
    "pbz/components.py:_load",
    "pbz/pbircheck.py:_load",
    "pbz/visuals.py:visual.json",
    "pbz/bookmarks.py:bookmark JSON",
    "pbz/suggest.py:report files",
    "pbz/usage.py:_report",
    "pbz/consolidate.py:_implicit_visuals",
]
GATED = {
    "interactiontest.py": "open_mirror_file_readonly",
    "preview.py": "open_mirror_file_readonly",
    "validate.py": "open_mirror_file_readonly",
    "describe.py": "read_text_checked",
    "docs.py": "read_text_checked",
    "filters.py": "read_text_checked",
    "layout.py": "read_definition_text",
    "style.py": "read_definition_text",
    "a11y.py": "read_definition_text",
    "author.py": "read_definition_text",
    "smoke.py": "read_definition_text",
    "images.py": "read_definition_text",
    "repair.py": "read_definition_text",
    "components.py": "read_definition_text",
    "pbircheck.py": "read_definition_text",
    "visuals.py": "read_definition_text",
    "bookmarks.py": "read_definition_text",
    "suggest.py": "read_definition_text",
    "usage.py": "read_definition_text",
    "consolidate.py": "read_definition_text",
}


def _reader_bypasses(text: str) -> list[str]:
    """Unchecked definition reads. Binary opens and marked package files are allowed."""
    bare = text.replace("read_text_checked(", "").replace("read_definition_text(", "")
    hits = []
    if "read_text(" in bare or ".read_text(" in bare or ".read_bytes(" in bare:
        hits.append("read_text")
    if "Path.read_text" in bare or "Path.read_bytes" in bare:
        hits.append("Path.read")
    for line in bare.splitlines():
        if "open(" not in line or "pbz-allow-open" in line:
            continue
        if "'rb'" in line or '"rb"' in line or "'wb'" in line or '"wb"' in line:
            continue
        hits.append(line.strip())
    return hits


class MirrorReaderLinks(unittest.TestCase):
    def test_inventory_is_committed(self):
        self.assertGreaterEqual(len(READER_INVENTORY), 3)

    def test_regular_file_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page.json")
            with open(path, "wb") as handle:
                handle.write(b'{"id":"p"}')
            self.assertEqual(open_mirror_file_readonly(path, mirror_root=tmp), b'{"id":"p"}')

    def test_hardlink_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "a.json")
            link = os.path.join(tmp, "b.json")
            with open(src, "wb") as handle:
                handle.write(b"PBZ_CANARY_LINK")
            try:
                os.link(src, link)
            except OSError:
                self.skipTest("hardlinks unavailable")
            with self.assertRaises(PbzError) as caught:
                open_mirror_file_readonly(link, mirror_root=tmp)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertNotIn("PBZ_CANARY_LINK", str(caught.exception))

    def test_toctou_swap_refused(self):
        from pbz import common
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page.json")
            with open(path, "wb") as handle:
                handle.write(b'{"id":"p"}')

            def swap(_path):
                os.remove(_path)
                with open(_path, "wb") as handle:
                    handle.write(b"PBZ_CANARY_SWAP")

            common._read_checked_hook = swap
            try:
                with self.assertRaises(PbzError) as caught:
                    open_mirror_file_readonly(path, mirror_root=tmp)
            finally:
                common._read_checked_hook = None
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertNotIn("PBZ_CANARY_SWAP", str(caught.exception))

    def test_gated_sources_do_not_raw_open_mirror_json(self):
        root = os.path.join(SKILL, "pbz")
        for name, needle in GATED.items():
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn(needle, text, name)
            self.assertNotIn("Path.read_text", text, name)
            self.assertNotIn("Path.read_bytes", text, name)
            if name in ("usage.py", "consolidate.py"):
                self.assertIn("read_definition_text(", text, name)
            else:
                self.assertFalse(_reader_bypasses(text), name)
        import inspect
        from pbz import consolidate, evidence, handoff, mirror, recipes, synth, usage
        scanned = [
            usage._report,
            consolidate._implicit_visuals,
            mirror._read_mine,
            synth._verified_owned,
            handoff._checked_text,
            recipes._snapshot,
            evidence._read_log,
        ]
        for fn in scanned:
            source = inspect.getsource(fn)
            self.assertFalse(_reader_bypasses(source), fn.__name__)
            broken = source
            for helper in ("read_definition_text(", "read_text_checked(", "read_checked("):
                if helper in source:
                    broken = source.replace(helper, "read_text(")
                    break
            self.assertNotEqual(broken, source, fn.__name__)
            self.assertTrue(_reader_bypasses(broken), fn.__name__)

    def test_scan_rejects_unchecked_reads(self):
        self.assertTrue(_reader_bypasses("read_text(path)"))
        self.assertTrue(_reader_bypasses("path.read_text()"))
        self.assertTrue(_reader_bypasses("path.read_bytes()"))
        self.assertTrue(_reader_bypasses("open(path, encoding='utf-8')"))
        self.assertFalse(_reader_bypasses("read_definition_text(path)"))
        self.assertFalse(_reader_bypasses("read_text_checked(path, root=root)"))
        self.assertFalse(_reader_bypasses("open(path, 'rb')"))
        self.assertFalse(_reader_bypasses("open(path, encoding='utf-8')  # pbz-allow-open: packaged rules file"))

    def test_preview_and_page_load_refuse_hardlink(self):
        from pathlib import Path
        from pbz import filters, preview
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "real.json")
            with open(src, "wb") as handle:
                handle.write(b'{"displayName":"PBZ_CANARY_PAGE"}')
            page_dir = os.path.join(tmp, "definition", "pages", "p1")
            os.makedirs(os.path.join(page_dir, "visuals", "v1"))
            link = os.path.join(page_dir, "page.json")
            try:
                os.link(src, link)
            except OSError:
                self.skipTest("hardlinks unavailable")
            with self.assertRaises(PbzError) as caught:
                preview._read(Path(link), Path(tmp))
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertNotIn("PBZ_CANARY_PAGE", str(caught.exception))
            visual = os.path.join(page_dir, "visuals", "v1", "visual.json")
            with self.assertRaises(PbzError) as caught:
                filters._load_page(type("W", (), {"mirror": tmp})(), visual)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertNotIn("PBZ_CANARY_PAGE", str(caught.exception))

    def test_definition_readers_refuse_hardlink_junction_and_symlink(self):
        import subprocess
        from pbz import a11y, components, images, layout, pbircheck, repair, smoke
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "pbz-workspace")
            page = os.path.join(root, "Demo.Report", "definition", "pages", "p1")
            os.makedirs(page)
            src = os.path.join(tmp, "real.json")
            with open(src, "w", encoding="utf-8") as handle:
                handle.write('{"displayName":"PBZ_CANARY_PAGE"}')
            link = os.path.join(page, "page.json")
            try:
                os.link(src, link)
            except OSError:
                self.skipTest("hardlinks unavailable")
            ws = type("W", (), {"mirror": root})()
            project = type("P", (), {"ws": ws, "mirror_report": os.path.join(root, "Demo.Report")})()
            readers = [
                lambda: layout._load(link),
                lambda: images._load(link),
                lambda: pbircheck._load(link),
                lambda: repair._load(ws, link),
                lambda: components._load(ws, link),
                lambda: smoke._read_pbir(link, "page.json"),
                lambda: a11y._require_json(project, link),
            ]
            for reader in readers:
                with self.assertRaises(PbzError) as caught:
                    reader()
                self.assertEqual(caught.exception.code, "UNSAFE_PATH")
                self.assertNotIn("PBZ_CANARY_PAGE", str(caught.exception))
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            with open(os.path.join(outside, "page.json"), "w", encoding="utf-8") as handle:
                handle.write('{"displayName":"PBZ_CANARY_PAGE"}')
            junction = os.path.join(root, "Demo.Report", "definition", "pages", "j1")
            made = subprocess.run(["cmd", "/c", "mklink", "/J", junction, outside],
                                  capture_output=True, text=True)
            if made.returncode == 0:
                linked = os.path.join(junction, "page.json")
                junction_readers = [
                    lambda: layout._load(linked),
                    lambda: images._load(linked),
                    lambda: pbircheck._load(linked),
                    lambda: repair._load(ws, linked),
                    lambda: components._load(ws, linked),
                    lambda: smoke._read_pbir(linked, "page.json"),
                    lambda: a11y._require_json(project, linked),
                ]
                for reader in junction_readers:
                    with self.assertRaises(PbzError) as caught:
                        reader()
                    self.assertEqual(caught.exception.code, "UNSAFE_PATH")
                    self.assertNotIn("PBZ_CANARY_PAGE", str(caught.exception))
            nested = os.path.join(tmp, "nested-outside", "pages", "pbz-workspace")
            os.makedirs(nested)
            with open(os.path.join(nested, "page.json"), "w", encoding="utf-8") as handle:
                handle.write('{"displayName":"PBZ_CANARY_PAGE"}')
            pages = os.path.join(root, "Demo.Report", "definition", "linked-pages")
            made = subprocess.run(["cmd", "/c", "mklink", "/J", pages, os.path.dirname(nested)],
                                  capture_output=True, text=True)
            if made.returncode == 0:
                with self.assertRaises(PbzError) as caught:
                    layout._load(os.path.join(pages, "pbz-workspace", "page.json"))
                self.assertEqual(caught.exception.code, "UNSAFE_PATH")
                self.assertNotIn("PBZ_CANARY_PAGE", str(caught.exception))

    def test_definition_reader_refuses_symlink(self):
        from pbz import layout
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "pbz-workspace", "Demo.Report", "definition", "pages", "p1")
            os.makedirs(root)
            src = os.path.join(tmp, "real.json")
            with open(src, "w", encoding="utf-8") as handle:
                handle.write('{"displayName":"PBZ_CANARY_PAGE"}')
            sym = os.path.join(root, "page.json")
            try:
                os.symlink(src, sym)
            except OSError:
                self.skipTest("symlinks unavailable")
            with self.assertRaises(PbzError) as caught:
                layout._load(sym)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertNotIn("PBZ_CANARY_PAGE", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
