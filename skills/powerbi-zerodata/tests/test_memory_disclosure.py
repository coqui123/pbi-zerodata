"""Strict disclosure strips bands, including a shape-mode cache.
Run: python tests/test_memory_disclosure.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import checks, cli, common, describe, dpapi, engine, index_store, mcp, memory, mirror, policy  # noqa: E402

SENTINEL = "SECRET_ENGINE_SENTINEL_9f3a"


def _bands(blob):
    return "size_band" in blob or "share_band" in blob


class MemoryDisclosureTests(unittest.TestCase):
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
        pol = policy.load(self.ws)
        pol["disclosure"] = "strict"
        policy.save(self.ws, pol)

    def _engine(self, verb, payload, timeout=30):
        return {"ok": True, "model": {"size_band": "<1 MiB"},
                "tables": [{"name": "Sales", "size_band": "1-10 MiB", "share_band": ">=20%",
                            "columns": [{"name": "Date", "size_band": "<1 MiB", "share_band": "<1%",
                                         "time_part": True}],
                            "hierarchies": []}],
                "relationships": [], "coverage": "FULL", "predicates": "DONE"}

    def test_strict_live_and_cached_shape_drop_bands(self):
        with patch("pbz.memory.engine_call", self._engine), patch("pbz.memory.desktop.require_port", lambda p: 1), \
                patch("pbz.memory.checks.spend", lambda *a, **k: True):
            live = memory.run(self.ws, self.project)
        blob = json.dumps(live)
        self.assertFalse(_bands(blob))
        self.assertTrue(live["tables"][0]["columns"][0]["time_part"])
        cached = {"verb": "memory", "project": self.project.name, "disclosure": "shape",
                  "saved_at": "2026-01-01T00:00:00", "mirror_rev": "m-old",
                  "model": {"size_band": "<1 MiB"},
                  "tables": [{"name": "Sales", "size_band": "1-10 MiB", "share_band": "<1%",
                              "columns": [{"name": "Date", "time_part": True, "size_band": "<1 MiB",
                                           "share_band": "1-5%"}],
                              "hierarchies": [{"name": "Cal", "size_band": "<1 MiB"}]}],
                  "relationships": [{"name": "r", "size_band": "<1 MiB"}],
                  "coverage": "FULL", "predicates": "DONE"}
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        with open(os.path.join(self.ws.pbz_dir, "memory.json"), "w", encoding="utf-8") as f:
            json.dump(cached, f)
        loaded = memory.load(self.ws, self.project)
        text = json.dumps(loaded)
        self.assertFalse(_bands(text))
        self.assertEqual(loaded["disclosure"], "strict")
        self.assertTrue(loaded["tables"][0]["columns"][0]["time_part"])
        buf = io.StringIO()
        with patch("pbz.memory.engine_call", self._engine), patch("pbz.memory.desktop.require_port", lambda p: 1), \
                patch("pbz.memory.checks.spend", lambda *a, **k: True), redirect_stdout(buf):
            cli.main(["--workspace", self.ws.root, "--project", "Parity", "audit", "memory"])

            def fake_run(argv, **kwargs):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(argv[3:])

                class Proc:
                    returncode = code
                    stdout = out.getvalue().encode("utf-8")
                    stderr = b""
                return Proc()

            with patch("pbz.mcp.subprocess.run", fake_run):
                mcp_out = mcp.call_tool("audit", {"action": "memory", "workspace": self.ws.root,
                                                  "project": "Parity"}, self.ws.root, None)
        combined = buf.getvalue() + mcp_out["content"][0]["text"]
        self.assertFalse(_bands(combined))
        self.assertIn("time_part", combined)

    def test_describe_reprojects_shape_index_on_all_four_paths(self):
        pol = policy.load(self.ws)
        pol["disclosure"] = "shape"
        policy.save(self.ws, pol)

        def index_engine(verb, payload, timeout=30):
            dpapi.load_key(self.ws.private)
            with open(os.path.join(self.ws.private, "index.json"), "w") as handle:
                json.dump({"columns": {"Sales[Comment]": {"type": "string", "class": "high"}}}, handle)
            dpapi.write_protected(os.path.join(self.ws.private, "categories.bin"), b"{}")
            return {"ok": True, "tables": [{"table": "Sales", "rows_bucket": "10-99", "columns": [
                {"column": "Comment", "class": "high", "card_bucket": "10-99", "blank_bucket": "0"}]}]}

        with patch("pbz.checks.engine_call", index_engine), patch("pbz.checks.desktop.require_port", return_value=1):
            checks.index(self.ws, self.project)
        paths = (lambda: describe.overview(self.ws, self.project),
                 lambda: describe.describe(self.ws, self.project, "Sales"),
                 lambda: describe.describe(self.ws, self.project, "Sales[Comment]"),
                 lambda: describe.values(self.ws, self.project, "Sales[Comment]"))
        for read in paths:
            self.assertIn("_bucket", json.dumps(read()))  # Shape positive control on every path.
        pol["disclosure"] = "strict"
        policy.save(self.ws, pol)  # Same stored generation, no reindex.
        for read in paths:
            self.assertNotIn("_bucket", json.dumps(read()))

    def test_probe_defaults_to_mirror_columns_and_preserves_flags(self):
        def probe_engine(verb, payload, timeout=30):
            self.assertEqual(verb, "probe")
            self.assertEqual(payload["disclosure"], "strict")
            self.assertIn("Comment", payload["columns"])
            return engine._project(verb, {"ok": True, "verb": "probe", "table": "Sales", "rows_bucket": "1-9",
                "columns": [{"column": "Comment", "type": "string", "has_blanks": True, "card_bucket": "1-9"},
                            {"column": SENTINEL, "type": "string", "has_blanks": False}]}, payload)
        with patch("pbz.checks.engine_call", probe_engine), patch("pbz.checks.desktop.require_port", return_value=1), \
                patch("pbz.checks.spend", return_value=True):
            out = checks.probe(self.ws, self.project, "Sales", None)
        self.assertEqual(out["columns"], [{"column": "Comment", "type": "string", "has_blanks": True}])
        self.assertNotIn("_bucket", json.dumps(out))
        self.assertNotIn(SENTINEL, json.dumps(out))

    def test_summary_is_project_bound_on_every_describe_path(self):
        pol = policy.load(self.ws)
        pol["disclosure"] = "shape"
        policy.save(self.ws, pol)
        dpapi.load_key(self.ws.private)
        with open(os.path.join(self.ws.private, "index.json"), "w") as handle:
            json.dump({"columns": {}}, handle)
        dpapi.write_protected(os.path.join(self.ws.private, "categories.bin"), b"{}")
        summary = {"project": self.project.name, "tables": [{"table": "Sales", "rows_bucket": "10-99", "columns": [
            {"column": "Comment", "card_bucket": "10-99"}]}]}
        index_store.publish(self.ws.private, self.project.name, summary)
        self.assertIn("Sales", describe._index_summary(self.ws, self.project))
        other = common.Project(self.ws, "Other")
        shutil.copytree(self.project.mirror_model, other.mirror_model)
        shutil.copytree(self.project.mirror_report, other.mirror_report)
        paths = (lambda p: describe.overview(self.ws, p),
                 lambda p: describe.describe(self.ws, p, "Sales"),
                 lambda p: describe.describe(self.ws, p, "Sales[Comment]"),
                 lambda p: describe.values(self.ws, p, "Sales[Comment]"))
        for read in paths:
            self.assertIn("_bucket", json.dumps(read(self.project)))
            self.assertNotIn("10-99", json.dumps(read(other)))
        for legacy in ({"tables": summary["tables"]}, {**summary, "project": "Other"}):
            index_store.publish(self.ws.private, self.project.name, legacy)
            self.assertEqual(describe._index_summary(self.ws, self.project), {})

    def test_summary_malformed_entries_and_shared_recursive_stripper(self):
        dpapi.load_key(self.ws.private)
        with open(os.path.join(self.ws.private, "index.json"), "w") as handle:
            json.dump({"columns": {}}, handle)
        dpapi.write_protected(os.path.join(self.ws.private, "categories.bin"), b"{}")
        for malformed in (None, [], {"tables": None}):
            index_store.publish(self.ws.private, self.project.name,
                                {**malformed, "project": self.project.name} if isinstance(malformed, dict) else malformed)
            self.assertEqual(describe._index_summary(self.ws, self.project), {})
        pol = policy.load(self.ws)
        pol["disclosure"] = "shape"
        policy.save(self.ws, pol)
        for malformed in (None, 3, [], {}, {"table": []}):
            with self.subTest(summary=malformed):
                index_store.publish(self.ws.private, self.project.name, {"project": self.project.name,
                    "tables": [malformed, {"table": "Sales", "rows_bucket": {"secret": SENTINEL},
                        "extra": SENTINEL, "columns": [None, 5, {}, {"column": "Comment",
                            "card_bucket": "1-9", "nested": [{"custom_bucket": "1-9"}]}]}]})
                out = describe._index_summary(self.ws, self.project)
                self.assertIn("Sales", out)
                self.assertEqual(out["Sales"], {"table": "Sales", "columns": [
                    {"column": "Comment", "card_bucket": "1-9"}]})
                self.assertNotIn("rows_bucket", out["Sales"])
                self.assertNotIn(SENTINEL, json.dumps(out))
        pol["disclosure"] = "strict"
        policy.save(self.ws, pol)
        self.assertNotIn("_bucket", json.dumps(describe._index_summary(self.ws, self.project)))
        body = {"rows_bucket": "1-9", "nested": [{"custom_bucket": "1-9", "ri": "RI_OK"}]}
        self.assertEqual(memory._strip_bands(body), {"nested": [{"ri": "RI_OK"}]})

    def test_grouped_equal_cli_returns_tokens_for_the_ten_engine_groups(self):
        pol = policy.load(self.ws)
        pol["disclosure"] = "shape"
        pol["reveal_columns"] = ["Sales[Comment]"]
        policy.save(self.ws, pol)
        hashes = [dpapi.hmac_hex(dpapi.load_key(self.ws.private), f"private-{i}") for i in range(10)]

        def engine(verb, payload, timeout=30):
            self.assertIn("Sales[Comment]", payload["reveal"])
            return {"ok": True, "checks": [{"id": "equal", "status": "FAIL", "groups": [
                {"Sales[Comment]": {"hmac": h}} for h in hashes] + [
                    {"Sales[Comment]": {"reveal": "Known member"}}]}]}

        spec = {"checks": [{"id": "equal", "kind": "equal", "left": "1", "right": "0",
                            "by": ["Sales[Comment]"]}]}
        outputs = []
        for _ in range(2):
            buf = io.StringIO()
            with patch("pbz.checks.engine_call", engine), patch("pbz.checks.desktop.require_port", return_value=1), \
                    patch("pbz.cli.find_workspace", return_value=self.ws), redirect_stdout(buf):
                cli.main(["--workspace", self.ws.root, "--project", self.project.name, "check", json.dumps(spec)])
            out = json.loads(buf.getvalue())
            self.assertIn("results", out)
            groups = out["results"][0]["groups"]
            self.assertEqual(len(groups), 11)
            emitted = [g["Sales[Comment]"] for g in groups[:10]]
            self.assertTrue(all("«" in value for value in emitted))
            self.assertEqual(len(set(emitted)), 10)
            self.assertEqual(groups[10]["Sales[Comment]"], "Known member")
            self.assertTrue(all(h not in buf.getvalue() for h in hashes))
            outputs.append(emitted)
        self.assertEqual(outputs[0], outputs[1])

    def test_no_index_rows_bucket_defaults_follow_disclosure(self):
        for mode in ("shape", "strict"):
            pol = policy.load(self.ws)
            pol["disclosure"] = mode
            policy.save(self.ws, pol)
            overview = describe.overview(self.ws, self.project)
            detail = describe.describe(self.ws, self.project, "Sales")
            sales = next(t for t in overview["tables"] if t["table"] == "Sales")
            if mode == "shape":
                self.assertEqual(sales["rows_bucket"], "unknown")
                self.assertEqual(detail["rows_bucket"], "unknown (run pbz index)")
            else:
                self.assertNotIn("rows_bucket", sales)
                self.assertNotIn("rows_bucket", detail)

    def test_status_reports_linked_manifest_as_index_error(self):
        target = Path(self.tmp.name) / "manifest.json"
        target.write_text("{}")
        Path(self.ws.private, "index-current.json").symlink_to(target)
        with patch("pbz.cli.engine_call", return_value={"ok": True}):
            out = cli.cmd_status(self.ws, self.project, None)
        self.assertFalse(out["index"])
        self.assertEqual(out["index_error"], "UNSAFE_PATH")

    def test_unexpected_engine_class_is_closed(self):
        def bad(verb, payload, timeout=30):
            return {"ok": False, "class": SENTINEL, "detail": SENTINEL}

        buf = io.StringIO()
        with patch("pbz.memory.engine_call", bad), patch("pbz.memory.desktop.require_port", lambda p: 1), \
                patch("pbz.memory.checks.spend", lambda *a, **k: True), redirect_stdout(buf):
            rc = cli.main(["--workspace", self.ws.root, "--project", "Parity", "audit", "memory"])

            def fake_run(argv, **kwargs):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = cli.main(argv[3:])

                class Proc:
                    returncode = code
                    stdout = out.getvalue().encode("utf-8")
                    stderr = b""
                return Proc()

            with patch("pbz.mcp.subprocess.run", fake_run):
                mcp_out = mcp.call_tool("audit", {"action": "memory", "workspace": self.ws.root,
                                                  "project": "Parity"}, self.ws.root, None)
        blob = buf.getvalue() + mcp_out["content"][0]["text"]
        self.assertNotEqual(rc, 0)
        self.assertTrue(mcp_out["isError"])
        self.assertIn("ENGINE_UNAVAILABLE", blob)
        self.assertNotIn(SENTINEL, blob)


if __name__ == "__main__":
    unittest.main()
