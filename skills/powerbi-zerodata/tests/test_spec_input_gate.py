"""`--spec` and sibling loaders refuse protected files before the first content byte.

Run: python tests/test_spec_input_gate.py
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _junction import make_junction  # noqa: E402
from pbz import cli, control, recipes, requirements, synthtest  # noqa: E402
from pbz.common import PbzError, Workspace  # noqa: E402

CANARY = b"CANARY-SPEC-SECRET"
SPEC = {"checks": [{"id": "c1", "kind": "compiles", "expr": "1"}]}
TASK = {"tasks": [{"id": "R1", "source": {"file": "Tasks.xlsx", "sheet": "Tasks", "cells": ["A1"]}}]}


class SpecInputGateTests(unittest.TestCase):
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
        self.reads = []
        real_read = os.read

        def watch(fd, n):
            self.reads.append(fd)
            return real_read(fd, n)

        self.watch = watch

    def _file(self, path, body):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(body)

    def _refused(self, path):
        self.reads.clear()
        with patch("os.read", self.watch):
            with self.assertRaises(PbzError) as caught:
                control.load_spec(path, root=self.ws.root)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, "refusing to read a protected specification")
        self.assertNotIn("CANARY", str(caught.exception))
        self.assertEqual(self.reads, [])

    def _import_refused(self, path):
        called = []

        def protect(dest, data):
            called.append(dest)

        self.reads.clear()
        with patch("os.read", self.watch), patch("pbz.dpapi.write_protected", protect):
            with self.assertRaises(PbzError) as caught:
                requirements.import_spec(self.ws, self.project, path)
        self.assertIn(caught.exception.code, ("REFUSED", "BAD_REQUEST"))
        self.assertNotIn(b"CANARY", str(caught.exception).encode())
        self.assertEqual(called, [])
        self.assertEqual(self.reads, [])
        self.assertFalse(os.path.isdir(os.path.join(self.ws.private, "requirements")))

    def test_malformed_json_is_invalid_not_local_state(self):
        path = os.path.join(self.tmp.name, "bad.json")
        self._file(path, b"{not-json CANARY-SPEC-SECRET")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--workspace", self.root, "check", "--validate-only", path])
        text = buf.getvalue()
        body = json.loads(text)
        self.assertEqual(rc, 2)
        self.assertEqual(body["error"], "BAD_REQUEST")
        self.assertEqual(body["detail"], "invalid specification")
        self.assertEqual(body["outcome"]["status"], "INVALID")
        self.assertEqual(body["outcome"]["exit_code"], 2)
        self.assertNotIn("LOCAL_STATE_UNAVAILABLE", text)
        self.assertNotIn("CANARY", text)
        self.assertNotIn(path, text)

    def test_symlink_to_private_json_is_refused_unread(self):
        secret = os.path.join(self.ws.private, "hidden.json")
        self._file(secret, b'{"checks":[]} ' + CANARY)
        link = os.path.join(self.tmp.name, "via.json")
        try:
            os.symlink(secret, link)
        except OSError:
            self.skipTest("symlink unavailable")
        self._refused(link)
        self._import_refused(link)
        self.assertEqual(open(secret, "rb").read(), b'{"checks":[]} ' + CANARY)

    def test_semantic_model_and_pbz_paths_are_refused(self):
        model = os.path.join(self.project.model_dir, "spec.json")
        pbz = os.path.join(self.ws.pbz_dir, "spec.json")
        self._file(model, b'{"ok":true,"note":"CANARY-SPEC-SECRET"}')
        self._file(pbz, b'{"ok":true,"note":"CANARY-SPEC-SECRET"}')
        self._refused(model)
        self._refused(pbz)
        self._import_refused(model)
        self._import_refused(pbz)

    def test_short_path_alias_under_semantic_model_is_refused(self):
        import subprocess
        model = os.path.join(self.project.model_dir, "definition", "spec.json")
        self._file(model, b'{"ok":true,"note":"CANARY-SPEC-SECRET"}')
        script = (
            "$fso = New-Object -ComObject Scripting.FileSystemObject; "
            f"$fso.GetFile('{model.replace(chr(39), chr(39)+chr(39))}').ShortPath"
        )
        try:
            short = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", script],
                text=True,
                encoding="utf-8",
                errors="replace",
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            self.skipTest("Windows short paths are unavailable")
        if not short or "~" not in short:
            self.skipTest("Windows short paths are unavailable")
        self.assertTrue(os.path.isfile(short))
        self.assertTrue(os.path.samefile(short, model))
        self._refused(short)
        self._import_refused(short)
        self.assertEqual(open(model, "rb").read(), b'{"ok":true,"note":"CANARY-SPEC-SECRET"}')

    def test_ancestor_junction_and_hardlink_refuse_before_read(self):
        outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(outside)
        secret = os.path.join(outside, "spec.json")
        self._file(secret, b'{"checks":[],"note":"CANARY-SPEC-SECRET"}')
        link = os.path.join(self.root, "alias")
        make_junction(self, link, outside)
        self._refused(os.path.join(link, "spec.json"))
        self._import_refused(os.path.join(link, "spec.json"))
        plain = os.path.join(self.tmp.name, "plain.json")
        self._file(plain, b'{"checks":[]}')
        hard = os.path.join(self.tmp.name, "hard.json")
        try:
            os.link(plain, hard)
        except OSError:
            self.skipTest("hard links unavailable")
        self.assertGreater(os.lstat(plain).st_nlink, 1)
        self._refused(hard)
        self._import_refused(hard)
        self.assertIn(b"CANARY-SPEC-SECRET", open(secret, "rb").read())

    def test_deep_ancestor_walk_fails_closed(self):
        outside = os.path.join(self.tmp.name, "deep-outside")
        cur = outside
        for i in range(70):
            cur = os.path.join(cur, f"d{i:02d}")
        os.makedirs(cur)
        secret = os.path.join(cur, "spec.json")
        body = (
            b'{"tasks":[{"id":"R1","source":{"file":"Tasks.xlsx","sheet":"Tasks","cells":["A1"]}}],'
            b'"note":"CANARY-SPEC-SECRET"}'
        )
        self._file(secret, body)
        alias = os.path.join(self.tmp.name, "deep-alias")
        make_junction(self, alias, outside)
        path = os.path.join(alias, * [f"d{i:02d}" for i in range(70)], "spec.json")
        self.assertTrue(os.path.isfile(path))
        before = open(secret, "rb").read()
        self.reads.clear()
        with patch("os.read", self.watch):
            with self.assertRaises(PbzError) as caught:
                control.load_spec(path, root=self.ws.root)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, "refusing to read a protected specification")
        self.assertNotIn("CANARY", str(caught.exception))
        self.assertEqual(self.reads, [])
        self._import_refused(path)
        self.assertEqual(open(secret, "rb").read(), before)
        self.assertEqual(list(Path(self.tmp.name).rglob("*.pbz-tmp")), [])
        ok = os.path.join(self.root, "ok-spec.json")
        self._file(ok, json.dumps(SPEC).encode())
        self.assertEqual(control.load_spec(ok, root=self.ws.root)["checks"][0]["id"], "c1")

    def test_oversize_is_bad_request(self):
        path = os.path.join(self.tmp.name, "big.json")
        self._file(path, b"{" + b" " * (control.SPEC_CAP + 1))
        with self.assertRaises(PbzError) as caught:
            control.load_spec(path, root=self.ws.root)
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertEqual(caught.exception.detail, "invalid specification")
        self.assertNotIn(path, str(caught.exception))

    def test_deep_nesting_and_surrogate_are_invalid_spec(self):
        depth = 5000
        path = os.path.join(self.tmp.name, "deep.json")
        self._file(path, (('{"a":' * depth) + "1" + ("}" * depth)).encode())
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--workspace", self.root, "check", "--validate-only", path])
        text = buf.getvalue()
        body = json.loads(text)
        self.assertEqual(rc, 2)
        self.assertEqual(body["error"], "BAD_REQUEST")
        self.assertEqual(body["detail"], "invalid specification")
        self.assertEqual(body["outcome"]["status"], "INVALID")
        self.assertEqual(body["outcome"]["exit_code"], 2)
        self.assertNotIn("CANARY", text)
        self.assertNotIn(path, text)
        self.assertFalse(os.path.exists(os.path.join(self.ws.pbz_dir, "evidence.jsonl")))
        called = []
        with patch("pbz.dpapi.write_protected", lambda dest, data: called.append(dest)):
            with self.assertRaises(PbzError) as caught:
                requirements.import_spec(self.ws, self.project, path)
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertEqual(caught.exception.detail, "invalid specification")
        self.assertEqual(called, [])
        self.assertFalse(os.path.isdir(os.path.join(self.ws.private, "requirements")))
        with self.assertRaises(PbzError) as caught:
            control.load_spec('{"k":"' + "\ud800" + '"}', root=self.ws.root)
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertEqual(caught.exception.detail, "invalid specification")
        self.assertNotIn("CANARY", str(caught.exception))

    def test_sibling_inline_specs_use_shared_gate(self):
        depth = 5000
        deep = ('{"a":' * depth) + "1" + ("}" * depth)
        oversize = '{"a":"' + ("x" * (control.SPEC_CAP + 1)) + '"}'
        surrogate = '{"k":"' + "\ud800" + '"}'
        ran = {"n": 0}

        def boom(*_a, **_k):
            ran["n"] += 1
            raise AssertionError("downstream must not run")

        for bad in (deep, oversize, surrogate):
            with self.assertRaises(PbzError) as caught:
                recipes.load(bad, root=self.ws.root)
            self.assertEqual(caught.exception.code, "BAD_REQUEST")
            self.assertEqual(caught.exception.detail, "invalid specification")
            self.assertNotIn("CANARY", str(caught.exception))
            with self.assertRaises(PbzError) as caught:
                synthtest._object_spec(bad, self.ws.root, missing="missing")
            self.assertEqual(caught.exception.code, "BAD_REQUEST")
            self.assertEqual(caught.exception.detail, "invalid specification")
            self.assertNotIn("CANARY", str(caught.exception))
            with patch("pbz.synthtest._dax_ok", boom), patch("pbz.recipes._subst", boom):
                with self.assertRaises(PbzError):
                    synthtest._spec_queries(bad, self.ws.root)
                with self.assertRaises(PbzError):
                    recipes.load(bad, root=self.ws.root)
            self.assertEqual(ran["n"], 0)
        ok_recipe = json.dumps({"recipe": "synthetic", "steps": [{"verb": "diff", "args": []}]})
        self.assertEqual(recipes.load(ok_recipe, root=self.ws.root)["steps"][0]["verb"], "diff")
        ok_queries = json.dumps({"queries": [{"id": "q1", "dax": "EVALUATE ROW(\"v\", 1)"}]})
        self.assertEqual(synthtest._spec_queries(ok_queries, self.ws.root)[0]["id"], "q1")

    def test_inline_dict_and_workspace_file_still_work(self):
        self.assertEqual(control.load_spec(dict(SPEC), root=self.ws.root), SPEC)
        inline = json.dumps(SPEC)
        self.assertEqual(control.load_spec(inline, root=self.ws.root)["checks"][0]["id"], "c1")
        path = os.path.join(self.root, "checks.json")
        self._file(path, json.dumps(SPEC).encode())
        self.assertEqual(control.load_spec(path, root=self.ws.root)["checks"][0]["kind"], "compiles")
        recipe = {"recipe": "synthetic", "steps": [{"verb": "diff", "args": []}]}
        rpath = os.path.join(self.root, "recipe.json")
        self._file(rpath, json.dumps(recipe).encode())
        self.assertEqual(recipes.load(rpath, root=self.ws.root)["steps"][0]["verb"], "diff")
        queries = {"queries": [{"id": "q1", "dax": "EVALUATE ROW(\"v\", 1)"}]}
        qpath = os.path.join(self.root, "queries.json")
        self._file(qpath, json.dumps(queries).encode())
        self.assertEqual(synthtest._spec_queries(qpath, self.ws.root)[0]["id"], "q1")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["--workspace", self.root, "check", "--validate-only", path])
        body = json.loads(buf.getvalue())
        self.assertEqual(rc, 0)
        self.assertTrue(body["spec_valid"])
        self.assertFalse(os.path.exists(os.path.join(self.ws.pbz_dir, "evidence.jsonl")))
        self.assertNotIn("CANARY", buf.getvalue())

    def test_swapped_handle_is_not_read(self):
        path = os.path.join(self.tmp.name, "ok.json")
        self._file(path, json.dumps(SPEC).encode())
        secret = os.path.join(self.tmp.name, "secret.json")
        self._file(secret, CANARY)
        real_open = os.open
        real_read = os.read
        reads = []

        def swap(target, flags, mode=0o777):
            if os.path.abspath(target) == os.path.abspath(path):
                return real_open(secret, flags)
            return real_open(target, flags, mode)

        def watch(fd, n):
            reads.append(fd)
            return real_read(fd, n)

        with patch("os.open", swap), patch("os.read", watch):
            with self.assertRaises(PbzError) as caught:
                control.load_spec(path, root=self.ws.root)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(reads, [])
        self.assertEqual(open(secret, "rb").read(), CANARY)


if __name__ == "__main__":
    unittest.main()
