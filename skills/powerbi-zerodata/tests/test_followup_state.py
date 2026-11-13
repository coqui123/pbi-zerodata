"""Follow-up fixes for check state, strict memory bands, column arity, and props.get.

Run: python tests/test_followup_state.py
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz import checks, common, daxcatalog, memory, policy, props, tmdl  # noqa: E402
from pbz.common import PbzError  # noqa: E402

SECRET = "SECRET_LEDGER_PAYLOAD"
MARKER = "ARITY_MARKER_zz"


def _bytes(path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _text(path) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _keys(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        found.update(value)
        for item in value.values():
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)
    return found


MEMORY = {
    "model": {"size_band": "<1 MiB"},
    "tables": [{
        "name": "Sales",
        "size_band": "1-10 MiB",
        "share_band": ">=20%",
        "columns": [{
            "name": "Amount",
            "size_band": "<1 MiB",
            "share_band": "<1%",
            "dictionary_heavy": False,
            "encoding": "Hash",
            "segment_band": "1-9",
            "ri": "RI_OK",
            "ri_count": "0",
        }],
        "hierarchies": [{"name": "Cal", "size_band": "<1 MiB", "segment_band": "10-99"}],
    }],
    "relationships": [{
        "name": "SalesDate",
        "from": "Sales[Date]",
        "to": "Date[Date]",
        "size_band": "<1 MiB",
        "ri": "RI_VIOLATION",
        "ri_count": "1-9",
    }],
    "coverage": "FULL",
    "predicates": "DONE",
}


class FollowupStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.root)
        with open(os.path.join(self.root, "Demo.pbip"), "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        self.ws = common.Workspace(self.root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        self.project = self.ws.project()
        self.state = os.path.join(self.ws.private, "state.json")

    def _write_state(self, text: str) -> None:
        os.makedirs(self.ws.private, exist_ok=True)
        with open(self.state, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def test_missing_state_is_a_fresh_budget(self):
        self.assertFalse(os.path.exists(self.state))
        st = checks._load_state(self.ws)
        self.assertEqual(st["day"], dt.date.today().isoformat())
        self.assertEqual(st["checks"], 0)
        self.assertEqual(st["probes"], 0)
        self.assertEqual(st["ledger"]["authored"], [])
        self.assertEqual(st["ledger"]["constants"], {})
        self.assertFalse(os.path.exists(self.state))
        self.assertTrue(checks.spend(self.ws, "probes"))
        self.assertEqual(checks._load_state(self.ws)["probes"], 1)

    def test_valid_ledger_survives_load(self):
        today = dt.date.today().isoformat()
        yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
        self._write_state(json.dumps({
            "day": yesterday, "checks": 9, "probes": 4,
            "ledger": {"constants": {"target": [{"h": "abc123", "day": yesterday}]}, "authored": ["feedface"]},
        }))
        before = _bytes(self.state)
        st = checks._load_state(self.ws)
        self.assertEqual(st["day"], today)
        self.assertEqual(st["checks"], 0)
        self.assertEqual(st["probes"], 0)
        self.assertEqual(st["ledger"]["authored"], ["feedface"])
        self.assertEqual(st["ledger"]["constants"]["target"][0]["h"], "abc123")
        self.assertEqual(_bytes(self.state), before)
        self._write_state(json.dumps({
            "day": today, "checks": 4, "probes": 2, "constants": {"target": ["h1"]},
            "ledger": {"constants": {}, "authored": ["ab"]},
        }))
        kept = checks._load_state(self.ws)
        self.assertEqual(kept["checks"], 4)
        self.assertEqual(kept["probes"], 2)
        self.assertEqual(kept["ledger"]["authored"], ["ab"])
        self.assertEqual(kept["ledger"]["constants"]["target"][0], {"h": "h1", "day": today})

    def test_corrupt_state_is_refused_and_not_reset(self):
        bodies = [
            "{" + json.dumps({"note": SECRET}),
            "null",
            "[]",
            "1",
            '"' + SECRET + '"',
        ]
        details = []
        for body in bodies:
            self._write_state(body)
            before = _bytes(self.state)
            with self.assertRaises(PbzError) as caught:
                checks.spend(self.ws, "probes")
            err = caught.exception
            self.assertEqual(err.code, "REFUSED")
            self.assertEqual(err.detail, checks._STATE_MALFORMED)
            self.assertNotIn(SECRET, err.detail)
            self.assertNotIn(SECRET, str(err))
            context = "" if err.__context__ is None else repr(err.__context__)
            self.assertNotIn(SECRET, context)
            self.assertIsNone(err.__cause__)
            self.assertEqual(_bytes(self.state), before)
            details.append(err.detail)
        self.assertEqual(len(set(details)), 1)
        with open(self.state, "wb") as handle:
            handle.write(b"\xff" + SECRET.encode("ascii"))
        before = _bytes(self.state)
        with self.assertRaises(PbzError) as caught:
            checks._load_state(self.ws)
        self.assertEqual(caught.exception.detail, checks._STATE_MALFORMED)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(_bytes(self.state), before)

    def test_unreadable_existing_state_is_refused_and_missing_stays_fresh(self):
        payload = b'{"checks": 7, "marker": "KEEP_BYTES"}\n'
        os.makedirs(self.ws.private, exist_ok=True)
        with open(self.state, "wb") as handle:
            handle.write(payload)
        before = _bytes(self.state)
        message = "state file is locked"

        def locked_read(path, *, root, max_bytes=None):
            if os.path.abspath(path) == os.path.abspath(self.state):
                raise OSError(message)
            return common.read_checked(path, root=root, max_bytes=max_bytes)

        with patch("pbz.checks.read_checked", locked_read):
            with self.assertRaises(PbzError) as caught:
                checks.spend(self.ws, "probes")
        err = caught.exception
        self.assertEqual(err.code, "REFUSED")
        self.assertEqual(err.detail, checks._STATE_MALFORMED)
        self.assertNotIn(message, err.detail)
        self.assertNotIn(message, str(err))
        context = "" if err.__context__ is None else repr(err.__context__)
        self.assertNotIn(message, context)
        self.assertIsNone(err.__cause__)
        self.assertEqual(_bytes(self.state), before)

        os.remove(self.state)
        st = checks._load_state(self.ws)
        self.assertEqual(st["day"], dt.date.today().isoformat())
        self.assertEqual(st["checks"], 0)
        self.assertEqual(st["probes"], 0)
        self.assertEqual(st["ledger"]["authored"], [])
        self.assertEqual(st["ledger"]["constants"], {})
        self.assertFalse(os.path.exists(self.state))

    def test_failed_stat_does_not_load_a_fresh_budget(self):
        today = dt.date.today().isoformat()
        self._write_state(json.dumps({
            "day": today, "checks": 7, "probes": 3,
            "ledger": {"constants": {}, "authored": ["KEEP_BYTES"]},
        }))
        before = _bytes(self.state)
        with patch("pbz.checks.os.path.lexists", return_value=False):
            st = checks._load_state(self.ws)
        self.assertEqual(st["checks"], 7)
        self.assertEqual(st["probes"], 3)
        self.assertEqual(st["ledger"]["authored"], ["KEEP_BYTES"])
        self.assertEqual(_bytes(self.state), before)

    def test_save_state_replaces_a_temp_file_in_the_same_directory(self):
        self._write_state(json.dumps({"note": "OLD_LEDGER_MARKER", "checks": 1}))
        seen = {}
        real_replace = os.replace

        def watch(src, dst):
            seen["src"] = src
            seen["dst"] = dst
            seen["src_text"] = _text(src)
            seen["dst_before"] = _bytes(dst)
            return real_replace(src, dst)

        with patch("pbz.checks.os.replace", watch):
            checks._save_state(self.ws, {"checks": 3, "marker": "NEW"})
        self.assertEqual(os.path.dirname(os.path.abspath(seen["src"])), os.path.dirname(os.path.abspath(seen["dst"])))
        self.assertEqual(os.path.abspath(seen["dst"]), os.path.abspath(self.state))
        self.assertNotEqual(os.path.abspath(seen["src"]), os.path.abspath(seen["dst"]))
        self.assertIn("OLD_LEDGER_MARKER", seen["dst_before"].decode("utf-8"))
        self.assertEqual(json.loads(seen["src_text"])["checks"], 3)
        self.assertFalse(os.path.exists(seen["src"]))
        saved = json.loads(_text(self.state))
        self.assertEqual(saved["checks"], 3)
        self.assertNotIn("OLD_LEDGER_MARKER", _text(self.state))
        self.assertEqual(os.listdir(self.ws.private), ["state.json"])

        def dump(obj, fp, *args, **kwargs):
            fp.write("PARTIAL_SECRET_9f3a")
            raise OSError("disk")

        with patch("pbz.checks.json.dump", dump):
            with self.assertRaises(OSError):
                checks._save_state(self.ws, {"checks": 9})
        text = _text(self.state)
        self.assertIn("NEW", text)
        self.assertNotIn("PARTIAL_SECRET_9f3a", text)
        self.assertEqual([name for name in os.listdir(self.ws.private) if name != "state.json"], [])

    def test_strict_strip_drops_segment_and_ri_count_only(self):
        nested = {"rows": [[{"segment_band": "0", "encoding": "Value", "ri": "RI_OK",
                             "ri_count": "1-9", "size_band": "n", "share_band": "n"}]]}
        stripped = memory._strip_bands(nested)
        self.assertEqual(stripped, {"rows": [[{"encoding": "Value", "ri": "RI_OK"}]]})
        self.assertIn("segment_band", nested["rows"][0][0])
        kept = memory.closed(MEMORY)
        self.assertEqual(kept["tables"][0]["columns"][0]["segment_band"], "1-9")
        self.assertEqual(kept["tables"][0]["columns"][0]["ri_count"], "0")
        gone = memory._strip_bands(kept)
        names = _keys(gone)
        self.assertFalse(names & {"size_band", "share_band", "segment_band", "ri_count"})
        self.assertEqual(gone["tables"][0]["columns"][0]["encoding"], "Hash")
        self.assertEqual(gone["tables"][0]["columns"][0]["ri"], "RI_OK")
        self.assertEqual(gone["relationships"][0]["ri"], "RI_VIOLATION")
        pol = policy.load(self.ws)
        pol["disclosure"] = "strict"
        policy.save(self.ws, pol)
        path = memory._path(self.ws)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        doc = {"verb": "memory", "project": self.project.name, "disclosure": "shape",
               "saved_at": "2026-01-01T00:00:00", "mirror_rev": "m0123456789ab", **MEMORY}
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        loaded = memory.load(self.ws, self.project)
        loaded_keys = _keys(loaded)
        self.assertFalse(loaded_keys & {"size_band", "share_band", "segment_band", "ri_count"})
        self.assertEqual(loaded["disclosure"], "strict")
        self.assertEqual(loaded["tables"][0]["columns"][0]["encoding"], "Hash")
        self.assertEqual(loaded["tables"][0]["columns"][0]["ri"], "RI_OK")
        self.assertEqual(loaded["relationships"][0]["ri"], "RI_VIOLATION")

    def _sales_model(self):
        definition = os.path.join(self.tmp.name, "definition", "tables")
        os.makedirs(definition)
        text = (
            "table Sales\n"
            "\n"
            "\tcolumn Amount\n"
            "\t\tdataType: double\n"
            "\n"
            "\tcolumn Hours = SUM()\n"
            "\t\tdataType: int64\n"
            "\n"
            "\tcolumn Year = YEAR('Date'[Date])\n"
            "\n"
            "\tcolumn Secret = SUMX(" + MARKER + ")\n"
            "\n"
            "\tcolumn Late = SUM()\n"
            "\t\tformatString = FORMAT(1)\n"
            "\n"
            "\tmeasure Total = RELATED()\n"
            "\n"
            "\tmeasure Mystery = NopeFunc(1)\n"
            "\n"
            "\tmeasure Ok = SUM(Sales[Amount])\n"
        )
        with open(os.path.join(definition, "Sales.tmdl"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        model = tmdl.load_model(os.path.join(self.tmp.name, "definition"))
        model.tables["Sales"].columns["Ghost"] = tmdl.Column("Sales", "Ghost", calculated=True)
        node = tmdl.Node("column", "Plain", 1, 0, file="tables/Sales.tmdl")
        model.tables["Sales"].columns["Plain"] = tmdl.Column("Sales", "Plain", calculated=True, node=node)
        return model

    def test_calculated_column_arity_uses_the_dax_region(self):
        model = self._sales_model()
        hours = model.column("Sales", "Hours")
        self.assertTrue(hours.calculated)
        self.assertFalse(hasattr(hours, "expression"))
        found = daxcatalog.findings(model)
        blob = json.dumps(found)
        self.assertNotIn(MARKER, blob)
        self.assertNotIn("SUM()", blob)
        self.assertNotIn("FORMAT(1)", blob)
        for item in found:
            self.assertTrue(set(item) <= {"severity", "code", "object", "message"})
            self.assertNotIn("expression", item)
        by_object = {}
        for item in found:
            by_object.setdefault(item["object"], []).append(item["message"])
        self.assertEqual(by_object["'Sales'[Hours]"], ["SUM has the wrong number of arguments"])
        self.assertEqual(by_object["'Sales'[Secret]"], ["SUMX has the wrong number of arguments"])
        self.assertEqual(by_object["'Sales'[Late]"], ["SUM has the wrong number of arguments"])
        self.assertEqual(by_object["[Total]"], ["RELATED has the wrong number of arguments"])
        for name in ("'Sales'[Year]", "'Sales'[Amount]", "'Sales'[Ghost]", "'Sales'[Plain]", "[Mystery]", "[Ok]"):
            self.assertNotIn(name, by_object)

    def test_props_get_refuses_a_missing_mirror_before_load(self):
        def grab(fn):
            try:
                fn()
            except PbzError as err:
                return err.code, err.detail
            return None

        missing = grab(lambda: props.get(self.ws, self.project, "[Total]"))
        self.assertEqual(missing, grab(lambda: props.run(self.ws, self.project, "[Total]", ["isHidden=true"])))
        self.assertEqual(missing, ("NO_MIRROR", "run `pbz pull` first"))
        with patch("pbz.props.tmdl.load_model", side_effect=AssertionError("loaded")):
            with self.assertRaises(PbzError) as caught:
                props.get(self.ws, self.project)
        self.assertEqual(caught.exception.code, "NO_MIRROR")
        os.makedirs(os.path.join(self.project.mirror_model, "definition"))
        with self.assertRaises(PbzError) as caught:
            props.get(self.ws, self.project, "[Missing]")
        self.assertEqual(caught.exception.code, "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
