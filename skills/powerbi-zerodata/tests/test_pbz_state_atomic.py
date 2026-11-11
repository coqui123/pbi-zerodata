"""Round 16: .pbz state paths use the workspace root, checked reads, and the shipped writer.

Run: python tests/test_pbz_state_atomic.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pbz.common import PbzError, Workspace, append_checked, read_checked  # noqa: E402
from pbz import audit, docs, handoff, memory, mirror  # noqa: E402


CANARY = b"CANARY-STATE-LINE\n"


class StateAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(self.tmp.name)
        open(os.path.join(self.tmp.name, "Canary.pbip"), "w", encoding="utf-8").close()
        self.project = self.ws.project()

    def _junction(self, link: str, target: str) -> None:
        from _junction import make_junction
        make_junction(self, link, target)

    def _outside(self) -> str:
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        return external.name

    def _project_map(self) -> dict:
        out = {}
        for cur, dirs, files in os.walk(self.tmp.name):
            dirs[:] = [d for d in dirs if d != ".pbz"]
            for name in files:
                path = os.path.join(cur, name)
                if os.path.isfile(path) and not os.path.islink(path):
                    out[path] = open(path, "rb").read()
        return out

    def test_junctioned_pbz_refuses_before_mutation(self):
        outside = self._outside()
        for name, body in (
            ("memory.json", CANARY),
            ("handoff.json", CANARY),
            ("push-log.jsonl", CANARY),
        ):
            with open(os.path.join(outside, name), "wb") as handle:
                handle.write(body)
        os.makedirs(os.path.join(outside, "audit"))
        with open(os.path.join(outside, "audit", "waivers.json"), "wb") as handle:
            handle.write(CANARY)
        self._junction(self.ws.pbz_dir, outside)
        before = self._project_map()
        spends = {"n": 0}
        engines = {"n": 0}

        def spend(*_a, **_k):
            spends["n"] += 1
            return True

        def engine(*_a, **_k):
            engines["n"] += 1
            return {"ok": True}

        with patch("pbz.checks.spend", spend), patch("pbz.memory.engine_call", engine), \
                patch("pbz.memory.desktop.require_port", return_value=1):
            with self.assertRaises(PbzError) as caught:
                memory.run(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(spends["n"], 0)
        self.assertEqual(engines["n"], 0)
        with self.assertRaises(PbzError) as caught:
            handoff.build(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        with self.assertRaises(PbzError) as caught:
            audit._load_json(self.ws, ("audit", "waivers.json"), {})
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        with self.assertRaises(PbzError) as caught:
            docs._changelog(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertNotIn(b"CANARY-STATE-LINE", str(caught.exception).encode())
        with self.assertRaises(PbzError) as caught:
            mirror.push(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(open(os.path.join(outside, "memory.json"), "rb").read(), CANARY)
        self.assertEqual(open(os.path.join(outside, "handoff.json"), "rb").read(), CANARY)
        self.assertEqual(open(os.path.join(outside, "push-log.jsonl"), "rb").read(), CANARY)
        self.assertEqual(open(os.path.join(outside, "audit", "waivers.json"), "rb").read(), CANARY)
        self.assertEqual(list(Path(outside).rglob("*.pbz-tmp")), [])
        self.assertEqual(list(Path(outside).rglob("*.tmp")), [])
        self.assertEqual(self._project_map(), before)

    def test_memory_leaf_symlink_is_refused_before_budget(self):
        outside = self._outside()
        secret = os.path.join(outside, "memory.json")
        with open(secret, "wb") as handle:
            handle.write(CANARY)
        os.makedirs(self.ws.pbz_dir)
        try:
            os.symlink(secret, os.path.join(self.ws.pbz_dir, "memory.json"))
        except OSError:
            self.skipTest("file symlink is unavailable")
        spends = {"n": 0}
        with patch("pbz.checks.spend", lambda *_a, **_k: spends.__setitem__("n", spends["n"] + 1) or True), \
                patch("pbz.memory.engine_call", lambda *_a, **_k: {"ok": True}), \
                patch("pbz.memory.desktop.require_port", return_value=1):
            with self.assertRaises(PbzError) as caught:
                memory.run(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(spends["n"], 0)
        self.assertEqual(open(secret, "rb").read(), CANARY)

    def test_hardlinked_state_is_not_read(self):
        outside = self._outside()
        secret = os.path.join(outside, "secret.json")
        with open(secret, "wb") as handle:
            handle.write(CANARY)
        os.makedirs(os.path.join(self.ws.pbz_dir, "audit"))
        linked = os.path.join(self.ws.pbz_dir, "audit", "waivers.json")
        log = os.path.join(self.ws.pbz_dir, "push-log.jsonl")
        try:
            os.link(secret, linked)
            os.link(secret, log)
        except OSError:
            self.skipTest("hard links are unavailable")
        reads = {"n": 0}
        real_open = open

        def counting(path, *args, **kwargs):
            reads["n"] += 1
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", counting):
            with self.assertRaises(PbzError) as caught:
                audit._load_json(self.ws, ("audit", "waivers.json"), {})
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            with self.assertRaises(PbzError) as caught:
                docs._changelog(self.ws, self.project)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            with self.assertRaises(PbzError) as caught:
                mirror._log_push(self.ws, self.project, "s", ["+ measure X"])
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(reads["n"], 0)
        self.assertNotIn(b"CANARY", str(caught.exception).encode())
        self.assertEqual(open(secret, "rb").read(), CANARY)

    def test_substituted_handle_is_not_read(self):
        os.makedirs(self.ws.pbz_dir)
        path = os.path.join(self.ws.pbz_dir, "push-log.jsonl")
        with open(path, "wb") as handle:
            handle.write(b"safe\n")
        outside = self._outside()
        other = os.path.join(outside, "other.jsonl")
        with open(other, "wb") as handle:
            handle.write(CANARY)
        reads = {"n": 0}
        real_open = open

        class Guard:
            def __init__(self, raw):
                self.raw = raw

            def read(self, *args, **kwargs):
                reads["n"] += 1
                return self.raw.read(*args, **kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.raw.close()

            def fileno(self):
                return self.raw.fileno()

        def swapped(target, *args, **kwargs):
            chosen = other if os.path.normcase(str(target)) == os.path.normcase(path) else target
            return Guard(real_open(chosen, *args, **kwargs))

        with patch("builtins.open", swapped):
            with self.assertRaises(PbzError) as caught:
                read_checked(path, root=self.ws.root)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(reads["n"], 0)
        self.assertEqual(open(other, "rb").read(), CANARY)

    def test_append_checked_refuses_same_inode_path_swap(self):
        import pbz.common as common_mod
        os.makedirs(self.ws.pbz_dir)
        path = os.path.join(self.ws.pbz_dir, "push-log.jsonl")
        with open(path, "wb") as handle:
            handle.write(b"safe\n")
        outside = self._outside()
        canary = os.path.join(outside, "canary.jsonl")
        with open(canary, "wb") as handle:
            handle.write(CANARY)
        before = open(path, "rb").read()
        external = open(canary, "rb").read()
        writes = {"n": 0}
        closed = {"n": 0}
        opened = {"n": 0}
        real_open = os.open
        real_write = os.write
        real_close = os.close
        real_lstat = os.lstat
        real_reparse = common_mod._reparse_link
        real_contained = common_mod._contained

        def count_write(fd, data):
            writes["n"] += 1
            return real_write(fd, data)

        def count_close(fd):
            closed["n"] += 1
            return real_close(fd)

        def mark_open(target, flags, mode=0o777):
            fd = real_open(target, flags, mode)
            if os.path.normcase(str(target)) == os.path.normcase(path):
                opened["n"] += 1
            return fd

        def lstat_identity_break(target):
            st = real_lstat(target)
            if opened["n"] and os.path.normcase(str(target)) == os.path.normcase(path):
                # Same handle identity survived open; the live path entry no longer matches.
                return real_lstat(canary)
            return st

        def reparse_after(target):
            if opened["n"] and os.path.normcase(str(target)) == os.path.normcase(path):
                return True
            return real_reparse(target)

        def contained_after(target, root):
            if opened["n"] and os.path.normcase(str(target)) == os.path.normcase(path):
                raise PbzError("UNSAFE_PATH", "refusing to write through a link")
            return real_contained(target, root)

        for injector in (
            {"os.lstat": lstat_identity_break},
            {"pbz.common._reparse_link": reparse_after},
            {"pbz.common._contained": contained_after},
        ):
            writes["n"] = 0
            closed["n"] = 0
            opened["n"] = 0
            patches = [patch("os.open", mark_open), patch("os.write", count_write), patch("os.close", count_close)]
            if "os.lstat" in injector:
                patches.append(patch("os.lstat", injector["os.lstat"]))
            if "pbz.common._reparse_link" in injector:
                patches.append(patch.object(common_mod, "_reparse_link", injector["pbz.common._reparse_link"]))
            if "pbz.common._contained" in injector:
                patches.append(patch.object(common_mod, "_contained", injector["pbz.common._contained"]))
            from contextlib import ExitStack
            with ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                with self.assertRaises(PbzError) as caught:
                    append_checked(path, "line\n", root=self.ws.root)
            self.assertEqual(caught.exception.code, "UNSAFE_PATH")
            self.assertEqual(writes["n"], 0)
            self.assertGreaterEqual(closed["n"], 1)
            self.assertEqual(open(path, "rb").read(), before)
            self.assertEqual(open(canary, "rb").read(), external)
        self.assertEqual(list(Path(outside).rglob("*.pbz-tmp")), [])
        self.assertEqual(list(Path(self.ws.root).rglob("*.pbz-tmp")), [])

    def test_failed_waiver_replace_keeps_old_bytes(self):
        audit._save_json(self.ws, ("audit", "waivers.json"), {"keep": {"reason": "old"}})
        before = open(os.path.join(self.ws.pbz_dir, "audit", "waivers.json"), "rb").read()
        real = os.replace

        def boom(src, dst):
            if str(src).endswith(".pbz-tmp"):
                raise OSError("replace")
            return real(src, dst)

        with patch("os.replace", boom):
            with self.assertRaises(OSError):
                audit._save_json(self.ws, ("audit", "waivers.json"), {"keep": {"reason": "new"}})
        self.assertEqual(open(os.path.join(self.ws.pbz_dir, "audit", "waivers.json"), "rb").read(), before)
        self.assertEqual(list(Path(self.ws.pbz_dir).rglob("*.pbz-tmp")), [])

    def test_invalid_fail_on_writes_nothing(self):
        before = self._project_map()
        with self.assertRaises(PbzError) as caught:
            audit.run(self.ws, self.project, fail_on="nope", save_baseline="should-not")
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        with self.assertRaises(PbzError) as caught:
            audit.run(self.ws, self.project, fail_on="new", save_baseline="should-not")
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertEqual(self._project_map(), before)
        self.assertFalse(os.path.exists(self.ws.pbz_dir))

    def test_malformed_waiver_is_refused(self):
        os.makedirs(os.path.join(self.ws.pbz_dir, "audit"))
        path = os.path.join(self.ws.pbz_dir, "audit", "waivers.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{")
        with self.assertRaises(PbzError) as caught:
            audit._load_json(self.ws, ("audit", "waivers.json"), {})
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, "audit state is malformed")
        self.assertNotIn(path, str(caught.exception))

    def _seed_tiny_project(self):
        model = os.path.join(self.tmp.name, "Canary.SemanticModel", "definition")
        os.makedirs(os.path.join(model, "tables"))
        with open(os.path.join(self.tmp.name, "Canary.SemanticModel", "definition.pbism"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        with open(os.path.join(model, "database.tmdl"), "w", encoding="utf-8") as handle:
            handle.write("database\n\tcompatibilityLevel: 1600\n")
        with open(os.path.join(model, "model.tmdl"), "w", encoding="utf-8") as handle:
            handle.write("model Model\n\tculture: en-US\n\nref table T\n")
        with open(os.path.join(model, "tables", "T.tmdl"), "w", encoding="utf-8") as handle:
            handle.write("table T\n\tmeasure M = 1\n\t\tlineageTag: 10000000-0000-0000-0000-000000000001\n")
        report = os.path.join(self.tmp.name, "Canary.Report", "definition")
        os.makedirs(report)
        with open(os.path.join(self.tmp.name, "Canary.Report", "definition.pbir"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        with open(os.path.join(report, "report.json"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        mirror.pull(self.ws, self.project)

    def test_structurally_malformed_audit_state_is_refused(self):
        self._seed_tiny_project()
        before = self._project_map()
        waiver = os.path.join(self.ws.pbz_dir, "audit", "waivers.json")
        os.makedirs(os.path.dirname(waiver), exist_ok=True)
        samples = [
            {"bad": None},
            {"bad": []},
            {"ok": {"reason": "x", "expires": ["not-a-date"]}},
        ]
        for sample in samples:
            with open(waiver, "w", encoding="utf-8") as handle:
                json.dump(sample, handle)
            with self.assertRaises(PbzError) as caught:
                audit.run(self.ws, self.project)
            self.assertEqual(caught.exception.code, "REFUSED")
            self.assertEqual(caught.exception.detail, "audit state is malformed")
            self.assertNotIn("CANARY", str(caught.exception))
            self.assertNotIn(waiver, str(caught.exception))
        os.remove(waiver)
        base_dir = os.path.join(self.ws.pbz_dir, "audit", "baselines")
        os.makedirs(base_dir, exist_ok=True)
        base = os.path.join(base_dir, "b1.json")
        for sample in ({}, {"saved": "x"}, {"findings": None}, {"findings": [{}]}, {"findings": [{"id": 1}]}):
            with open(base, "w", encoding="utf-8") as handle:
                json.dump(sample, handle)
            before_base = open(base, "rb").read()
            with self.assertRaises(PbzError) as caught:
                audit.run(self.ws, self.project, since="b1", save_baseline="should-not")
            self.assertEqual(caught.exception.code, "REFUSED")
            self.assertEqual(caught.exception.detail, "audit state is malformed")
            self.assertNotIn("CANARY", str(caught.exception))
            self.assertEqual(open(base, "rb").read(), before_base)
            self.assertFalse(os.path.exists(os.path.join(base_dir, "should-not.json")))
        with open(base, "w", encoding="utf-8") as handle:
            json.dump({"findings": []}, handle)
        empty = audit.run(self.ws, self.project, since="b1")
        self.assertEqual(empty.get("baseline", {}).get("name"), "b1")
        self.assertEqual(self._project_map(), before)
        self.assertFalse(os.path.exists(os.path.join(base_dir, "should-not.json")))
        self.assertEqual(list(Path(self.ws.root).rglob("*.pbz-tmp")), [])

    def test_audit_decoder_depth_is_refused(self):
        self._seed_tiny_project()
        before = self._project_map()
        depth = 5000
        body = (('{"a":' * depth) + "1" + ("}" * depth)).encode()
        waiver = os.path.join(self.ws.pbz_dir, "audit", "waivers.json")
        os.makedirs(os.path.dirname(waiver), exist_ok=True)
        with open(waiver, "wb") as handle:
            handle.write(body)
        waiver_before = open(waiver, "rb").read()
        with self.assertRaises(PbzError) as caught:
            audit.run(self.ws, self.project)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, "audit state is malformed")
        self.assertNotIn(waiver, str(caught.exception))
        self.assertEqual(open(waiver, "rb").read(), waiver_before)
        os.remove(waiver)
        base_dir = os.path.join(self.ws.pbz_dir, "audit", "baselines")
        os.makedirs(base_dir, exist_ok=True)
        base = os.path.join(base_dir, "deep.json")
        with open(base, "wb") as handle:
            handle.write(body)
        base_before = open(base, "rb").read()
        with self.assertRaises(PbzError) as caught:
            audit.run(self.ws, self.project, since="deep", save_baseline="should-not")
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, "audit state is malformed")
        self.assertEqual(open(base, "rb").read(), base_before)
        self.assertFalse(os.path.exists(os.path.join(base_dir, "should-not.json")))
        self.assertEqual(self._project_map(), before)
        self.assertEqual(list(Path(self.ws.root).rglob("*.pbz-tmp")), [])

    def test_workspace_via_junction_still_writes_state(self):
        real = os.path.join(self.tmp.name, "real-ws")
        os.makedirs(real)
        link = os.path.join(self.tmp.name, "linked-ws")
        self._junction(link, real)
        ws = Workspace(link)
        open(os.path.join(link, "Canary.pbip"), "w", encoding="utf-8").close()
        audit._save_json(ws, ("audit", "waivers.json"), {})
        body = json.loads(open(os.path.join(real, ".pbz", "audit", "waivers.json"), encoding="utf-8").read())
        self.assertEqual(body, {})

    def test_late_push_log_failure_is_a_warning(self):
        model = os.path.join(self.tmp.name, "Canary.SemanticModel", "definition")
        os.makedirs(os.path.join(model, "tables"))
        with open(os.path.join(self.tmp.name, "Canary.SemanticModel", "definition.pbism"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        with open(os.path.join(model, "database.tmdl"), "w", encoding="utf-8") as handle:
            handle.write("database\n\tcompatibilityLevel: 1600\n")
        with open(os.path.join(model, "model.tmdl"), "w", encoding="utf-8") as handle:
            handle.write("model Model\n\tculture: en-US\n\nref table T\n")
        with open(os.path.join(model, "tables", "T.tmdl"), "w", encoding="utf-8") as handle:
            handle.write("table T\n\tmeasure M = 1\n\t\tlineageTag: 10000000-0000-0000-0000-000000000001\n")
        report = os.path.join(self.tmp.name, "Canary.Report")
        os.makedirs(os.path.join(report, "definition"))
        with open(os.path.join(report, "definition.pbir"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        with open(os.path.join(report, "definition", "report.json"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        pulled = mirror.pull(self.ws, self.project)
        self.assertEqual(pulled["result"], "PULLED")
        measure = os.path.join(self.ws.mirror, "Canary.SemanticModel", "definition", "tables", "T.tmdl")
        text = open(measure, encoding="utf-8").read().replace("measure M = 1", "measure M = 2")
        with open(measure, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        before_real = open(os.path.join(model, "tables", "T.tmdl"), encoding="utf-8").read()

        with patch("pbz.mirror.validate.run_stage", return_value=[]), \
                patch("pbz.mirror.desktop.find_instance", return_value=None), \
                patch("pbz.mirror.append_checked",
                      side_effect=PbzError("UNSAFE_PATH", "refusing to write through a link")):
            result = mirror.push(self.ws, self.project)
        self.assertEqual(result.get("result"), "PUSHED")
        self.assertIn({"code": "POST_COMMIT_HISTORY", "message": "commit succeeded; follow-up work is incomplete"},
                      result.get("warnings") or [])
        self.assertIn("measure M = 2", open(os.path.join(model, "tables", "T.tmdl"), encoding="utf-8").read())
        self.assertNotEqual(before_real, open(os.path.join(model, "tables", "T.tmdl"), encoding="utf-8").read())
        self.assertEqual(list(Path(self.ws.root).rglob("*.pbz-tmp")), [])

    def test_audit_swallows_unsafe_memory_but_direct_load_refuses(self):
        outside = self._outside()
        secret = os.path.join(outside, "memory.json")
        with open(secret, "wb") as handle:
            handle.write(CANARY)
        os.makedirs(self.ws.pbz_dir)
        try:
            os.link(secret, os.path.join(self.ws.pbz_dir, "memory.json"))
        except OSError:
            self.skipTest("hard links are unavailable")
        reads = {"n": 0}
        real_open = open

        def counting(path, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(secret):
                reads["n"] += 1
            return real_open(path, *args, **kwargs)

        with patch("builtins.open", counting):
            self.assertIs(audit._memory(self.ws, self.project), audit._MEMORY_INCOMPLETE)
            with self.assertRaises(PbzError) as caught:
                memory.load(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(reads["n"], 0)

    def _assert_memory_run_refuses_before_desktop(self, dest_setup):
        outside = self._outside()
        secret = os.path.join(outside, "memory.json")
        with open(secret, "wb") as handle:
            handle.write(CANARY)
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        dest = os.path.join(self.ws.pbz_dir, "memory.json")
        dest_setup(dest, secret)
        desktop = {"n": 0}
        budget = {"n": 0}
        engine = {"n": 0}
        before = open(secret, "rb").read()
        before_map = sorted(p.relative_to(outside).as_posix() for p in Path(outside).rglob("*") if p.is_file())

        def require_port(*_a, **_k):
            desktop["n"] += 1
            return 1234

        def spend(*_a, **_k):
            budget["n"] += 1
            return True

        def engine_call(*_a, **_k):
            engine["n"] += 1
            return {"ok": True}

        with patch("pbz.memory.desktop.require_port", require_port), \
                patch("pbz.memory.checks.spend", spend), \
                patch("pbz.memory.engine_call", engine_call):
            with self.assertRaises(PbzError) as caught:
                memory.run(self.ws, self.project)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual((desktop["n"], budget["n"], engine["n"]), (0, 0, 0))
        self.assertEqual(open(secret, "rb").read(), before)
        after_map = sorted(p.relative_to(outside).as_posix() for p in Path(outside).rglob("*") if p.is_file())
        self.assertEqual(after_map, before_map)
        self.assertEqual(list(Path(self.ws.root).rglob("*.pbz-tmp")), [])

    def test_memory_run_refuses_hardlinked_dest_before_desktop(self):
        def setup(dest, secret):
            try:
                os.link(secret, dest)
            except OSError:
                self.skipTest("hard links are unavailable")

        self._assert_memory_run_refuses_before_desktop(setup)

    def test_memory_run_refuses_directory_dest_before_desktop(self):
        def setup(dest, _secret):
            os.makedirs(dest)

        self._assert_memory_run_refuses_before_desktop(setup)


if __name__ == "__main__":
    unittest.main()
