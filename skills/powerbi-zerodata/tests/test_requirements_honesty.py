"""Requirement proofs stay bound to the mirror and the saved real bytes.

Run: python tests/test_requirements_honesty.py
"""
from __future__ import annotations

import hashlib
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

from pbz import mirror, requirements  # noqa: E402
from pbz.common import PbzError, Workspace  # noqa: E402

SRC = {"file": "Tasks.xlsx", "sheet": "Tasks", "cells": ["A1"]}
CHECK = {"id": "c1", "kind": "compiles", "expr": "1"}
CANARY = b"CANARY-LEDGER-SECRET"


class RequirementsHonestyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.root)
        open(os.path.join(self.root, "Fixture.pbip"), "w", encoding="utf-8").close()
        self.ws = Workspace(self.root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private)
        self.project = self.ws.project()
        self.rel = "Fixture.Report/note.txt"
        self.body = b"synthetic-note"
        for base in (self.ws.root, self.ws.mirror):
            path = os.path.join(base, *self.rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(self.body)
        digest = hashlib.sha256(self.body).hexdigest()
        mirror.save_manifest(self.ws, self.project, {"version": 1, "files": {
            self.rel: {"real": digest, "mirror": digest}}})
        self.store = {}

        def protect(path, data):
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(data)
            self.store[os.path.abspath(path)] = data

        def reveal(path):
            return self.store.get(os.path.abspath(path))

        self.protect = protect
        self.patches = [
            patch("pbz.dpapi.write_protected", protect),
            patch("pbz.dpapi.read_protected", reveal),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def _task(self, ident="R1", checks=None, deps=None):
        task = {"id": ident, "description": "CANARY-PRIVATE-TEXT", "source": dict(SRC)}
        if checks is not None:
            task["checks"] = checks
        if deps is not None:
            task["depends_on"] = deps
        return task

    def _import(self, tasks):
        return requirements.import_spec(self.ws, self.project, {"tasks": tasks})

    def _ledger_text(self):
        return Path(requirements._path(self.ws, self.project)).read_text(encoding="utf-8")

    def _pass(self, ident="c1"):
        def run(_ws, _project, spec):
            self.ran.append(spec)
            return {"results": [{"id": ident, "status": "PASS", "note": "CANARY-CHECK-NOTE"}]}
        return run

    def test_non_list_checks_container_is_bad_request(self):
        import io
        from contextlib import redirect_stdout
        from pbz import cli
        before = dict(self.store)
        samples = (1, True, None, "checks", {"id": "c1"})
        for bad in samples:
            with self.assertRaises(PbzError) as caught:
                requirements.import_spec(self.ws, self.project, {
                    "tasks": [{"id": "R1", "description": "CANARY-PRIVATE-TEXT", "source": dict(SRC),
                               "checks": bad}]})
            self.assertEqual(caught.exception.code, "BAD_REQUEST")
            self.assertEqual(caught.exception.detail, "invalid registered acceptance checks")
            self.assertNotIn("CANARY", str(caught.exception))
        self.assertEqual(self.store, before)
        path = os.path.join(self.tmp.name, "bad-checks.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"tasks": [{"id": "R1", "source": dict(SRC), "checks": 1}]}, handle)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--workspace", self.root, "requirements", "import", path])
        text = buf.getvalue()
        body = json.loads(text)
        self.assertEqual(rc, 2)
        self.assertEqual(body["error"], "BAD_REQUEST")
        self.assertNotIn("CANARY", text)
        self.assertEqual(self.store, before)
        self.assertFalse(os.path.exists(requirements._path(self.ws, self.project)))

    def test_duplicate_check_id_never_imports(self):
        before = dict(self.store)
        with self.assertRaises(PbzError) as caught:
            self._import([self._task(checks=[CHECK, dict(CHECK)])])
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertEqual(caught.exception.detail, "duplicate registered check id")
        self.assertNotIn("CANARY", str(caught.exception))
        self.assertEqual(self.store, before)

    def test_duplicate_pass_rows_are_never_verified(self):
        self._import([self._task(checks=[CHECK, {"id": "c2", "kind": "compiles", "expr": "2"}])])
        before = self._ledger_text()

        def dup(_ws, _project, _spec):
            return {"results": [{"id": "c1", "status": "PASS"}, {"id": "c1", "status": "PASS"}]}

        with patch("pbz.checks.run_checks", dup):
            with self.assertRaises(PbzError) as caught:
                requirements.verify(self.ws, self.project, "R1")
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertEqual(caught.exception.detail, "requirement check evidence is unusable")
        self.assertNotIn("CANARY", str(caught.exception))
        self.assertEqual(self._ledger_text(), before)
        self.assertEqual(requirements.status(self.ws, self.project)["tasks"][0]["status"], "OPEN")

    def test_missing_extra_unknown_and_malformed_never_pass(self):
        self._import([self._task(checks=[CHECK])])
        before = self._ledger_text()
        samples = [
            {"results": []},
            {"results": [{"id": "c1", "status": "PASS"}, {"id": "extra", "status": "PASS"}]},
            {"results": [{"id": "c1", "status": "MAYBE"}]},
            {"results": [{"id": "c1"}]},
            {"results": [{"id": ["c1"], "status": "PASS"}]},
            {"results": [{"id": "c1", "status": {"ok": True}}]},
            {"results": ["not-a-row"]},
            "not-a-dict",
        ]
        for sample in samples:
            def run(_ws, _project, _spec, sample=sample):
                return sample
            with patch("pbz.checks.run_checks", run):
                with self.assertRaises(PbzError) as caught:
                    requirements.verify(self.ws, self.project, "R1")
            self.assertEqual(caught.exception.code, "BAD_REQUEST")
            self.assertNotIn("CANARY", str(caught.exception))
        self.assertEqual(self._ledger_text(), before)

    def test_missing_sync_blocks_before_checks(self):
        mirror.save_manifest(self.ws, self.project, {})
        self._import([self._task(checks=[CHECK])])
        self.ran = []
        with patch("pbz.checks.run_checks", self._pass()):
            with self.assertRaises(PbzError) as caught:
                requirements.verify(self.ws, self.project, "R1")
        self.assertEqual(caught.exception.code, "STALE_EVIDENCE")
        self.assertEqual(caught.exception.detail, "sync evidence is unusable")
        self.assertEqual(self.ran, [])

    def test_change_during_verify_does_not_stamp_a_later_revision(self):
        self._import([self._task(checks=[CHECK])])
        before = self._ledger_text()
        real = os.path.join(self.ws.root, *self.rel.split("/"))

        def run(_ws, _project, _spec):
            with open(real, "wb") as f:
                f.write(b"changed-real")
            return {"results": [{"id": "c1", "status": "PASS"}]}

        with patch("pbz.checks.run_checks", run):
            with self.assertRaises(PbzError) as caught:
                requirements.verify(self.ws, self.project, "R1")
        self.assertIn(caught.exception.code, ("CONFLICT", "STALE_EVIDENCE", "PENDING_PUSH"))
        self.assertEqual(self._ledger_text(), before)
        self.assertNotIn("changed-real", before)

    def test_real_change_after_verify_blocks_accept_and_status(self):
        self._import([self._task("R1", checks=None), self._task("R2", checks=[CHECK], deps=["R1"])])
        with patch("pbz.requirements._human_terminal", return_value=True):
            requirements.accept(self.ws, self.project, "R1", human=True)
        with patch("pbz.checks.run_checks", self._pass()):
            self.ran = []
            out = requirements.verify(self.ws, self.project, "R2")
        self.assertEqual(out["result"], "VERIFIED")
        with patch("pbz.requirements._human_terminal", return_value=True):
            requirements.accept(self.ws, self.project, "R2", human=True)
        self.assertEqual({t["id"]: t["status"] for t in requirements.status(self.ws, self.project)["tasks"]},
                         {"R1": "ACCEPTED", "R2": "ACCEPTED"})
        real = os.path.join(self.ws.root, *self.rel.split("/"))
        with open(real, "wb") as f:
            f.write(b"later-real")
        states = {t["id"]: t["status"] for t in requirements.status(self.ws, self.project)["tasks"]}
        self.assertEqual(states, {"R1": "STALE", "R2": "STALE"})
        with patch("pbz.requirements._human_terminal", return_value=True):
            with self.assertRaises(PbzError) as blocked:
                requirements.accept(self.ws, self.project, "R2", human=True)
        self.assertEqual(blocked.exception.code, "CONFLICT")
        digest = hashlib.sha256(b"later-real").hexdigest()
        man = mirror.load_manifest(self.ws, self.project)
        man["files"][self.rel]["real"] = digest
        mirror.save_manifest(self.ws, self.project, man)
        with patch("pbz.checks.run_checks", self._pass()):
            self.ran = []
            again = requirements.verify(self.ws, self.project, "R2")
        self.assertEqual(again["result"], "VERIFIED")
        with patch("pbz.requirements._human_terminal", return_value=True):
            with self.assertRaises(PbzError) as caught:
                requirements.accept(self.ws, self.project, "R2", human=True)
        self.assertEqual(caught.exception.code, "DEPENDENCY_PENDING")
        self.assertEqual(requirements.status(self.ws, self.project)["tasks"][0]["status"], "STALE")

    def test_legacy_binding_is_not_current(self):
        self._import([self._task(checks=[CHECK])])
        path = requirements._path(self.ws, self.project)
        ledger = json.loads(Path(path).read_text(encoding="utf-8"))
        ledger["tasks"]["R1"]["verification"] = {"revision": "mold", "passed": True,
                                                 "checks": [{"id": "c1", "status": "PASS"}]}
        ledger["tasks"]["R1"]["accepted_revision"] = "mold"
        Path(path).write_text(json.dumps(ledger), encoding="utf-8")
        row = requirements.status(self.ws, self.project)["tasks"][0]
        self.assertEqual(row["status"], "STALE")
        self.assertNotEqual(row["status"], "VERIFIED")
        self.assertNotEqual(row["status"], "ACCEPTED")

    def test_hardlinked_ledger_is_not_read(self):
        self._import([self._task()])
        path = requirements._path(self.ws, self.project)
        extra = path + ".link"
        try:
            os.link(path, extra)
        except OSError:
            self.skipTest("hard links unavailable")
        secret = os.path.join(self.tmp.name, "secret.txt")
        with open(secret, "wb") as f:
            f.write(CANARY)
        reads = []
        real_open = open

        def watch(target, *args, **kwargs):
            handle = real_open(target, *args, **kwargs)
            if os.path.abspath(str(target)) == os.path.abspath(path):
                orig = handle.read

                def tracked(*a, **k):
                    reads.append(b"read")
                    return orig(*a, **k)

                handle.read = tracked
            return handle

        with patch("builtins.open", watch), patch("pbz.requirements._human_terminal", return_value=True):
            for call in (
                lambda: requirements.status(self.ws, self.project),
                lambda: requirements.verify(self.ws, self.project, "R1"),
                lambda: requirements.accept(self.ws, self.project, "R1", human=True),
            ):
                with self.assertRaises(PbzError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "UNSAFE_PATH")
                self.assertNotIn("CANARY", str(caught.exception))
        self.assertEqual(reads, [])
        self.assertEqual(open(secret, "rb").read(), CANARY)

    def test_substituted_ledger_handle_is_not_read(self):
        self._import([self._task(checks=[CHECK])])
        path = requirements._path(self.ws, self.project)
        secret = os.path.join(self.tmp.name, "other-ledger.json")
        with open(secret, "wb") as f:
            f.write(CANARY)
        reads = []
        real_open = open

        def swap(target, *args, **kwargs):
            if os.path.abspath(str(target)) == os.path.abspath(path) and "b" in str(args[:1] or kwargs.get("mode", "")):
                handle = real_open(secret, "rb")

                def tracked(*a, **k):
                    reads.append(b"read")
                    raise AssertionError("substituted ledger was read")

                handle.read = tracked
                return handle
            return real_open(target, *args, **kwargs)

        before = self._ledger_text()
        self.ran = []
        with patch("builtins.open", swap), patch("pbz.checks.run_checks", self._pass()), \
                patch("pbz.requirements._human_terminal", return_value=True):
            for call in (
                lambda: requirements.status(self.ws, self.project),
                lambda: requirements.verify(self.ws, self.project, "R1"),
                lambda: requirements.accept(self.ws, self.project, "R1", human=True),
            ):
                with self.assertRaises(PbzError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "UNSAFE_PATH")
                self.assertNotIn("CANARY", str(caught.exception))
        self.assertEqual(reads, [])
        self.assertEqual(self.ran, [])
        self.assertEqual(self._ledger_text(), before)
        self.assertEqual(open(secret, "rb").read(), CANARY)

    def test_refused_import_writes_no_private_config_and_failed_save_keeps_ledger(self):
        secret = os.path.join(self.ws.pbz_dir, "spec.json")
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(secret, "wb") as f:
            f.write(b'{"tasks":[{"id":"R9","source":' + json.dumps(SRC).encode() + b'}]}CANARY')
        before = dict(self.store)
        with self.assertRaises(PbzError):
            requirements.import_spec(self.ws, self.project, secret)
        self.assertEqual(self.store, before)
        self._import([self._task()])
        kept = self._ledger_text()
        real_write = requirements.control.save

        def boom(path, value, *, root=None):
            if os.path.abspath(path) == os.path.abspath(requirements._path(self.ws, self.project)):
                raise OSError("save")
            return real_write(path, value, root=root)

        with patch("pbz.control.save", boom):
            with self.assertRaises(OSError):
                self._import([self._task("R2")])
        self.assertEqual(self._ledger_text(), kept)
        self.assertEqual(list(Path(self.ws.mirror).rglob("*.pbz-tmp")), [])

    def test_human_only_accept_unchanged(self):
        self._import([self._task()])
        with self.assertRaises(PbzError) as caught:
            requirements.accept(self.ws, self.project, "R1")
        self.assertEqual(caught.exception.code, "HUMAN_ONLY")
        with patch("pbz.requirements._human_terminal", return_value=False):
            with self.assertRaises(PbzError) as caught:
                requirements.accept(self.ws, self.project, "R1", human=True)
        self.assertEqual(caught.exception.code, "HUMAN_ONLY")

    def test_real_addition_blocks_verify_and_stales_accept(self):
        self._import([self._task("R1", checks=None), self._task("R2", checks=[CHECK], deps=["R1"])])
        added = os.path.join(self.project.report_dir, "definition", "extra.json")
        before = self._ledger_text()

        def add_during(_ws, _project, _spec):
            os.makedirs(os.path.dirname(added), exist_ok=True)
            with open(added, "w", encoding="utf-8") as handle:
                handle.write('{"name":"CANARY-ADD"}\n')
            return {"results": [{"id": "c1", "status": "PASS"}]}

        with patch("pbz.checks.run_checks", add_during):
            with self.assertRaises(PbzError) as caught:
                requirements.verify(self.ws, self.project, "R2")
        self.assertEqual(caught.exception.code, "CONFLICT")
        self.assertEqual(self._ledger_text(), before)
        if os.path.exists(added):
            os.remove(added)
        with patch("pbz.requirements._human_terminal", return_value=True):
            requirements.accept(self.ws, self.project, "R1", human=True)
        with patch("pbz.checks.run_checks", self._pass()):
            self.ran = []
            requirements.verify(self.ws, self.project, "R2")
        with patch("pbz.requirements._human_terminal", return_value=True):
            requirements.accept(self.ws, self.project, "R2", human=True)
        os.makedirs(os.path.dirname(added), exist_ok=True)
        with open(added, "w", encoding="utf-8") as handle:
            handle.write('{"name":"CANARY-ADD"}\n')
        states = {t["id"]: t["status"] for t in requirements.status(self.ws, self.project)["tasks"]}
        self.assertEqual(states, {"R1": "STALE", "R2": "STALE"})
        with patch("pbz.requirements._human_terminal", return_value=True):
            with self.assertRaises(PbzError) as blocked:
                requirements.accept(self.ws, self.project, "R2", human=True)
        self.assertEqual(blocked.exception.code, "CONFLICT")
        with patch("pbz.checks.run_checks", self._pass()):
            with self.assertRaises(PbzError) as caught:
                requirements.verify(self.ws, self.project, "R2")
        self.assertEqual(caught.exception.code, "CONFLICT")


if __name__ == "__main__":
    unittest.main()
