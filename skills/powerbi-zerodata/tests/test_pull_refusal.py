"""Pull refuses a late decode before any write. Run: python tests/test_pull_refusal.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, mirror  # noqa: E402


def file_map(root):
    out = {}
    if not os.path.isdir(root):
        return out
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            path = os.path.join(dirpath, name)
            rel = os.path.relpath(path, root).replace("\\", "/")
            with open(path, "rb") as f:
                out[rel] = f.read()
    return out


def no_tmp(root):
    for dirpath, _dirs, names in os.walk(root):
        for name in names:
            if name.endswith(".pbz-tmp"):
                return False
    return True


class PullRefusalTests(unittest.TestCase):
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

    def test_bad_utf8_later_file_changes_nothing(self):
        files = mirror.project_files(self.project)
        rel, real = files[-1]
        with open(real, "wb") as f:
            f.write(b"\xff\xfe not utf-8")
        before_m = file_map(self.ws.mirror)
        before_man = file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))
        with self.assertRaises(common.PbzError) as caught:
            mirror.pull(self.ws, self.project, force=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertIn(rel, caught.exception.detail)
        self.assertNotIn("utf", caught.exception.detail.lower())
        self.assertEqual(file_map(self.ws.mirror), before_m)
        self.assertEqual(file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))), before_man)
        self.assertTrue(no_tmp(self.ws.root))
        self.assertTrue(no_tmp(self.ws.private))

    def test_late_rebase_base_decode_is_closed(self):
        files = mirror.project_files(self.project)
        rel, real = files[-1]
        mirror_path = os.path.join(self.ws.mirror, *rel.split("/"))
        base = os.path.join(mirror.base_dir(self.ws, self.project), *rel.split("/"))
        with open(mirror_path, "ab") as f:
            f.write(b"\n")
        text, _bom = common.read_text(real)
        with open(real, "wb") as f:
            f.write(text.encode("utf-8") + b"\n")
        with open(base, "wb") as f:
            f.write(b"\xff rebase-secret")
        before = (file_map(self.ws.mirror), file_map(mirror.base_dir(self.ws, self.project)),
                  file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))))
        with self.assertRaises(common.PbzError) as caught:
            mirror.pull(self.ws, self.project, rebase=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, rel)
        self.assertNotIn("rebase-secret", str(caught.exception))
        self.assertEqual((file_map(self.ws.mirror), file_map(mirror.base_dir(self.ws, self.project)),
                          file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))), before)
        self.assertTrue(no_tmp(self.ws.root))
        self.assertTrue(no_tmp(self.ws.private))

    def test_malformed_pbir_refuses_before_publish(self):
        from pbz import tokens
        files = mirror.project_files(self.project)
        rel, real = next((rel, real) for rel, real in files if rel.lower().endswith("/visual.json"))
        with open(real, "w", encoding="utf-8", newline="\n") as f:
            f.write('{"name": "CANARY-SECRET",}')
        before_m = file_map(self.ws.mirror)
        before_base = file_map(mirror.base_dir(self.ws, self.project))
        before_man = file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))
        before_tm = file_map(os.path.dirname(tokens.TokenMap(self.ws.private).path))
        with self.assertRaises(common.PbzError) as caught:
            mirror.pull(self.ws, self.project, force=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, rel)
        self.assertNotIn("CANARY-SECRET", str(caught.exception))
        self.assertEqual(file_map(self.ws.mirror), before_m)
        self.assertEqual(file_map(mirror.base_dir(self.ws, self.project)), before_base)
        self.assertEqual(file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))), before_man)
        self.assertEqual(file_map(os.path.dirname(tokens.TokenMap(self.ws.private).path)), before_tm)
        blob = b"".join(file_map(self.ws.mirror).values())
        self.assertNotIn(b"CANARY-SECRET", blob)

    def test_malformed_visual_cli_envelope_is_refused(self):
        import io
        import json
        from contextlib import redirect_stdout
        from pbz import cli
        files = mirror.project_files(self.project)
        rel, real = next((rel, real) for rel, real in files if rel.lower().endswith("/visual.json"))
        with open(real, "w", encoding="utf-8", newline="\n") as f:
            f.write('{"name": "CANARY-SECRET",}')
        buf = io.StringIO()
        private = os.path.join(self.tmp.name, "cli-private")
        os.makedirs(private, exist_ok=True)
        with redirect_stdout(buf):
            old = os.environ.get("PBZ_PRIVATE_ROOT")
            os.environ["PBZ_PRIVATE_ROOT"] = private
            try:
                rc = cli.main(["--workspace", self.ws.root, "pull", "--force"])
            finally:
                if old is None:
                    os.environ.pop("PBZ_PRIVATE_ROOT", None)
                else:
                    os.environ["PBZ_PRIVATE_ROOT"] = old
        text = buf.getvalue()
        body = json.loads(text)
        self.assertEqual(rc, 2)
        self.assertEqual(body["error"], "REFUSED")
        self.assertEqual(body["detail"], rel)
        self.assertNotEqual(body["error"], "INTERNAL_ERROR")
        self.assertNotEqual(body.get("result"), "PULLED")
        self.assertNotIn("CANARY-SECRET", text)

    def test_deep_nesting_refuses_without_echo(self):
        files = mirror.project_files(self.project)
        rel, real = next((rel, real) for rel, real in files if rel.lower().endswith("/page.json"))
        text = '{"a":' * 200 + '"CANARY-NEST"' + "}" * 200
        with open(real, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        before_m = file_map(self.ws.mirror)
        with self.assertRaises(common.PbzError) as caught:
            mirror.pull(self.ws, self.project, force=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotIn("CANARY-NEST", str(caught.exception))
        self.assertEqual(file_map(self.ws.mirror), before_m)

    def test_non_object_pbir_root_is_refused(self):
        from pbz import tokens
        files = mirror.project_files(self.project)
        rel, real = next(pair for pair in files if pair[0].lower().endswith(("/report.json", "/page.json", "/visual.json")))
        with open(real, "w", encoding="utf-8", newline="\n") as f:
            f.write("[918273, true]")
        before_m = file_map(self.ws.mirror)
        before_base = file_map(mirror.base_dir(self.ws, self.project))
        before_man = file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))
        before_tm = file_map(os.path.dirname(tokens.TokenMap(self.ws.private).path))
        with self.assertRaises(common.PbzError) as caught:
            mirror.pull(self.ws, self.project, force=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, rel)
        self.assertNotIn("918273", str(caught.exception))
        self.assertEqual(file_map(self.ws.mirror), before_m)
        self.assertEqual(file_map(mirror.base_dir(self.ws, self.project)), before_base)
        self.assertEqual(file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))), before_man)
        self.assertEqual(file_map(os.path.dirname(tokens.TokenMap(self.ws.private).path)), before_tm)

    def test_parent_junction_under_mirror_refuses_publish(self):
        import subprocess
        outside = os.path.join(self.tmp.name, "pull-outside")
        os.makedirs(outside)
        before_out = file_map(outside)
        report = os.path.join(self.ws.mirror, self.project.name + ".Report")
        definition = os.path.join(report, "definition")
        # Move definition aside and replace with a junction to outside.
        held = definition + ".held"
        os.rename(definition, held)
        try:
            made = subprocess.run(["cmd", "/c", "mklink", "/J", definition, outside], capture_output=True)
        except OSError:
            os.rename(held, definition)
            self.skipTest("creating a junction is unavailable")
        if made.returncode != 0:
            os.rename(held, definition)
            self.skipTest("creating a junction is unavailable")
        before_m = file_map(self.ws.mirror)
        before_base = file_map(mirror.base_dir(self.ws, self.project))
        before_man = file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))
        with self.assertRaises(common.PbzError) as caught:
            mirror.pull(self.ws, self.project, force=True)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(file_map(outside), before_out)
        self.assertEqual(file_map(self.ws.mirror), before_m)
        self.assertEqual(file_map(mirror.base_dir(self.ws, self.project)), before_base)
        self.assertEqual(file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))), before_man)
        self.assertTrue(no_tmp(outside))
        self.assertTrue(no_tmp(self.ws.mirror))
        # cleanup junction so TemporaryDirectory can remove the tree
        os.rmdir(definition)
        os.rename(held, definition)

    def test_stale_symlink_delete_preflight_keeps_maps(self):
        files = mirror.project_files(self.project)
        safe_rel, safe_real = next(
            (rel, real) for rel, real in files if rel.lower().endswith(".tmdl"))
        stale_rel, stale_real = next(
            (rel, real) for rel, real in files
            if rel != safe_rel and rel.lower().endswith((".json", ".tmdl")))
        outside = os.path.join(self.tmp.name, "stale-outside")
        os.makedirs(outside)
        secret = os.path.join(outside, "CANARY-STALE.bin")
        with open(secret, "wb") as f:
            f.write(b"CANARY-STALE")
        # Remove the real source so pull treats the mirror copy as stale.
        os.remove(stale_real)
        stale_mirror = os.path.join(self.ws.mirror, *stale_rel.split("/"))
        if os.path.exists(stale_mirror):
            os.remove(stale_mirror)
        try:
            os.symlink(secret, stale_mirror)
        except OSError:
            self.skipTest("file symlink is unavailable")
        with open(safe_real, "ab") as f:
            f.write(b"\n")
        before_m = file_map(self.ws.mirror)
        before_base = file_map(mirror.base_dir(self.ws, self.project))
        before_man = file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))
        before_out = file_map(outside)
        with self.assertRaises(common.PbzError) as caught:
            mirror.pull(self.ws, self.project, force=True)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(file_map(self.ws.mirror), before_m)
        self.assertEqual(file_map(mirror.base_dir(self.ws, self.project)), before_base)
        self.assertEqual(file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))), before_man)
        self.assertEqual(file_map(outside), before_out)
        self.assertTrue(no_tmp(self.ws.mirror))
        self.assertTrue(no_tmp(outside))

    def test_rebase_input_under_junction_is_refused_before_read(self):
        import subprocess
        from unittest.mock import patch
        files = mirror.project_files(self.project)
        rel, real = next((r, p) for r, p in files if r.lower().endswith(".tmdl"))
        # Edit the mirror so rebase wants to keep/merge this path, then change real.
        mpath = os.path.join(self.ws.mirror, *rel.split("/"))
        with open(mpath, "ab") as f:
            f.write(b"\n")
        with open(real, "ab") as f:
            f.write(b"\n")
        parent = os.path.dirname(mpath)
        outside = os.path.join(self.tmp.name, "rebase-out")
        os.makedirs(outside)
        # Canary at the basename rebase would open through the junctioned parent.
        secret = os.path.join(outside, os.path.basename(mpath))
        with open(secret, "wb") as f:
            f.write(b"CANARY-REBASE")
        secret_id = (os.lstat(secret).st_dev, os.lstat(secret).st_ino)
        held = parent + ".held"
        os.rename(parent, held)
        try:
            made = subprocess.run(["cmd", "/c", "mklink", "/J", parent, outside], capture_output=True)
        except OSError:
            os.rename(held, parent)
            self.skipTest("creating a junction is unavailable")
        if made.returncode != 0:
            os.rename(held, parent)
            self.skipTest("creating a junction is unavailable")
        external_opens = []
        external_reads = []
        real_open = open

        def watch(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            try:
                info = os.fstat(handle.fileno())
                if (info.st_dev, info.st_ino) == secret_id:
                    external_opens.append(str(path))
                    orig_read = handle.read

                    def tracked(*a, **k):
                        external_reads.append(b"read")
                        return orig_read(*a, **k)

                    handle.read = tracked
            except OSError:
                pass
            return handle

        # Positive control: opening the junctioned mirror path reaches the canary.
        with patch("builtins.open", watch):
            with open(mpath, "rb") as probe:
                probe.read()
        self.assertEqual(len(external_opens), 1)
        self.assertEqual(external_reads, [b"read"])
        external_opens.clear()
        external_reads.clear()

        before_m = file_map(self.ws.mirror)
        before_base = file_map(mirror.base_dir(self.ws, self.project))
        before_man = file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))
        before_out = file_map(outside)
        try:
            with patch("builtins.open", watch):
                with self.assertRaises(common.PbzError) as caught:
                    mirror.pull(self.ws, self.project, rebase=True)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertEqual(external_opens, [])
            self.assertEqual(external_reads, [])
            self.assertNotIn("CANARY-REBASE", str(caught.exception))
            self.assertEqual(file_map(self.ws.mirror), before_m)
            self.assertEqual(file_map(mirror.base_dir(self.ws, self.project)), before_base)
            self.assertEqual(file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))), before_man)
            self.assertEqual(file_map(outside), before_out)
            self.assertTrue(no_tmp(self.ws.mirror))
            self.assertTrue(no_tmp(outside))
        finally:
            os.rmdir(parent)
            os.rename(held, parent)

    def test_aliased_source_report_root_is_refused(self):
        import subprocess
        from pbz import tokens
        outside = os.path.join(self.tmp.name, "src-out")
        os.makedirs(os.path.join(outside, "definition"))
        canary = os.path.join(outside, "definition", "report.json")
        with open(canary, "w", encoding="utf-8") as f:
            f.write('{"name":"CANARY-SOURCE"}')
        report = self.project.report_dir
        held = report + ".held"
        os.rename(report, held)
        try:
            made = subprocess.run(["cmd", "/c", "mklink", "/J", report, outside], capture_output=True)
        except OSError:
            os.rename(held, report)
            self.skipTest("creating a junction is unavailable")
        if made.returncode != 0:
            os.rename(held, report)
            self.skipTest("creating a junction is unavailable")
        before_m = file_map(self.ws.mirror)
        before_base = file_map(mirror.base_dir(self.ws, self.project))
        before_man = file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))
        before_tm = file_map(os.path.dirname(tokens.TokenMap(self.ws.private).path))
        before_out = file_map(outside)
        try:
            with self.assertRaises(common.PbzError) as caught:
                mirror.pull(self.ws, self.project, force=True)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertNotIn("CANARY-SOURCE", str(caught.exception))
            self.assertEqual(file_map(self.ws.mirror), before_m)
            self.assertEqual(file_map(mirror.base_dir(self.ws, self.project)), before_base)
            self.assertEqual(file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))), before_man)
            self.assertEqual(file_map(os.path.dirname(tokens.TokenMap(self.ws.private).path)), before_tm)
            self.assertEqual(file_map(outside), before_out)
        finally:
            os.rmdir(report)
            os.rename(held, report)


    def test_tokenize_pbir_callers_stay_in_mirror(self):
        import pathlib
        from unittest.mock import patch
        from pbz import tokens
        root = pathlib.Path(__file__).resolve().parents[1] / "pbz"
        hits = []
        for path in sorted(root.glob("*.py")):
            if path.name == "tokens.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "tokenize_pbir(" in text:
                hits.append(path.name)
        self.assertEqual(hits, ["mirror.py"])
        mirror_text = (root / "mirror.py").read_text(encoding="utf-8")
        self.assertRegex(mirror_text, r"def _tokenize\(")
        self.assertIn("tokens.tokenize_pbir(", mirror_text)
        self.assertRegex(
            mirror_text,
            r"except PbzError as err:\s*\n\s*if err\.code == \"REFUSED\":\s*\n\s*raise PbzError\(\"REFUSED\", rel\)",
        )
        files = mirror.project_files(self.project)
        rel, _real = next(pair for pair in files if not pair[0].lower().endswith(".tmdl"))

        def boom(*_a, **_k):
            raise common.PbzError("REFUSED", "CANARY-SECRET-DETAIL")

        with patch.object(tokens, "tokenize_pbir", boom):
            with self.assertRaises(common.PbzError) as caught:
                mirror.pull(self.ws, self.project, force=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, rel)
        self.assertNotIn("CANARY-SECRET-DETAIL", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
