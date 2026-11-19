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
from pbz import cli, evidence, governance, images, mcp, mirror_txn, perf, policy, ruleset, tmdl, usage, validate, watch  # noqa: E402

SENTINEL = "424242"


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


class LocatedBindingTests(unittest.TestCase):
    def model(self, measure=False):
        model = tmdl.load_model("", texts={
            "tables/Sales.tmdl": "table Sales\n\tcolumn Amount\n\tcolumn OnlyHere\n",
            "tables/Other.tmdl": "table Other\n\tcolumn Amount\n",
            "tables/Home.tmdl": "table Home\n" + ("\tmeasure Amount = 1\n" if measure else ""),
        })
        return model

    def test_dax_golden_occurrence_offsets(self):
        from pbz import deps, rename, changes
        # «...» here marks independently selected golden spans, removed before
        # resolution. No expected offset is derived from deps or rename output.
        fixtures = [
            ("«[Amount]» + Sales«[Amount]»", "Sales", False, False),
            ("SUMX(Sales, «[Amount]» + SUMX(Other, [Amount]) + «[Amount]»)", "Home", False, False),
            ("SUMX(Other, [Amount]) + «[Amount]»", "Sales", False, False),
            ("«[OnlyHere]»", "Home", False, False),
            ("Sales«[Amount]» > 0 && «[Amount]» > 0", "Sales", True, False),
            ("[Amount] + Sales«[Amount]» + Other[Amount]", "Sales", False, True),
            ("Other[Amount] + Sales«[Amount]»", "Sales", False, False),
            ("Home[Amount] + Sales«[Amount]»", "Sales", False, True),
            ("Other[Amount] + «[Amount]»", "Sales", True, True),
        ]
        for marked, home, row, measure in fixtures:
            with self.subTest(fixture=marked):
                text, expected, opened = "", [], None
                for ch in marked:
                    if ch == "«":
                        opened = len(text)
                    elif ch == "»":
                        expected.append((opened, len(text)))
                    else:
                        text += ch
                res = deps.Resolver(self.model(measure))
                old = "OnlyHere" if "OnlyHere" in text else "Amount"
                info = deps.dax_bindings(text, home, res, row_home=row, base=7)
                found = [(b.start - 7, b.end - 7) for b in info.bindings
                         if b.ref == deps.Ref("column", "Sales", old)]
                self.assertEqual(found, expected)
                edits = rename.dax_ref_edits("prefix!" + text, 7, 7 + len(text), "column",
                                             "Sales", old, "New", home, res=res, row_home=row)
                self.assertEqual(edits, [(a + 7, b + 7, "[New]") for a, b in expected])
                labels = changes._refs("dax", text, home, res, "tablePermission" if row else "measure")
                self.assertEqual("Sales[" + old + "]" in labels, bool(expected))

    def test_region_context_rls_and_function_original_offsets(self):
        from pbz import deps, rename
        res = deps.Resolver(self.model(True))
        text = "role Reader\n\ttablePermission Sales = [Amount] > 0 && Sales[Amount] > 0\n"
        doc = tmdl.parse("roles/Reader.tmdl", text)
        reg = next(r for r in doc.regions if r.kind == "dax")
        bindings = deps.dax_region(text, reg, res).bindings
        self.assertEqual(rename._binding_edits(bindings, "column", "Sales", "Amount", "New"),
                         [(text.index("[Amount]"), text.index("[Amount]") + 8, "[New]"),
                          (text.rindex("[Amount]"), text.rindex("[Amount]") + 8, "[New]")])
        expr = "(x: scalar) => Sales[Amount] + x"
        binding = deps.dax_bindings(expr, "", res, function=True, base=10).bindings[0]
        self.assertEqual((binding.start, binding.end), (30, 38))

    def test_unresolved_rename_candidate_refused(self):
        from pbz import deps, rename
        from pbz.common import PbzError
        for expr, home in (("[Amount]", ""), ("Missing[Amount]", "Sales")):
            with self.subTest(expr=expr), self.assertRaises(PbzError) as caught:
                rename.dax_ref_edits(expr, 0, len(expr), "column", "Sales", "Amount", "New",
                                     home, res=deps.Resolver(self.model()))
            self.assertEqual(caught.exception.code, "AMBIGUOUS")

    def test_semantic_preview_preserves_rls_home_context(self):
        from pbz import changes, deps
        from types import SimpleNamespace
        model = self.model(True)
        prefix = "Mini.SemanticModel/definition/"
        texts = {prefix + rel: doc.text for rel, doc in model.docs.items()}
        role = prefix + "roles/Reader.tmdl"
        texts[role] = "role Reader\n\ttablePermission Sales = [Amount] > 0\n"
        before, ma = changes._model_side(texts, SimpleNamespace(name="Mini"))
        texts[role] = "role Reader\n\ttablePermission Sales = Other[Amount] > 0\n"
        after, mb = changes._model_side(texts, SimpleNamespace(name="Mini"))
        result = changes._compare_model(before, after, deps.Resolver(ma), deps.Resolver(mb))
        rule = next(r for r in result if r["kind"] == "tablePermission")
        self.assertEqual(rule["expressions"][0]["refs_removed"], ["Sales[Amount]"])
        self.assertEqual(rule["expressions"][0]["refs_added"], ["Other[Amount]"])

    def test_m_tracker_scopes_and_case(self):
        from pbz import deps
        model = tmdl.Model()
        model.expressions = {name: None for name in ("Sales", "Other", "Source")}
        res = deps.Resolver(model)
        fixtures = [
            ("if Sales = Other then Sales else Other", ["Sales", "Other", "Sales", "Other"]),
            ("let x = (let Sales = 1 in Sales), y = Sales in y", ["Sales"]),
            ("let Sales = Other in Sales", ["Other"]),
            ("let r = [Sales = 1, x = Sales], y = Sales in y", ["Sales"]),
            ("let source = 1 in Source", ["Source"]),
            ("let Sales = 1 in Sales", []),
            ("let r = [Sales = 1, x = Sales] in r", []),
            ("let Sales = Sales in Sales", ["Sales"]),
            ("[Sales = Sales, x = Sales]", ["Sales"]),
            ("let Sales = 1 in (let Sales = Sales in Sales)", []),
            ("(Sales as number) => Sales + Other", ["Other"]),
            ("each [Sales] + Sales + _", ["Sales"]),
            ('let r = [#"Sales" = 1, x = #"Sales"] in Sales', ["Sales"]),
            ("if true then let Sales = 1 in Sales else Sales", ["Sales"]),
            ("let f = (Sales) => Sales, x = Sales in x", ["Sales"]),
        ]
        for expr, expected in fixtures:
            with self.subTest(expr=expr):
                bindings = deps.m_bindings(expr, res, base=5)
                self.assertEqual([b.ref.name for b in bindings], expected)
                self.assertFalse(any(b.uncertain for b in bindings))
                self.assertEqual([expr[b.start-5:b.end-5].removeprefix('#"').removesuffix('"') for b in bindings], expected)
                self.assertEqual([r.name for r in deps.m(expr, res)], list(dict.fromkeys(expected)))
        for expr, expected in (("let Sales = 1", ["Sales"]),
                               ("try let Sales = 1 in Sales otherwise Sales", ["Sales"] * 3)):
            uncertain = deps.m_bindings(expr, res)
            self.assertEqual([b.ref.name for b in uncertain], expected)
            self.assertTrue(all(b.uncertain for b in uncertain))


class EvidenceTipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.root)
        self.ws = Workspace(self.root)
        self.ws.private = os.path.join(self.tmp.name, "private")
        self.key = evidence.dpapi.load_key(self.ws.private)
        self.macs = {}
        self.active = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
        self.segs = os.path.join(self.ws.pbz_dir, "evidence.d")
        self.anchor = os.path.join(self.ws.pbz_dir, "evidence.chain.json")

    def _mac(self, name: str) -> str:
        return self.macs[name]

    def _record(self, name: str, prev: str = evidence.GENESIS) -> str:
        entry = {"fixture": name, "chain_prev": prev}
        entry["chain_mac"] = evidence._mac(self.key, prev, entry)
        self.macs[name] = entry["chain_mac"]
        return json.dumps(entry) + "\n"

    def _write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def test_active_mac_wins_over_newer_segment(self):
        self._write(os.path.join(self.segs, "0002.jsonl"),
                    self._record("a") + self._record("b", self._mac("a")))
        self._write(self.active, self._record("c", self._mac("b")) + '{"legacy": true}\n')
        self.assertEqual(evidence._tip(self.ws), self._mac("c"))

    def test_active_without_mac_uses_newest_segment_then_anchor(self):
        self._write(os.path.join(self.segs, "0001.jsonl"), self._record("a"))
        self._write(os.path.join(self.segs, "0002.jsonl"),
                    self._record("b", self._mac("a")) + self._record("c", self._mac("b")))
        self._write(self.active, '{"legacy": true}\n')
        self.assertEqual(evidence._tip(self.ws), self._mac("c"))

        # Model a retained older segment following an authenticated, archived
        # prefix. It must authenticate, but cannot supply the newest segment's tip.
        self._record("e")
        self._write(self.anchor, json.dumps({"anchor": self._mac("e")}) + "\n")
        self._write(os.path.join(self.segs, "0001.jsonl"), self._record("a", self._mac("e")))
        self._write(os.path.join(self.segs, "0002.jsonl"), '{"legacy": true}\n')
        self._write(self.active, "\n")
        self.assertEqual(evidence._tip(self.ws), self._mac("e"))

    def test_missing_active_still_uses_newest_segment(self):
        self._write(os.path.join(self.segs, "0001.jsonl"), self._record("a"))
        self._write(os.path.join(self.segs, "0002.jsonl"), self._record("d", self._mac("a")))
        self.assertFalse(os.path.lexists(self.active))
        self.assertEqual(evidence._tip(self.ws), self._mac("d"))

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

    def test_check_identity_and_hard_ratio_fail_closed(self):
        from pbz import common
        project = common.Project(self.ws, "One")
        with patch("pbz.perf.desktop.require_port", return_value=1), \
             patch("pbz.perf.checks.spend", return_value=True), \
             patch("pbz.perf.engine_call") as engine:
            engine.return_value = _perf_body(verdict="SAME", ratio_verdict="WITHIN_LIMIT")
            self.assertEqual(perf.expect(self.ws, project, "EVALUATE {1}", check_id="a:b")["status"], "PASS")
            first = engine.call_args.args[1]
            self.assertEqual(first["baseline"], "a:b")
            for verdict in ("NOISY", "SAME"):
                engine.return_value = _perf_body(verdict=verdict, ratio_verdict="OVER_LIMIT")
                self.assertEqual(perf.expect(self.ws, project, "EVALUATE {1}", check_id="other")["status"], "FAIL")
            engine.return_value = _perf_body(verdict="SAME")
            self.assertEqual(perf.expect(self.ws, project, "EVALUATE {1}")["status"], "ERROR")
            engine.return_value = {"ok": True, "baseline": "MISSING", "query": "secret", "workload": "private-fingerprint"}
            self.assertEqual(perf.expect(self.ws, project, "EVALUATE {2}")["status"], "ERROR")
            out = perf.run(self.ws, common.Project(self.ws, "Two"), "EVALUATE {1}", baseline="a:b", action="save")
            self.assertNotEqual(first["project_identity"], engine.call_args.args[1]["project_identity"])
            self.assertNotIn("query", out)
            self.assertNotIn("workload", out)
            self.assertNotIn("fingerprint", str(out))

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
        root = os.path.join(tmp.name, "pbz-workspace", "Txn.SemanticModel")
        os.makedirs(os.path.join(root, "sub"))
        kept = os.path.join(root, "sub", "kept.txt")
        prior = b"prior-bytes\n"
        with open(kept, "wb") as handle:
            handle.write(prior)
        created = os.path.join(root, "created.txt")
        boom = _Cut("stop")

        from pbz.common import Workspace, Project
        ws = Workspace(tmp.name)
        ws.private = os.path.join(tmp.name, "private")
        project = Project(ws, "Txn")

        def fn():
            from pbz.common import commit_bytes
            commit_bytes([("write_bytes", kept, b"changed\n"), ("write_bytes", created, b"new\n")], root=root)
            raise boom

        with self.assertRaises(_Cut) as caught:
            mirror_txn.transaction(root, fn, ws=ws, project=project)
        self.assertIs(caught.exception, boom)
        with open(kept, "rb") as handle:
            self.assertEqual(handle.read(), prior)
        self.assertFalse(os.path.lexists(created))

    def test_success_keeps_bytes_and_return_value(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = os.path.join(tmp.name, "pbz-workspace", "Txn.SemanticModel")
        os.makedirs(root)
        kept = os.path.join(root, "kept.txt")
        with open(kept, "wb") as handle:
            handle.write(b"prior\n")

        from pbz.common import Workspace, Project
        ws = Workspace(tmp.name)
        ws.private = os.path.join(tmp.name, "private")
        project = Project(ws, "Txn")

        def fn():
            from pbz.common import commit_bytes
            commit_bytes([("write_bytes", kept, b"changed\n")], root=root)
            return 7

        self.assertEqual(mirror_txn.transaction(root, fn, ws=ws, project=project), 7)
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

        calls = {"n": 0}

        def stop_after_two(*_args):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise KeyboardInterrupt

        with patch("pbz.watch.validate.run_mirror", return_value=findings), \
                patch("pbz.watch.bpa.run", return_value={"model": {"findings": 4}}) as bpa_run, \
                patch("pbz.watch.time.sleep", side_effect=stop_after_two) as sleep:
            polled = watch.run(object(), object(), once=False, seconds=9)
        self.assertEqual(len(polled["passes"]), 2)
        self.assertFalse(polled["once"])
        self.assertEqual(polled["passes"][0]["bpa"], 4)
        self.assertEqual(polled["passes"][1]["bpa"], 4)
        self.assertEqual(sleep.call_count, 2)
        sleep.assert_called_with(9)
        self.assertEqual(bpa_run.call_count, 2)

        with patch("pbz.watch.validate.run_mirror", return_value=[]), \
                patch("pbz.watch.bpa.run", return_value={"report": {"findings": 3}}), \
                patch("pbz.watch.time.sleep"):
            report_only = watch.run(object(), object(), once=True)
        self.assertEqual(report_only["passes"][0]["bpa"], 3)


class PlanFixTests(unittest.TestCase):
    def test_vendor_host_is_a_domain_not_a_suffix(self):
        self.assertTrue(cli._vendor_host("learn.microsoft.com"))
        self.assertTrue(cli._vendor_host("microsoft.com"))
        self.assertTrue(cli._vendor_host("www.w3.org"))
        self.assertFalse(cli._vendor_host("notmicrosoft.com"))
        self.assertFalse(cli._vendor_host("evilschema.org.example"))

    def test_crlf_ref_insert_does_not_split_the_line_ending(self):
        text = "ref table Sales\r\n"
        out = governance._add_ref_text(text, "table", "Date")
        self.assertNotIn("\r\r\n", out)
        self.assertIn("ref table Date\r\n", out)
        self.assertTrue(out.startswith("ref table Sales\r\n"))

    def test_ruleset_keeps_equality_and_boolean_operators(self):
        kinds = ruleset._lex("Name == 1 && Other != 2 || !Flag")
        ops = [tok for tok in kinds if tok[0] == "op"]
        self.assertEqual([op for _kind, op in ops], ["==", "&&", "!=", "||", "!"])

    def test_watch_on_mcp_is_a_single_pass(self):
        tool = mcp.BY_NAME["watch"]
        argv, _cwd = mcp.build_argv(tool, {}, os.getcwd(), None)
        self.assertIn("--once", argv)

    def test_report_measures_from_different_reports_do_not_share_a_key(self):
        one = usage.Obj("report_measure", "Sales", "Total", extra={"report": "East"})
        other = usage.Obj("report_measure", "Sales", "Total", extra={"report": "West"})
        same = usage.Obj("measure", "Sales", "Total")
        self.assertNotEqual(one.key, other.key)
        self.assertEqual(same.key, ("measure", "sales", "total"))
        self.assertEqual(len(one.key), 4)

    def test_image_remove_rejects_a_path_item(self):
        self.assertFalse(images.valid_item(r"..\secret.png"))
        self.assertFalse(images.valid_item("folder/picture.png"))


if __name__ == "__main__":
    unittest.main()
