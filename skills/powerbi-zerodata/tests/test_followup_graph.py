"""Follow-up fixes: evidence tip, circular measures, perf disclosure, mirror restore, watch counts.

Run: python tests/test_followup_graph.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz.common import Workspace  # noqa: E402
from pbz import evidence, mirror_txn, perf, policy, tmdl, validate, watch  # noqa: E402

SENTINEL = "424242"


def _mac(ch: str) -> str:
    return ch * 64


def _record(mac: str) -> str:
    return json.dumps({"chain_mac": mac}) + "\n"


def _model(specs: list[tuple[str, str, str]]) -> tmdl.Model:
    model = tmdl.Model()
    tables: dict[str, tmdl.Table] = {}
    for table, name, expr in specs:
        if table not in tables:
            tables[table] = tmdl.Table(name=table)
            model.tables[table] = tables[table]
        tables[table].measures[name] = tmdl.Measure(table=table, name=name, expression=expr)
    return model


def _perf_body(**over) -> dict:
    body = {
        "ok": True,
        "timing": "BANDED",
        "duration_band": "100_500MS",
        "fe_share": "HIGH",
        "se_queries": "1_5",
        "dq_events": "0",
        "cache_hit": "SOME",
        "callback": False,
        "agg_rewrite": "MISS",
        "trace": "CAPTURED",
        "verdict": "SAME",
        "baseline": "MISSING",
        "plan": {
            "CallbackDataID": "1",
            "Spool_Iterator": "0",
            "CrossApply": "2_5",
            "LargeMaterialization": "GT_20",
        },
    }
    body.update(over)
    return body


class EvidenceTipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.root)
        self.ws = Workspace(self.root)
        self.active = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
        self.segs = os.path.join(self.ws.pbz_dir, "evidence.d")
        self.anchor = os.path.join(self.ws.pbz_dir, "evidence.chain.json")

    def _write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def test_active_mac_wins_over_newer_segment(self):
        self._write(os.path.join(self.segs, "0002.jsonl"), _record(_mac("a")) + _record(_mac("b")))
        self._write(self.active, _record(_mac("c")) + '{"legacy": true}\n')
        self.assertEqual(evidence._tip(self.ws), _mac("c"))

    def test_active_without_mac_uses_newest_segment_then_anchor(self):
        self._write(os.path.join(self.segs, "0001.jsonl"), _record(_mac("a")))
        self._write(os.path.join(self.segs, "0002.jsonl"), _record(_mac("b")) + _record(_mac("c")))
        self._write(self.active, '{"legacy": true}\n')
        self.assertEqual(evidence._tip(self.ws), _mac("c"))

        self._write(self.anchor, json.dumps({"anchor": _mac("e")}) + "\n")
        self._write(os.path.join(self.segs, "0002.jsonl"), '{"legacy": true}\n')
        self._write(self.active, "\n")
        self.assertEqual(evidence._tip(self.ws), _mac("e"))

    def test_missing_active_still_uses_newest_segment(self):
        self._write(os.path.join(self.segs, "0001.jsonl"), _record(_mac("a")))
        self._write(os.path.join(self.segs, "0002.jsonl"), _record(_mac("d")))
        self.assertFalse(os.path.lexists(self.active))
        self.assertEqual(evidence._tip(self.ws), _mac("d"))

    def test_empty_log_uses_the_anchor(self):
        self.assertEqual(evidence._tip(self.ws), evidence.GENESIS)


class CircularDependencyTests(unittest.TestCase):
    def test_path_through_a_finished_cycle_and_a_self_reference(self):
        # M1-M3 is a cycle. M4 points at M2, and M1 points back at M4, so M4 is on a cycle
        # even though a gray-stack walk can finish M1-M3 before it sees M4. M5 only points in.
        model = _model([
            ("Sales", "M1", f"[M2] + [M4] + {SENTINEL}"),
            ("Sales", "M2", f"[M3] + {SENTINEL}"),
            ("Sales", "M3", f"[M1] + {SENTINEL}"),
            ("Sales", "M4", f"[M2] + {SENTINEL}"),
            ("Sales", "M5", f"[M2] + {SENTINEL}"),
            ("Sales", "Solo", f"[Solo] + {SENTINEL}"),
            ("Sales", "Plain", SENTINEL),
        ])
        found = validate.circular_dependencies(model)
        blob = json.dumps(found)
        self.assertNotIn(SENTINEL, blob)
        self.assertNotIn("expression", blob)
        self.assertEqual([item["where"] for item in found], [
            "Sales[M1]", "Sales[M2]", "Sales[M3]", "Sales[M4]", "Sales[Solo]",
        ])
        for item in found:
            self.assertEqual(item["code"], "CIRCULAR_DEPENDENCY")
            self.assertEqual(item["severity"], "error")
            self.assertEqual(set(item), {"severity", "code", "where", "message"})
            name = item["where"][len("Sales["):-1]
            self.assertEqual(item["message"], f"[{name}] is in a circular measure dependency")


class PerfDisclosureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.root)
        self.ws = Workspace(self.root)

    def _strict(self) -> None:
        pol = policy.load(self.ws)
        pol["disclosure"] = "strict"
        policy.save(self.ws, pol)

    def test_shape_keeps_bands_and_drops_unknown_baseline(self):
        out = perf._closed(_perf_body(baseline="NO_BASELINE"), self.ws)
        self.assertEqual(out["duration_band"], "100_500MS")
        self.assertEqual(out["fe_share"], "HIGH")
        self.assertEqual(out["se_queries"], "1_5")
        self.assertEqual(out["dq_events"], "0")
        self.assertEqual(out["cache_hit"], "SOME")
        self.assertEqual(out["plan"]["CallbackDataID"], "1")
        self.assertEqual(out["timing"], "BANDED")
        self.assertIs(out["callback"], False)
        self.assertEqual(out["agg_rewrite"], "MISS")
        self.assertEqual(out["trace"], "CAPTURED")
        self.assertEqual(out["verdict"], "SAME")
        self.assertNotIn("baseline", out)
        self.assertNotIn("status", out)
        self.assertNotIn("NO_BASELINE", json.dumps(out))

    def test_strict_drops_magnitude_bands_only(self):
        self._strict()
        out = perf._closed(_perf_body(), self.ws)
        self.assertEqual(set(out), {
            "verb", "timing", "callback", "agg_rewrite", "trace", "verdict", "baseline",
        })
        self.assertEqual(out["timing"], "BANDED")
        self.assertIs(out["callback"], False)
        self.assertEqual(out["agg_rewrite"], "MISS")
        self.assertEqual(out["trace"], "CAPTURED")
        self.assertEqual(out["verdict"], "SAME")
        self.assertEqual(out["baseline"], "MISSING")
        self.assertNotIn("NO_BASELINE", json.dumps(out))
        self.assertNotIn("status", out)

    def test_run_uses_workspace_policy(self):
        self._strict()
        with patch("pbz.perf.engine_call", return_value=_perf_body(verdict="SLOWER", baseline="SAVED")), \
                patch("pbz.perf.desktop.require_port", return_value=1), \
                patch("pbz.perf.checks.spend", return_value=True):
            out = perf.run(self.ws, object(), "EVALUATE {1}")
        self.assertEqual(out["verdict"], "SLOWER")
        self.assertEqual(out["baseline"], "SAVED")
        self.assertNotIn("duration_band", out)
        self.assertNotIn("plan", out)
        self.assertNotIn("fe_share", out)


class _Cut(BaseException):
    pass


class MirrorTransactionTests(unittest.TestCase):
    def test_base_exception_restores_and_reraises(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "tree")
        os.makedirs(os.path.join(root, "sub"))
        kept = os.path.join(root, "sub", "kept.txt")
        prior = b"prior-bytes\n"
        with open(kept, "wb") as handle:
            handle.write(prior)
        created = os.path.join(root, "created.txt")
        boom = _Cut("stop")

        def fn():
            with open(kept, "wb") as handle:
                handle.write(b"changed\n")
            with open(created, "wb") as handle:
                handle.write(b"new\n")
            raise boom

        with self.assertRaises(_Cut) as caught:
            mirror_txn.transaction(root, fn)
        self.assertIs(caught.exception, boom)
        with open(kept, "rb") as handle:
            self.assertEqual(handle.read(), prior)
        self.assertFalse(os.path.lexists(created))

    def test_success_keeps_bytes_and_return_value(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "tree")
        os.makedirs(root)
        kept = os.path.join(root, "kept.txt")
        with open(kept, "wb") as handle:
            handle.write(b"prior\n")

        def fn():
            with open(kept, "wb") as handle:
                handle.write(b"changed\n")
            return 7

        self.assertEqual(mirror_txn.transaction(root, fn), 7)
        with open(kept, "rb") as handle:
            self.assertEqual(handle.read(), b"changed\n")


class WatchCountTests(unittest.TestCase):
    def test_report_findings_add_to_model_and_once_stays_single(self):
        findings = [{"severity": "error"}, {"severity": "warning"}]
        rules = {"model": {"findings": 2}, "report": {"findings": 3}}
        with patch("pbz.watch.validate.run_mirror", return_value=findings), \
                patch("pbz.watch.bpa.run", return_value=rules) as bpa_run, \
                patch("pbz.watch.time.sleep") as sleep:
            once = watch.run(object(), object(), once=True, seconds=9)
        self.assertEqual(once, {"verb": "watch", "once": True, "passes": [
            {"validate": 2, "validate_errors": 1, "bpa": 5},
        ]})
        self.assertEqual(bpa_run.call_args.args[2], "all")
        sleep.assert_not_called()

        with patch("pbz.watch.validate.run_mirror", return_value=findings), \
                patch("pbz.watch.bpa.run", return_value={"model": {"findings": 4}}) as bpa_run, \
                patch("pbz.watch.time.sleep") as sleep:
            polled = watch.run(object(), object(), once=False, seconds=9)
        self.assertEqual(len(polled["passes"]), 2)
        self.assertFalse(polled["once"])
        self.assertEqual(polled["passes"][0]["bpa"], 4)
        self.assertEqual(polled["passes"][1]["bpa"], 4)
        sleep.assert_called_once_with(9)
        self.assertEqual(bpa_run.call_count, 2)

        with patch("pbz.watch.validate.run_mirror", return_value=[]), \
                patch("pbz.watch.bpa.run", return_value={"report": {"findings": 3}}), \
                patch("pbz.watch.time.sleep"):
            report_only = watch.run(object(), object(), once=True)
        self.assertEqual(report_only["passes"][0]["bpa"], 3)


if __name__ == "__main__":
    unittest.main()
