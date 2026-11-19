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

from pbz.common import PbzError, _contained, open_mirror_file_readonly, read_checked  # noqa: E402

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

    def test_contained_refuses_dotdot_through_link(self):
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            root = os.path.join(tmp, "root")
            outside = os.path.join(tmp, "outside")
            os.makedirs(root)
            os.makedirs(outside)
            with open(os.path.join(outside, "sibling.txt"), "wb") as handle:
                handle.write(b"PBZ_CANARY_SECRET")
            link = os.path.join(root, "link")
            made = subprocess.run(
                ["cmd", "/c", "mklink", "/J", link, outside],
                capture_output=True,
                text=True,
            )
            if made.returncode != 0:
                try:
                    os.symlink(outside, link, target_is_directory=True)
                except OSError:
                    self.skipTest("symlinks/junctions unavailable")
            trap = os.path.join(root, "link", "..", "sibling.txt")
            with self.assertRaises(PbzError) as caught:
                _contained(trap, root)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            with self.assertRaises(PbzError) as caught:
                read_checked(trap, root=root)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")

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


class CurrentMirrorCollectionTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, os.path.dirname(__file__))
        import test_parity
        from pbz import common, mirror
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "ws")
        test_parity.build(root)
        self.ws = common.Workspace(root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)
        self.path = os.path.join(self.project.mirror_report, "definition", "inert.json")
        with open(self.path, "w") as handle:
            handle.write('{"note":"KNOWN_INERT_HIT"}')

    def _ambiguous(self):
        from pbz import findreplace, mirror
        self.assertTrue(findreplace.find(self.ws, self.project, "KNOWN_INERT_HIT")["snippets"])
        self.assertEqual(findreplace.replace(self.ws, self.project, "KNOWN_INERT_HIT", "other")["text_hits"], 1)
        mirror.snapshot(self.ws, self.project, "safe-control")
        os.link(self.path, os.path.join(self.tmp.name, "alias"))
        return os.path.relpath(self.path, self.ws.mirror).replace("\\", "/")

    def _refuses(self, call, *, private_unchanged=True):
        from pbz import mirror
        from test_pull_refusal import file_map
        before = (file_map(self.ws.mirror), file_map(self.ws.private))
        history = file_map(mirror.history_dir(self.ws, self.project))
        with self.assertRaises(PbzError) as caught:
            call()
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIn("KNOWN_INERT_HIT", str(caught.exception))
        self.assertEqual(file_map(self.ws.mirror), before[0])
        self.assertEqual(file_map(mirror.history_dir(self.ws, self.project)), history)
        if private_unchanged:
            self.assertEqual(file_map(self.ws.private), before[1])

    def test_find_has_no_snippets_on_ambiguous_file(self):
        from pbz import findreplace
        self._ambiguous()
        self._refuses(lambda: findreplace.find(self.ws, self.project, "KNOWN_INERT_HIT"))

    def test_replace_has_no_text_hits_on_ambiguous_file(self):
        from pbz import findreplace
        self._ambiguous()
        self._refuses(lambda: findreplace.replace(self.ws, self.project, "KNOWN_INERT_HIT", "other"))

    def test_snapshot_publishes_no_blob_on_ambiguous_file(self):
        from pbz import mirror
        self._ambiguous()
        self._refuses(lambda: mirror.snapshot(self.ws, self.project, "unsafe"))

    def test_current_collector_refuses_ambiguous_file(self):
        from pbz import mirror
        self._ambiguous()
        self._refuses(lambda: mirror.mirror_texts(self.ws, self.project))

    def test_mirror_changes_default_refuses_ambiguous_file(self):
        from pbz import mirror
        rel = self._ambiguous()
        man = {"files": {rel: {"mirror": "old"}}}
        self._refuses(lambda: mirror.mirror_changes(self.ws, self.project, man))

    def test_excluded_additions_refuses_ambiguous_in_scope_file(self):
        from pbz import mirror
        self._ambiguous()
        self._refuses(lambda: mirror.excluded_mirror_additions(self.ws, self.project))

    def test_merge3_fallback_refuses_ambiguous_file(self):
        from pbz import mirror
        rel = self._ambiguous()
        self._refuses(lambda: mirror._merge3(self.ws, self.project, rel, "other"))

    def test_replace_bom_reread_refuses_ambiguous_file(self):
        from pbz import findreplace, mirror
        from unittest.mock import patch
        rel = self._ambiguous()
        # Force the collection to succeed, isolating the later BOM reread.
        with patch.object(mirror, "mirror_texts", return_value={rel: "KNOWN_INERT_HIT"}):
            self._refuses(lambda: findreplace.replace(self.ws, self.project, "KNOWN_INERT_HIT", "other", apply=True),
                          private_unchanged=False)  # The failed edit retains its durable rollback journal.

    def test_load_model_checked_root_and_real_literal_control(self):
        from pbz import checks, mirror, tmdl
        self._ambiguous()
        model_file = os.path.join(self.project.mirror_model, "definition", "inert.tmdl")
        os.link(self.path, model_file)
        self._refuses(lambda: tmdl.load_model(os.path.dirname(model_file), root=self.ws.mirror))
        self.assertTrue(mirror.real_model(self.project).tables)
        self.assertIn("Jane Doe", checks.model_literals(self.project))

    def test_aliased_base_refuses_and_policy_directory_stays_skipped(self):
        from pbz import mirror
        skipped = os.path.join(self.project.mirror_report, ".pbi")
        os.makedirs(skipped)
        external = os.path.join(self.tmp.name, "excluded-inert")
        with open(external, "wb") as handle:
            handle.write(b"\xff")
        os.link(external, os.path.join(skipped, "ignored.json"))
        # A policy exclusion remains skipped even if the excluded entry is unsafe.
        self.assertTrue(mirror.mirror_texts(self.ws, self.project))
        held = self.project.mirror_report + ".held"
        os.rename(self.project.mirror_report, held)
        try:
            os.symlink(held, self.project.mirror_report, target_is_directory=True)
        except OSError:
            self.skipTest("directory symlink is unavailable")
        self._refuses(lambda: mirror.snapshot(self.ws, self.project, "alias"))

    def test_collection_swap_and_walk_error_refuse(self):
        from pbz import common, mirror
        from unittest.mock import patch
        from test_pull_refusal import file_map
        before = file_map(self.ws.private)
        def swap(path):
            if path == self.path:
                os.rename(path, path + ".held")
                with open(path, "w") as handle:
                    handle.write("SWAPPED_INERT")
        with patch.object(common, "_read_checked_hook", swap):
            with self.assertRaises(PbzError):
                mirror.snapshot(self.ws, self.project, "swap")
        self.assertEqual(file_map(self.ws.private), before)
        def broken_walk(*args, **kwargs):
            kwargs["onerror"](PermissionError("inert"))
            return iter(())
        with patch.object(mirror.os, "walk", broken_walk):
            with self.assertRaises(PbzError):
                mirror.snapshot(self.ws, self.project, "walk")
        self.assertEqual(file_map(self.ws.private), before)


if __name__ == "__main__":
    unittest.main()
