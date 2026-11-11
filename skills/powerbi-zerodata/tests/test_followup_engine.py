"""Follow-up checks for trace columns, engine requests, reload, and calendars.

Run from skills/powerbi-zerodata: python tests/test_followup_engine.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL))

from pbz import calendars, common, desktop, engine, tmdl  # noqa: E402
from pbz.common import PbzError  # noqa: E402

DATE = (
    "table Date\n"
    "\tcolumn Date\n"
    "\t\tdataType: dateTime\n"
    "\t\tsourceColumn: Date\n"
)
SALES = "table Sales\n\tmeasure 'Total Sales' = 1\n"


class TraceSourceTests(unittest.TestCase):
    def test_columns_are_read_apart_and_missing_duration_is_not_zero(self):
        source = (SKILL / "engine" / "Trace.cs").read_text(encoding="utf-8")
        read = source.split("static Hit? ReadHit", 1)[1].split("static string Clip", 1)[0]
        collect = source.split("public List<Hit> Collect", 1)[1].split("public void Dispose", 1)[0]
        self.assertIn("public const long MissingDuration = -1;", source)
        self.assertNotIn("Subclass = args.EventSubclass.ToString(),", source)
        self.assertNotIn("Text = Clip(args.TextData", source)
        for needle in ("args.EventSubclass", "args.Duration", "args.ObjectName", "args.SessionID", "args.TextData"):
            self.assertEqual(read.count(needle), 1, needle)
        self.assertGreaterEqual(read.count("catch"), 5)
        self.assertIn("Hit.MissingDuration", read)
        self.assertNotIn("duration = 0", read)
        self.assertIn('hit.Class == "ProgressReportEnd"', collect)
        self.assertIn("Hit.MissingDuration", collect)
        self.assertNotIn("Console.", source)
        self.assertNotIn("duration_ms", source)


class EngineRequestTests(unittest.TestCase):
    def test_nonfinite_request_is_bad_request_and_is_not_sent(self):
        exe = Path(self._touch())
        for payload in ({"dax": float("nan")}, {"dax": float("inf")}, {"dax": float("-inf")}, {"blob": {1}}):
            with self.subTest(payload=payload), patch.object(engine, "ENGINE_EXE", exe), \
                    patch.object(engine.subprocess, "run", side_effect=AssertionError("engine ran")):
                out = engine.call("perf", payload)
            self.assertEqual(out, {"ok": False, "verb": "perf", "class": "BAD_REQUEST"})

    def test_finite_request_is_still_sent(self):
        exe = Path(self._touch())
        seen = {}

        def fake_run(args, input=None, **_kwargs):
            seen["input"] = input
            return SimpleNamespace(returncode=0, stdout=b'{"ok": true, "verb": "ping"}')

        with patch.object(engine, "ENGINE_EXE", exe), patch.object(engine.subprocess, "run", side_effect=fake_run):
            out = engine.call("ping", {"name": "Ok", "n": 1.5})
        self.assertEqual(out, {"ok": True, "verb": "ping"})
        self.assertEqual(json.loads(seen["input"].decode("utf-8")), {"name": "Ok", "n": 1.5})
        self.assertNotIn(b"NaN", seen["input"])

    def _touch(self) -> str:
        handle = tempfile.NamedTemporaryFile(prefix="pbz-engine-", delete=False)
        handle.close()
        self.addCleanup(os.remove, handle.name)
        return handle.name


class ReloadTests(unittest.TestCase):
    def setUp(self):
        self.project = SimpleNamespace(name="Synthetic", pbip="Synthetic.pbip", ws=SimpleNamespace(private="p"))
        self.inst = {"pid": 123, "via": "bridge", "unsaved": False}

    def _reload(self, stdout: bytes, returncode: int = 0, unsaved: bool | None = False):
        inst = {**self.inst, "unsaved": unsaved}
        with patch.object(desktop, "find_instance", return_value=inst), \
                patch.object(desktop.os.path, "exists", return_value=True), \
                patch.object(desktop.subprocess, "run", return_value=SimpleNamespace(returncode=returncode, stdout=stdout)) as run:
            result = desktop.reload(self.project)
        return result, run

    def test_ok_true_still_reloads(self):
        result, run = self._reload(b'{"ok": true}')
        self.assertEqual(result, "RELOADED")
        run.assert_called_once()

    def test_empty_stdout_and_missing_ok_are_not_reloaded(self):
        for stdout in (b"", b"{}", b"null", b"[true]", b'{"ok": false}', b'{"ready": true}'):
            with self.subTest(stdout=stdout):
                result, _run = self._reload(stdout)
                self.assertEqual(result, "RELOAD_FAILED")

    def test_return_code_and_error_still_fail(self):
        self.assertEqual(self._reload(b'{"ok": true}', returncode=1)[0], "RELOAD_FAILED")
        self.assertEqual(self._reload(b'{"ok": true, "error": "BUSY"}')[0], "RELOAD_FAILED")

    def test_dirty_and_unknown_still_stop_before_the_bridge(self):
        for state, expected in ((True, "DESKTOP_DIRTY"), (None, "DESKTOP_STATE_UNKNOWN")):
            with self.subTest(state=state):
                result, run = self._reload(b'{"ok": true}', unsaved=state)
                self.assertEqual(result, expected)
                run.assert_not_called()


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pbz-cal-")
        self.addCleanup(self.tmp.cleanup)

    def _project(self, tables: dict[str, str | bytes]):
        root = Path(self.tmp.name) / "ws"
        model = root / "pbz-workspace" / "Synthetic.SemanticModel" / "definition" / "tables"
        model.mkdir(parents=True)
        (root / "Synthetic.pbip").write_text("{}\n", encoding="utf-8")
        for name, body in tables.items():
            data = body if isinstance(body, bytes) else body.encode("utf-8")
            (model / name).write_bytes(data)
        ws = common.Workspace(str(root))
        return ws, ws.project()

    def _bytes(self, project, filename: str) -> bytes:
        path = Path(project.mirror_model) / "definition" / "tables" / filename
        return path.read_bytes()

    def test_duplicate_name_on_the_table_is_refused_before_write(self):
        body = DATE + (
            "\tcalendar Order\n"
            "\t\tcalendarColumnGroup\n"
            "\t\t\tcolumn: Date\n"
            "\t\t\tunit: Day\n"
        )
        ws, project = self._project({"Date.tmdl": body})
        before = self._bytes(project, "Date.tmdl")
        with patch.object(calendars, "commit_bytes", side_effect=AssertionError("wrote")):
            with self.assertRaises(PbzError) as caught:
                calendars.add_calendar(ws, project, "Date", "order", "Date")
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertEqual(caught.exception.detail, "calendar already exists")
        self.assertEqual(self._bytes(project, "Date.tmdl"), before)

    def test_same_name_on_another_table_is_allowed(self):
        date = DATE + (
            "\tcalendar Order\n"
            "\t\tcalendarColumnGroup\n"
            "\t\t\tcolumn: Date\n"
            "\t\t\tunit: Day\n"
        )
        ship = (
            "table Ship\n"
            "\tcolumn Date\n"
            "\t\tdataType: dateTime\n"
            "\t\tsourceColumn: Date\n"
        )
        ws, project = self._project({"Date.tmdl": date, "Ship.tmdl": ship})
        added = calendars.add_calendar(ws, project, "Ship", "Order", "Date", unit="Month")
        self.assertEqual(added["table"], "Ship")
        self.assertEqual(added["name"], "Order")
        self.assertEqual(added["unit"], "Month")
        model = tmdl.load_model(os.path.join(project.mirror_model, "definition"))
        ship_node = model.tables["Ship"].node
        self.assertEqual(
            [node.name for node in ship_node.children if node.kind == "calendar"],
            ["Order"],
        )
        self.assertEqual(
            [node.name for node in model.tables["Date"].node.children if node.kind == "calendar"],
            ["Order"],
        )

    def test_newline_name_is_refused_before_write(self):
        raw = b"\xef\xbb\xbf" + DATE.replace("\n", "\r\n").encode("utf-8")
        ws, project = self._project({"Date.tmdl": raw})
        before = self._bytes(project, "Date.tmdl")
        with patch.object(calendars, "commit_bytes", side_effect=AssertionError("wrote")):
            with self.assertRaises(PbzError) as caught:
                calendars.add_calendar(ws, project, "Date", "Order\nLost", "Date")
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertEqual(caught.exception.detail, "names and expressions are a single line")
        self.assertEqual(self._bytes(project, "Date.tmdl"), before)
        self.assertNotIn(b"calendarColumnGroup", self._bytes(project, "Date.tmdl"))

    def test_readback_failure_restores_previous_bytes(self):
        ws, project = self._project({"Date.tmdl": DATE})
        before = self._bytes(project, "Date.tmdl")
        real = tmdl.load_model
        calls = {"n": 0}

        def blow(mdef, texts=None):
            calls["n"] += 1
            if calls["n"] > 1:
                raise RuntimeError("unreadable")
            return real(mdef, texts)

        with patch.object(calendars.tmdl, "load_model", side_effect=blow):
            with self.assertRaises(PbzError) as caught:
                calendars.add_calendar(ws, project, "Date", "Order", "Date")
        self.assertEqual(caught.exception.detail, "calendar block did not parse")
        self.assertEqual(self._bytes(project, "Date.tmdl"), before)

    def test_new_calendar_parses(self):
        ws, project = self._project({"Date.tmdl": DATE})
        added = calendars.add_calendar(ws, project, "Date", "Order", "Date", unit="Day")
        self.assertEqual(added["name"], "Order")
        model = tmdl.load_model(os.path.join(project.mirror_model, "definition"))
        calendar = next(node for node in model.tables["Date"].node.children if node.kind == "calendar")
        group = next(node for node in calendar.children if node.kind == "calendarColumnGroup")
        self.assertEqual(group.props.get("column", "").strip(), "Date")
        self.assertEqual(group.props.get("unit", "").strip(), "Day")

    def test_second_kpi_is_still_refused(self):
        ws, project = self._project({"Sales.tmdl": SALES})
        first = calendars.set_kpi(ws, project, "[Total Sales]", "1", None, None)
        self.assertEqual(first["set"], ["target"])
        before = self._bytes(project, "Sales.tmdl")
        with patch.object(calendars, "commit_bytes", side_effect=AssertionError("wrote")):
            with self.assertRaises(PbzError) as caught:
                calendars.set_kpi(ws, project, "[Total Sales]", "2", None, None)
        self.assertEqual(caught.exception.code, "BAD_REQUEST")
        self.assertEqual(caught.exception.detail, "kpi already exists")
        self.assertEqual(self._bytes(project, "Sales.tmdl"), before)
        self.assertIn(b"kpi", before)


if __name__ == "__main__":
    unittest.main()
