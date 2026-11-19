"""Push captures candidates before validation and keeps PARTIAL after a reload failure.
Run: python tests/test_push_boundary_honesty.py
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import cli, common, mcp, mirror, outcomes, tokens  # noqa: E402


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


class PushBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "ws")
        test_parity.build(root)
        os.environ["PBZ_PRIVATE_ROOT"] = os.path.join(self.tmp.name, "private")
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        self.ws = common.Workspace(root)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)

    def _touch_mirror(self, suffix):
        rel = next(rel for rel, _real in mirror.project_files(self.project) if suffix in rel)
        path = os.path.join(self.ws.mirror, *rel.split("/"))
        with open(path, "ab") as f:
            f.write(b"\n")
        return rel, path

    def test_change_during_validation_is_conflict(self):
        rel, _path = self._touch_mirror(".SemanticModel/")
        real = mirror._real_path(self.ws, rel)
        manifest = file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project)))

        def validate(ws, project, stage, tm):
            with open(real, "ab") as f:
                f.write(b"\n")
            with open(os.path.join(self.ws.mirror, *rel.split("/")), "ab") as f:
                f.write(b"x")
            return []

        with patch("pbz.mirror.validate.run_stage", validate), patch("pbz.mirror.desktop.find_instance", lambda p: None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "CONFLICT")
        self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])
        self.assertEqual(file_map(os.path.dirname(mirror.manifest_path(self.ws, self.project))), manifest)

    def test_addition_appearing_during_validation_is_not_adopted(self):
        rel = f"{self.project.name}.SemanticModel/definition/tables/Added.tmdl"
        path = os.path.join(self.ws.mirror, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("table Added\n")
        real = mirror._real_path(self.ws, rel)

        def validate(ws, project, stage, tm):
            os.makedirs(os.path.dirname(real), exist_ok=True)
            with open(real, "w", encoding="utf-8", newline="\n") as f:
                f.write("CANARY-ADDED")
            return []

        with patch("pbz.mirror.validate.run_stage", validate), patch("pbz.mirror.desktop.find_instance", lambda p: None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "CONFLICT")
        self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])
        self.assertEqual(open(real, encoding="utf-8").read(), "CANARY-ADDED")

    def _project_bytes(self):
        return {rel: Path(path).read_bytes() for rel, path in mirror.project_files(self.project)}

    def _sync_bytes(self):
        return {"manifest": Path(mirror.manifest_path(self.ws, self.project)).read_bytes(),
                "base": file_map(mirror.base_dir(self.ws, self.project))}

    def test_editor_save_between_conflict_check_and_staging_survives(self):
        rel, _ = self._touch_mirror(".SemanticModel/")
        before = self._project_bytes()
        manifest = self._sync_bytes()
        original = mirror._detokenize
        injected = b"EDITOR-SAVE-BEFORE-STAGING\n"

        def detokenize(*args):
            with open(mirror._real_path(self.ws, rel), "wb") as handle:
                handle.write(injected)
            return original(*args)

        with patch("pbz.mirror._detokenize", detokenize), \
                patch("pbz.mirror.validate.run_stage", return_value=[]), \
                patch("pbz.mirror.desktop.find_instance", return_value=None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "CONFLICT")
        self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])
        before[rel] = injected
        self.assertEqual(self._project_bytes(), before)
        self.assertEqual(self._sync_bytes(), manifest)

    def _recreated_deletion(self, late):
        rel = next(rel for rel, _ in mirror.project_files(self.project) if rel.endswith(".tmdl"))
        path = os.path.join(self.ws.mirror, *rel.split("/"))
        os.remove(path)
        self._touch_mirror(".Report/")  # Another push-owned write must also be undone.
        before = self._project_bytes()
        manifest = self._sync_bytes()
        injected = b"EDITOR-RECREATED-MIRROR\n"
        original = mirror.write_text
        original_hash = mirror._post_mirror_hash
        original_journal = mirror._journal_save
        original_remove = mirror.remove_file
        events = []

        def journal(directory, journal):
            result = original_journal(directory, journal)
            events.append(journal["state"])
            return result

        def remove(path, **kwargs):
            if kwargs.get("root") == self.ws.root:
                events.append("remove")
            return original_remove(path, **kwargs)

        def checked(*args, **kwargs):
            result = original_hash(*args, **kwargs)
            events.append("check")
            return result

        def recreate(*args, **kwargs):
            events.append("inject")
            with open(path, "wb") as handle:
                handle.write(injected)
            return []

        def write(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get("root") == self.ws.root:
                events.append("write")
                recreate()
            return result

        with patch("pbz.mirror.validate.run_stage", return_value=[]) if late else \
                patch("pbz.mirror.validate.run_stage", recreate), \
                patch("pbz.mirror.write_text", write), \
                patch("pbz.mirror._post_mirror_hash", checked), \
                patch("pbz.mirror._journal_save", journal), \
                patch("pbz.mirror.remove_file", remove), \
                patch("pbz.mirror.desktop.find_instance", return_value=None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "CONFLICT")
        self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])
        self.assertEqual(Path(path).read_bytes(), injected)
        self.assertEqual(self._project_bytes(), before)
        self.assertEqual(self._sync_bytes(), manifest)
        if late:
            first_write = events.index("write")
            applying = events.index("APPLYING")
            self.assertIn("check", events[applying + 1:first_write])
            self.assertEqual(events[first_write + 1], "inject")
            self.assertIn("check", events[first_write + 2:])
        else:
            self.assertNotIn("write", events)
            self.assertNotIn("remove", events)

    def test_deleted_mirror_recreated_during_staging(self):
        self._recreated_deletion(False)

    def test_deleted_mirror_recreated_after_last_prewrite_check(self):
        self._recreated_deletion(True)

    def test_editor_save_after_push_write_survives_and_other_writes_rollback(self):
        rel, _ = self._touch_mirror(".SemanticModel/")
        other_rel, _ = self._touch_mirror(".Report/")
        before = self._project_bytes()
        manifest = self._sync_bytes()
        writes = {}
        original = mirror.write_text
        injected = b"EDITOR-SAVE-AFTER-WRITE\n"

        def write(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if kwargs.get("root") == self.ws.root:
                writes[path] = Path(path).read_bytes()
            if path == mirror._real_path(self.ws, rel):
                with open(path, "wb") as handle:
                    handle.write(injected)
            return result

        with patch("pbz.mirror.write_text", write), \
                patch("pbz.mirror.validate.run_stage", return_value=[]), \
                patch("pbz.mirror.desktop.find_instance", return_value=None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "CONFLICT")
        self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])
        before[rel] = injected
        self.assertEqual(self._project_bytes(), before)
        self.assertEqual(self._sync_bytes(), manifest)
        other_real = mirror._real_path(self.ws, other_rel)
        self.assertIn(other_real, writes)
        self.assertNotEqual(writes[other_real], before[other_rel])

    def test_untouched_candidate_bytes_survive_staging_conflict(self):
        trigger, _ = self._touch_mirror(".SemanticModel/")
        candidate, candidate_path = self._touch_mirror(".Report/")
        before = self._project_bytes()
        sync = self._sync_bytes()
        tm = tokens.TokenMap(self.ws.private)
        text, bom = common.read_text(candidate_path)
        after, unknown = mirror._detokenize(candidate, text, tm)
        self.assertFalse(unknown)
        candidate_bytes = bom + after.encode("utf-8")
        garbage = b"EDITOR-TRIGGER\x00\xff\n"
        original = mirror._journal_save
        real_writes = []
        original_write = mirror.write_text

        def journal(directory, journal):
            result = original(directory, journal)
            if journal["state"] == "APPLYING":
                Path(mirror._real_path(self.ws, trigger)).write_bytes(garbage)
                Path(mirror._real_path(self.ws, candidate)).write_bytes(candidate_bytes)
            return result

        def write(path, *args, **kwargs):
            if kwargs.get("root") == self.ws.root:
                real_writes.append(path)
            return original_write(path, *args, **kwargs)

        with patch("pbz.mirror._journal_save", journal), \
                patch("pbz.mirror.write_text", write), \
                patch("pbz.mirror.validate.run_stage", return_value=[]), \
                patch("pbz.mirror.desktop.find_instance", return_value=None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "CONFLICT")
        self.assertEqual(out["project"], self.project.name)
        self.assertEqual(real_writes, [])
        before[trigger], before[candidate] = garbage, candidate_bytes
        self.assertEqual(self._project_bytes(), before)
        self.assertEqual(self._sync_bytes(), sync)
        self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])

    def test_restore_rechecks_applied_target_before_overwrite(self):
        rel = next(iter(self._project_bytes()))
        real = mirror._real_path(self.ws, rel)
        after = b"push-candidate"
        directory, journal = mirror._begin_transaction(
            self.ws, self.project, "restore-recheck", [("workspace", rel, common.sha(after))])
        Path(real).write_bytes(after)
        original_hash = mirror.file_sha
        reads = []

        def hash_file(path):
            if path == real:
                reads.append(path)
                if len(reads) == 2:
                    Path(real).write_bytes(b"third-party-during-restore")
            return original_hash(path)

        with patch("pbz.mirror.file_sha", hash_file):
            out = mirror._restore_transaction(
                self.ws, self.project, directory, journal, preserve_external=True,
                applied={("workspace", rel)})
        self.assertEqual(out, {"result": "ROLLED_BACK", "conflicts": [rel]})
        self.assertEqual(Path(real).read_bytes(), b"third-party-during-restore")
        self.assertEqual(mirror.pending_recovery(self.ws, self.project), [])

    def test_conflict_preserves_nonclean_restore_result(self):
        for late in (False, True):
            with self.subTest(late=late):
                rel, _ = self._touch_mirror(".SemanticModel/")
                real = mirror._real_path(self.ws, rel)
                before_real = Path(real).read_bytes()
                original_save, original_write = mirror._journal_save, mirror.write_text

                def journal(directory, journal):
                    result = original_save(directory, journal)
                    if not late and journal["state"] == "APPLYING":
                        Path(real).write_bytes(b"external")
                    return result

                def write(path, *args, **kwargs):
                    result = original_write(path, *args, **kwargs)
                    if late and path == real:
                        Path(real).write_bytes(b"external")
                    return result

                restore_result = {"result": "RECOVERY_REQUIRED", "conflicts": [rel], "detail": "retained"}
                with patch("pbz.mirror._journal_save", journal), \
                        patch("pbz.mirror.write_text", write), \
                        patch("pbz.mirror._restore_transaction", return_value=restore_result), \
                        patch("pbz.mirror.validate.run_stage", return_value=[]), \
                        patch("pbz.mirror.desktop.find_instance", return_value=None):
                    out = mirror.push(self.ws, self.project)
                self.assertEqual(out, {"verb": "push", "project": self.project.name, **restore_result})
                # This intentionally mocked restore leaves a pending transaction.
                # Finish it before the next subcase with the saved original bytes.
                directory = mirror._transaction_dir(self.ws, self.project)
                import shutil
                shutil.rmtree(directory)
                Path(real).write_bytes(before_real)

    def test_excluded_directory_addition_is_refused(self):
        folder = os.path.join(self.ws.mirror, self.project.name + ".SemanticModel", ".pbi")
        os.makedirs(folder)
        with open(os.path.join(folder, "localSettings.json"), "w", encoding="utf-8") as f:
            f.write('{"secret":"CANARY-PBI"}')
        real = os.path.join(self.project.model_dir, ".pbi", "localSettings.json")
        with patch("pbz.mirror.desktop.find_instance", lambda p: None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "REFUSED")
        self.assertFalse(os.path.exists(real))
        self.assertNotIn("CANARY-PBI", json.dumps(out))

    def test_reload_failure_is_partial_on_cli_and_mcp(self):
        def boom(project):
            raise RuntimeError("reload secret")

        patches = (
            patch("pbz.mirror.validate.run_stage", lambda *a, **k: []),
            patch("pbz.mirror.desktop.find_instance", lambda p: {"unsaved": False}),
            patch("pbz.mirror.desktop.reload", boom),
        )
        for item in patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in patches])

        self._touch_mirror(".Report/")
        out = mirror.push(self.ws, self.project, reload=True)
        env, rc = outcomes.envelope(out)
        self.assertEqual(out["result"], "PUSHED")
        self.assertNotEqual(out.get("reload"), "RELOADED")
        self.assertEqual(rc, 5)
        self.assertEqual(env["outcome"]["status"], "PARTIAL")

        self._touch_mirror(".Report/")
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli_rc = cli.main(["--workspace", self.ws.root, "--project", "Parity", "push", "--reload"])
        body = json.loads(buf.getvalue())
        self.assertEqual(cli_rc, 5)
        self.assertEqual(body["outcome"]["exit_code"], 5)
        self.assertEqual(body["result"], "PUSHED")

        self._touch_mirror(".Report/")

        def fake_run(argv, **kwargs):
            out_buf = io.StringIO()
            with redirect_stdout(out_buf):
                code = cli.main(argv[3:])

            class Proc:
                returncode = code
                stdout = out_buf.getvalue().encode("utf-8")
                stderr = b""
            return Proc()

        with patch("pbz.mcp.subprocess.run", fake_run):
            mcp_out = mcp.call_tool("push", {"reload": True, "workspace": self.ws.root, "project": "Parity"}, self.ws.root, None)
        mcp_body = json.loads(mcp_out["content"][0]["text"])
        self.assertTrue(mcp_out["isError"])
        self.assertEqual(mcp_body["outcome"]["exit_code"], 5)
        self.assertEqual(mcp_body["result"], "PUSHED")
        self.assertNotIn("reload secret", buf.getvalue() + mcp_out["content"][0]["text"])

    def test_definition_junction_is_not_walked(self):
        outside = os.path.join(self.tmp.name, "outside-def")
        os.makedirs(outside)
        with open(os.path.join(outside, "CANARY-NAME.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        link = os.path.join(self.ws.mirror, self.project.name + ".Report", "definition", "outside")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        try:
            made = subprocess.run(["cmd", "/c", "mklink", "/J", link, outside], capture_output=True)
        except OSError:
            self.skipTest("creating a junction is unavailable")
        if made.returncode != 0:
            self.skipTest("creating a junction is unavailable")
        with patch("pbz.mirror.desktop.find_instance", lambda p: None):
            out = mirror.preflight(self.ws, self.project)
        rel = self.project.name + ".Report/definition/outside/"
        self.assertEqual(out["files"], [rel])
        self.assertNotIn("CANARY-NAME", json.dumps(out))

    def test_cross_project_alias_is_refused(self):
        other = os.path.join(self.ws.mirror, "Other.Report", "definition")
        os.makedirs(other)
        canary = os.path.join(other, "CANARY-OTHER.json")
        with open(canary, "w", encoding="utf-8") as f:
            f.write('{"secret": true}')
        target = os.path.join(
            self.ws.mirror, self.project.name + ".Report", "definition", "pages", "linked.json")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        try:
            os.symlink(canary, target)
        except OSError:
            self.skipTest("file symlink is unavailable")
        man = mirror.load_manifest(self.ws, self.project)
        rel = f"{self.project.name}.Report/definition/pages/linked.json"
        man.setdefault("files", {})[rel] = {"real": None, "mirror": "0" * 64}
        mirror.save_manifest(self.ws, self.project, man)
        with patch("pbz.mirror.desktop.find_instance", lambda p: None):
            out = mirror.push(self.ws, self.project)
        self.assertEqual(out["result"], "REFUSED")
        self.assertIn(rel, out.get("files", []))
        self.assertNotIn("CANARY-OTHER", json.dumps(out))
        # Item R-2: the mirror text collector now refuses the whole collection on an
        # unsafe in-scope entry instead of silently omitting it, so no text is served.
        with self.assertRaises(common.PbzError) as caught:
            mirror.mirror_texts(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn("CANARY-OTHER", str(caught.exception))

    def test_cross_project_directory_junction_is_not_walked(self):
        other = os.path.join(self.ws.mirror, "Other.Report", "definition", "secret")
        os.makedirs(other)
        with open(os.path.join(other, "CANARY-NAME.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        link = os.path.join(self.ws.mirror, self.project.name + ".Report", "definition", "outside")
        os.makedirs(os.path.dirname(link), exist_ok=True)
        try:
            made = subprocess.run(["cmd", "/c", "mklink", "/J", link, other], capture_output=True)
        except OSError:
            self.skipTest("creating a junction is unavailable")
        if made.returncode != 0:
            self.skipTest("creating a junction is unavailable")
        with patch("pbz.mirror.desktop.find_instance", lambda p: None):
            pre = mirror.preflight(self.ws, self.project)
            dry = mirror.push(self.ws, self.project, dry_run=True)
            out = mirror.push(self.ws, self.project)
        rel = self.project.name + ".Report/definition/outside/"
        for result in (pre, dry, out):
            self.assertEqual(result.get("files"), [rel])
            self.assertNotIn("CANARY-NAME", json.dumps(result))

    def test_project_report_root_alias_is_refused(self):
        other = os.path.join(self.ws.mirror, "Other.Report")
        os.makedirs(os.path.join(other, "definition"))
        with open(os.path.join(other, "definition", "CANARY-NAME.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        report = os.path.join(self.ws.mirror, self.project.name + ".Report")
        held = report + ".held"
        os.rename(report, held)
        try:
            made = subprocess.run(["cmd", "/c", "mklink", "/J", report, other], capture_output=True)
        except OSError:
            os.rename(held, report)
            self.skipTest("creating a junction is unavailable")
        if made.returncode != 0:
            os.rename(held, report)
            self.skipTest("creating a junction is unavailable")
        try:
            with patch("pbz.mirror.desktop.find_instance", lambda p: None):
                pre = mirror.preflight(self.ws, self.project)
                dry = mirror.push(self.ws, self.project, dry_run=True)
                out = mirror.push(self.ws, self.project)
            rel = self.project.name + ".Report/"
            for result in (pre, dry, out):
                self.assertEqual(result.get("result"), "REFUSED")
                self.assertEqual(result.get("files"), [rel])
                self.assertNotIn("CANARY-NAME", json.dumps(result))
            self.assertNotIn(
                f"{self.project.name}.Report/definition/CANARY-NAME.json",
                mirror.mirror_texts(self.ws, self.project))
        finally:
            os.rmdir(report)
            os.rename(held, report)


if __name__ == "__main__":
    unittest.main()
