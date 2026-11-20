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

    def test_post_open_path_swap_refused(self):
        import builtins
        from unittest.mock import patch
        from pbz import common
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "page.json")
            sibling = os.path.join(tmp, "replacement.json")
            with open(path, "wb") as handle:
                handle.write(b"original")
            with open(sibling, "wb") as handle:
                handle.write(b"POST_OPEN_SWAP")
            self.assertNotEqual(os.lstat(path).st_ino, os.lstat(sibling).st_ino)
            real_open, real_lstat = builtins.open, os.lstat
            opened, swapped = False, False
            def track_open(name, *args, **kwargs):
                nonlocal opened
                handle = real_open(name, *args, **kwargs)
                if name == path:
                    opened = True
                return handle
            def swap_after_open(name, *args, **kwargs):
                nonlocal swapped
                if name == path and opened and not swapped:
                    os.replace(sibling, path)
                    swapped = True
                return real_lstat(name, *args, **kwargs)
            with patch.object(builtins, "open", track_open), patch.object(os, "lstat", swap_after_open):
                with self.assertRaises(PbzError) as caught:
                    common.read_checked(path, root=tmp)
            self.assertTrue(swapped)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")

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

    def _refuses(self, call, *, private_unchanged=True, code="REFUSED"):
        from pbz import mirror
        from test_pull_refusal import file_map
        before = (file_map(self.ws.mirror), file_map(self.ws.private))
        history = file_map(mirror.history_dir(self.ws, self.project))
        with self.assertRaises(PbzError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)
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
        fresh = os.path.join(os.path.dirname(self.path), "never-snapshotted.json")
        with open(fresh, "w") as handle:
            handle.write('{"note":"UNIQUE_UNSNAPSHOTTED_INERT"}')
        os.link(fresh, os.path.join(self.tmp.name, "fresh-alias"))
        self._refuses(lambda: mirror.snapshot(self.ws, self.project, "unsafe"))

    def test_current_collector_refuses_ambiguous_file(self):
        from pbz import mirror
        self._ambiguous()
        self._refuses(lambda: mirror.mirror_texts(self.ws, self.project))

    def test_mirror_changes_default_refuses_ambiguous_file(self):
        from pbz import mirror
        rel = self._ambiguous()
        man = {"files": {rel: {"mirror": "old"}}}
        from unittest.mock import patch
        with patch.object(mirror, "mirror_texts", return_value={}):
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
            self._refuses(lambda: findreplace.replace(self.ws, self.project, "KNOWN_INERT_HIT", "other", apply=True))

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
            from _junction import make_junction
            make_junction(self, self.project.mirror_report, held)
        self._refuses(lambda: mirror.snapshot(self.ws, self.project, "alias"), code="UNSAFE_PATH")

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
            with self.assertRaises(PbzError) as caught:
                mirror.snapshot(self.ws, self.project, "swap")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(file_map(self.ws.private), before)
        def broken_walk(*args, **kwargs):
            kwargs["onerror"](PermissionError("inert"))
            return iter(())
        with patch.object(mirror.os, "walk", broken_walk):
            with self.assertRaises(PbzError) as caught:
                mirror.snapshot(self.ws, self.project, "walk")
        self.assertEqual(caught.exception.code, "REFUSED")
        for data in file_map(mirror.history_dir(self.ws, self.project)).values():
            self.assertNotIn(b"SWAPPED_INERT", data)
        self.assertEqual(file_map(self.ws.private), before)

    def test_invalid_utf8_refuses_without_content(self):
        from pbz import mirror
        with open(self.path, "wb") as handle:
            handle.write(b'KNOWN_INERT_HIT\xff')
        self._refuses(lambda: mirror.mirror_texts(self.ws, self.project))

    def test_replace_refuses_stale_collection(self):
        from pbz import common, findreplace, mirror
        from unittest.mock import patch
        rel = os.path.relpath(self.path, self.ws.mirror).replace("\\", "/")
        newer = b'{"note":"NEWER_INERT"}'
        common.replace_bytes(self.path, newer, root=self.ws.mirror)
        with patch.object(mirror, "mirror_texts", return_value={rel: 'KNOWN_INERT_HIT'}):
            self._refuses(lambda: findreplace.replace(self.ws, self.project, "KNOWN_INERT_HIT", "other", apply=True),
                          code="RECOVERY_REQUIRED")
        self.assertEqual(read_checked(self.path, root=self.ws.mirror), newer)

    def test_replace_expected_bytes_cover_bom_and_late_change(self):
        from pbz import common, findreplace
        from unittest.mock import patch
        old = b'\xef\xbb\xbf{"note":"KNOWN_INERT_HIT"}\r\n'
        newer = b'\xef\xbb\xbf{"note":"NEWER_INERT"}\r\n'
        common.replace_bytes(self.path, old, root=self.ws.mirror)
        commit = common.commit_bytes
        calls = []
        def change_before_snapshot(ops, **kwargs):
            self.assertEqual(kwargs["expected"][self.path], old)
            calls.append(ops)
            common.replace_bytes(self.path, newer, root=self.ws.mirror)
            return commit(ops, **kwargs)
        with patch.object(findreplace, "commit_bytes", change_before_snapshot):
            with self.assertRaises(PbzError) as caught:
                findreplace.replace(self.ws, self.project, "KNOWN_INERT_HIT", "other", apply=True)
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        self.assertEqual(len(calls), 1)
        self.assertEqual(read_checked(self.path, root=self.ws.mirror), newer)

    def test_merge3_checked_private_base_refuses(self):
        from pbz import common, mirror
        rel = os.path.relpath(self.path, self.ws.mirror).replace("\\", "/")
        base = os.path.join(mirror.base_dir(self.ws, self.project), *rel.split("/"))
        os.makedirs(os.path.dirname(base), exist_ok=True)
        common.replace_bytes(base, b"KNOWN_INERT_HIT", root=self.ws.private)
        os.link(base, os.path.join(self.tmp.name, "base-alias"))
        self._refuses(lambda: mirror._merge3(self.ws, self.project, rel, "other"))

    def test_expected_snapshot_refuses_all_writes_before_apply(self):
        from pbz import common
        other = os.path.join(os.path.dirname(self.path), "other.json")
        common.replace_bytes(other, b"newer", root=self.ws.mirror)
        before = read_checked(self.path, root=self.ws.mirror)
        with self.assertRaises(PbzError) as caught:
            common.commit_bytes([("write_bytes", self.path, b"changed"), ("write_bytes", other, b"changed")],
                                root=self.ws.mirror, expected={self.path: before, other: b"stale"})
        self.assertEqual(caught.exception.code, "RECOVERY_REQUIRED")
        self.assertEqual(read_checked(self.path, root=self.ws.mirror), before)
        self.assertEqual(read_checked(other, root=self.ws.mirror), b"newer")

    def test_enumeration_errors_are_project_scoped(self):
        from pbz import mirror
        from unittest.mock import patch
        def broken_walk(path, **kwargs):
            kwargs["onerror"](PermissionError(13, "inert", path))
            return iter(())
        other = os.path.join(self.ws.mirror, "Other.SemanticModel", "definition")
        own = os.path.join(self.project.mirror_model, "definition")
        def walk_error(path):
            return lambda *args, **kwargs: broken_walk(path, **kwargs)
        with patch.object(mirror.os, "walk", walk_error(other)):
            self.assertEqual(mirror.excluded_mirror_additions(self.ws, self.project), [])
        with patch.object(mirror.os, "walk", walk_error(own)):
            with self.assertRaises(PbzError) as caught:
                mirror.excluded_mirror_additions(self.ws, self.project)
        self.assertEqual(caught.exception.code, "REFUSED")

    def test_enumeration_errors_at_mirror_root_refuse(self):
        from pbz import mirror
        from unittest.mock import patch
        for filename in (self.ws.mirror, None):
            with self.subTest(filename=filename):
                def broken_walk(*args, **kwargs):
                    kwargs["onerror"](OSError(13, "inert", filename))
                    return iter(())
                with patch.object(mirror.os, "walk", broken_walk):
                    with self.assertRaises(PbzError) as caught:
                        mirror.excluded_mirror_additions(self.ws, self.project)
                self.assertEqual(caught.exception.code, "REFUSED")

    def _relate_reread_refuses(self, action):
        from pbz import relate
        from unittest.mock import patch
        loaded = relate._model(self.project)
        self.assertTrue(loaded[1].relationships)
        path = os.path.join(loaded[0], "relationships.tmdl")
        alias = os.path.join(self.tmp.name, "relationships-alias")
        # Preserve parseable bytes, but make the on-disk file unsafe after model load.
        with open(path, "rb") as handle:
            data = handle.read()
        with open(alias, "wb") as handle:
            handle.write(data)
        os.remove(path)
        os.link(alias, path)
        self.assertEqual(os.lstat(path).st_nlink, 2)
        calls = {
            "add": lambda: relate.add(self.ws, self.project, "Sales[Amount]", "Date[Date]", dry_run=True),
            "remove": lambda: relate.remove(self.ws, self.project, "Sales[CustomerKey]", "Customer[CustomerKey]",
                                             dry_run=True),
            "set": lambda: relate.set_(self.ws, self.project, "Sales[CustomerKey]", "Customer[CustomerKey]",
                                      both=True, dry_run=True),
        }
        with patch.object(relate, "_model", return_value=loaded):
            with self.assertRaises(PbzError) as caught:
                calls[action]()
        self.assertEqual(caught.exception.code, "REFUSED")

    def test_relate_add_reread_refuses_hardlink(self):
        self._relate_reread_refuses("add")

    def test_relate_remove_reread_refuses_hardlink(self):
        self._relate_reread_refuses("remove")

    def test_relate_set_reread_refuses_hardlink(self):
        self._relate_reread_refuses("set")

    def test_model_enumeration_default_and_strict(self):
        from pbz import tmdl
        from unittest.mock import patch
        missing = os.path.join(self.project.mirror_model, "missing")
        model = tmdl.load_model(missing)
        self.assertFalse(model.tables)
        self.assertFalse(model.docs)
        with self.assertRaises(PbzError) as caught:
            tmdl.load_model(missing, root=self.ws.mirror)
        self.assertEqual(caught.exception.code, "REFUSED")
        def broken_walk(path, **kwargs):
            kwargs["onerror"](PermissionError(13, "INERT_ERROR_PAYLOAD", path))
            return iter(())
        with patch.object(tmdl.os, "walk", broken_walk):
            self.assertFalse(tmdl.load_model(self.project.mirror_model).docs)
            with self.assertRaises(PbzError) as caught:
                tmdl.load_model(self.project.mirror_model, root=self.ws.mirror)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, os.path.basename(self.project.mirror_model))

    def test_directory_model_loads_are_rooted_or_explicitly_allowed(self):
        import ast
        # Real-side reads intentionally operate outside the sanitized mirror.
        allowlist = {
            "mirror.py": "real_model reads the real project; pull uses prepared texts",
            "desktop.py": "Desktop compares expected tables against the real project",
            "memscrub.py": "classifies real model literals before sanitizing memory",
            "synth.py": "synthesis loads supplied model texts",
            "changes.py": "change comparison loads supplied document texts",
            "checks.py": "model_literals and validation compare the real model",
            "consumers.py": "catalog scanning reads external consumer models",
        }
        # Pin functions too, so adding a mirror read to an allowed file cannot bypass the check.
        allowed_functions = {"mirror.py": {"real_model"}, "desktop.py": {"require_port"},
                             "memscrub.py": {"run"}, "checks.py": {"model_literals", "run_checks"},
                             "consumers.py": {"_parse_model", "_binding_only"}}
        found = []
        for name in sorted(os.listdir(os.path.join(SKILL, "pbz"))):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(SKILL, "pbz", name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "tmdl"
                        and node.func.attr == "load_model"):
                    continue
                if len(node.args) > 1 or any(k.arg in ("root", "texts") for k in node.keywords):
                    continue
                functions = [f for f in ast.walk(tree) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))
                             and f.lineno <= node.lineno <= f.end_lineno]
                fn = max(functions, key=lambda f: f.lineno).name if functions else "<module>"
                found.append((name, fn))
                self.assertIn(name, allowlist, (name, node.lineno))
                self.assertIn(fn, allowed_functions.get(name, set()), (name, fn, node.lineno))
        self.assertCountEqual(found, [(name, fn) for name, functions in allowed_functions.items()
                                      for fn in functions])


if __name__ == "__main__":
    unittest.main()
