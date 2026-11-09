"""Round 18 Item 2: static interaction contracts. Run: python tests/test_interactions_static.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

import test_parity  # noqa: E402
from pbz import common, evidence, interactiontest, mirror  # noqa: E402

CANARY = "PBZ_CANARY_TITLE"


class InteractionsStatic(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "ws")
        test_parity.build(root)
        self.ws = common.Workspace(root)
        self.project = self.ws.project()
        mirror.pull(self.ws, self.project)
        pages = os.path.join(self.project.mirror_report, "definition", "pages", "p1")
        os.makedirs(os.path.join(pages, "visuals", "v1"), exist_ok=True)
        with open(os.path.join(pages, "page.json"), "w", encoding="utf-8") as handle:
            json.dump({"name": CANARY, "displayName": CANARY}, handle)
        with open(os.path.join(pages, "visuals", "v1", "visual.json"), "w", encoding="utf-8") as handle:
            json.dump({"visual": {"visualType": "actionButton", "visualContainerObjects": {
                "visualLink": [{"properties": {"type": {"expr": {"Literal": {"Value": "'Back'"}}}}}]
            }}}, handle)

    def test_frozen_results(self):
        self.assertEqual(interactiontest.RESULTS, frozenset({
            "MATCH", "MISMATCH", "DEFAULT_UNRESOLVED", "MISSING_ID", "RUNTIME_ONLY",
            "LEGACY", "UNKNOWN_TYPE", "UNSUPPORTED", "REFUSED",
        }))

    def test_back_is_match_without_canary(self):
        out = interactiontest.run(self.ws, self.project, {
            "version": 1, "cases": [{"id": "b", "action": "back", "page": "p1", "visual": "v1"}]})
        blob = json.dumps(out)
        self.assertEqual(out["results"][0]["status"], "MATCH")
        self.assertEqual(out["stage"], "STATIC_CONTRACT")
        self.assertNotIn(CANARY, blob)
        self.assertFalse(out["native_executed"])

    def test_missing_visual(self):
        out = interactiontest.run(self.ws, self.project, {
            "version": 1, "cases": [{"id": "m", "action": "hover", "page": "p1", "visual": "nope"}]})
        self.assertEqual(out["results"][0]["status"], "MISSING_ID")

    def test_native_unsupported(self):
        out = interactiontest.run(self.ws, self.project, {
            "version": 1, "cases": [{"id": "n", "action": "back", "page": "p1", "visual": "v1"}]}, "native")
        self.assertEqual(out["status"], "UNSUPPORTED")
        self.assertFalse(out["executed"])
        self.assertTrue(all(r["status"] == "UNSUPPORTED" for r in out["results"]))

    def test_static_evidence_is_not_human_accepted(self):
        out = interactiontest.run(self.ws, self.project, {
            "version": 1, "cases": [{"id": "b", "action": "back", "page": "p1", "visual": "v1"}]})
        stage, summary = evidence.summarize("interactions", None, out)
        self.assertEqual(stage, "STATIC_CONTRACT")
        self.assertNotEqual(stage, "HUMAN_ACCEPTED")
        self.assertEqual(summary["outcome"], "INCOMPLETE")
        self.assertFalse(summary["executed"])
        self.assertNotIn(CANARY, json.dumps(summary))
        entry = {"stage": stage, "scope": "interactions", "mirror_rev": "m0123456789ab", "summary": summary}
        self.assertTrue(evidence.static_proof_ok([entry], mirror_rev="m0123456789ab", spec_digest=summary["spec_digest"]))
        self.assertFalse(evidence.static_proof_ok([entry], mirror_rev="m000000000000", spec_digest=summary["spec_digest"]))
        self.assertFalse(evidence.static_proof_ok([entry], mirror_rev="m0123456789ab", spec_digest="s000000000000"))
        self.assertIsNone(evidence.summarize("interactions", None, {"stage": "STATIC_CONTRACT", "results": [], "spec_digest": "nope"}))
        view = evidence.consume_static([entry], mirror_rev="m0123456789ab", spec_digest=summary["spec_digest"])
        self.assertTrue(view["static_contract"])
        self.assertFalse(view["runtime_satisfied"])
        self.assertFalse(view["human_accepted"])
        bad = dict(entry)
        bad["summary"] = dict(summary, counts={"MISMATCH": 1})
        self.assertFalse(evidence.static_proof_ok([bad], mirror_rev="m0123456789ab", spec_digest=summary["spec_digest"]))
        self.assertFalse(evidence.consume_static([{"stage": "HUMAN_ACCEPTED", "scope": "review", "mirror_rev": "m0123456789ab", "summary": summary}],
                                                  mirror_rev="m0123456789ab", spec_digest=summary["spec_digest"])["static_contract"])

    def _visual(self, visual):
        path = os.path.join(self.project.mirror_report, "definition", "pages", "p1", "visuals", "v1", "visual.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(visual, handle)

    def test_closed_tokens_from_classification(self):
        spec = {"version": 1, "cases": [{"id": "c", "action": "hover", "page": "p1", "visual": "v1"}]}
        self._visual({"visual": {"visualType": "legacy.card"}})
        self.assertEqual(interactiontest.run(self.ws, self.project, spec)["results"][0]["status"], "LEGACY")
        self._visual({"visual": {"visualType": "runtime:chart", "nativeOnly": True}})
        self.assertEqual(interactiontest.run(self.ws, self.project, spec)["results"][0]["status"], "RUNTIME_ONLY")
        self._visual({"visual": {"visualType": "unknown"}})
        self.assertEqual(interactiontest.run(self.ws, self.project, spec)["results"][0]["status"], "UNKNOWN_TYPE")
        self._visual({"visual": {"visualType": "unrecognizedCustomType", "visualContainerObjects": {
            "visualLink": [{"properties": {"type": {"expr": {"Literal": {"Value": "'Back'"}}}}}]
        }}})
        back = {"version": 1, "cases": [{"id": "b", "action": "back", "page": "p1", "visual": "v1"}]}
        self.assertEqual(interactiontest.run(self.ws, self.project, back)["results"][0]["status"], "UNKNOWN_TYPE")
        self._visual({"visual": {"visualType": "actionButton"}})
        drill = {"version": 1, "cases": [{"id": "d", "action": "drillthrough", "page": "p1", "visual": "v1", "target": "p1"}]}
        self.assertEqual(interactiontest.run(self.ws, self.project, drill)["results"][0]["status"], "DEFAULT_UNRESOLVED")
        self._visual({"visual": {"visualType": "card", "visualContainerObjects": {
            "visualLink": [{"properties": {"enabledTooltip": {"expr": {"Literal": {"Value": "'false'"}}}}}]
        }}})
        self.assertEqual(interactiontest.run(self.ws, self.project, spec)["results"][0]["status"], "MISMATCH")
        self._visual({"visual": {"visualType": "card", "visualContainerObjects": {
            "visualHeader": [{"properties": {"showTooltipButton": {"expr": {"Literal": {"Value": "'true'"}}}}}],
            "visualHeaderTooltip": [{"properties": {"text": {"expr": {"Literal": {"Value": "'hint'"}}}}}],
        }}})
        self.assertEqual(interactiontest.run(self.ws, self.project, spec)["results"][0]["status"], "MATCH")
        native = interactiontest.run(self.ws, self.project, spec, "native")
        self.assertIsNone(evidence.summarize("interactions", None, native))
        with open(interactiontest.__file__, encoding="utf-8") as handle:
            source = handle.read()
        for banned in ("socket", "urllib", "engine_call", "http.client"):
            self.assertNotIn(banned, source)

    def test_review_binds_current_spec_digest(self):
        spec = {"version": 1, "cases": [{"id": "b", "action": "back", "page": "p1", "visual": "v1"}]}
        out = interactiontest.run(self.ws, self.project, spec)
        evidence.record(self.ws, self.project, "interactions", None, out)
        other = interactiontest.run(self.ws, self.project, {
            "version": 1, "cases": [{"id": "c", "action": "back", "page": "p1", "visual": "v1"}]})
        self.assertNotEqual(out["spec_digest"], other["spec_digest"])
        self.assertFalse(evidence.review(self.ws, self.project)["coverage"]["static_contract"])
        self.assertFalse(evidence.review(self.ws, self.project)["coverage"]["runtime_from_static"])
        self.assertFalse(evidence.review(self.ws, self.project)["coverage"]["human_from_static"])
        bound = evidence.review(self.ws, self.project, current_spec_digest=out["spec_digest"])
        self.assertTrue(bound["coverage"]["static_contract"])
        self.assertFalse(bound["coverage"]["runtime_from_static"])
        stale = evidence.review(self.ws, self.project, current_spec_digest=other["spec_digest"])
        self.assertFalse(stale["coverage"]["static_contract"])
        self.assertFalse(evidence.static_proof_ok(
            [{"stage": "STATIC_CONTRACT", "scope": "interactions", "mirror_rev": "not-a-rev", "summary": {}}],
            mirror_rev="not-a-rev", spec_digest=out["spec_digest"]))

    def test_cli_and_evidence_omit_canary(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from pbz import cli
        spec = os.path.join(self.tmp.name, "spec.json")
        with open(spec, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "cases": [{"id": "b", "action": "back", "page": "p1", "visual": "v1"}]}, handle)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--workspace", self.ws.root, "interactions", "check", spec])
        self.assertEqual(rc, 0)
        self.assertNotIn(CANARY, out.getvalue())
        self.assertNotIn(CANARY, err.getvalue())
        log = os.path.join(self.ws.root, ".pbz", "evidence.jsonl")
        self.assertTrue(os.path.isfile(log))
        with open(log, encoding="utf-8") as handle:
            self.assertNotIn(CANARY, handle.read())
        guard = os.path.join(self.ws.root, ".pbz", "logs", "guard.log")
        if os.path.isfile(guard):
            with open(guard, encoding="utf-8") as handle:
                self.assertNotIn(CANARY, handle.read())
        page = os.path.join(self.project.mirror_report, "definition", "pages", "p1", "page.json")
        with open(page, "w", encoding="utf-8") as handle:
            handle.write("{")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = cli.main(["--workspace", self.ws.root, "interactions", "check", spec])
        self.assertNotEqual(rc, 0)
        self.assertNotIn(CANARY, out.getvalue())
        self.assertNotIn(CANARY, err.getvalue())
        with open(log, encoding="utf-8") as handle:
            self.assertNotIn(CANARY, handle.read())

    def test_malformed_nested_metadata_is_refused(self):
        import io
        from contextlib import redirect_stderr, redirect_stdout
        from pbz import cli
        visual = os.path.join(self.project.mirror_report, "definition", "pages", "p1", "visuals", "v1", "visual.json")
        spec = {"version": 1, "cases": [{"id": "b", "action": "back", "page": "p1", "visual": "v1"}]}
        with open(visual, "w", encoding="utf-8") as handle:
            json.dump({"visual": {"visualType": "actionButton", "visualContainerObjects": {
                "visualLink": [{"properties": {"type": {"expr": {"Literal": {"Value": 123}}}}}]
            }}}, handle)
        out = interactiontest.run(self.ws, self.project, spec)
        self.assertEqual(out["results"][0]["status"], "REFUSED")
        blob = json.dumps(out)
        self.assertNotIn(CANARY, blob)
        self.assertNotIn("AttributeError", blob)
        self.assertNotIn("Traceback", blob)
        stage, summary = evidence.summarize("interactions", None, out)
        entry = {"stage": stage, "scope": "interactions", "mirror_rev": "m0123456789ab", "summary": summary}
        self.assertFalse(evidence.static_proof_ok(
            [entry], mirror_rev="m0123456789ab", spec_digest=summary["spec_digest"]))
        with open(visual, "w", encoding="utf-8") as handle:
            json.dump({"visual": None}, handle)
        out = interactiontest.run(self.ws, self.project, spec)
        self.assertEqual(out["results"][0]["status"], "REFUSED")
        self.assertNotIn(CANARY, json.dumps(out))
        path = os.path.join(self.tmp.name, "spec.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(spec, handle)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            cli.main(["--workspace", self.ws.root, "interactions", "check", path])
        shown = stdout.getvalue() + stderr.getvalue()
        self.assertIn("REFUSED", shown)
        self.assertNotIn(CANARY, shown)
        self.assertNotIn("Traceback", shown)
        self.assertNotIn("AttributeError", shown)

    def test_requirements_verify_ignores_static_contract(self):
        import inspect
        from pbz import requirements
        src = inspect.getsource(requirements.verify)
        self.assertNotIn("STATIC_CONTRACT", src)
        self.assertNotIn("static_proof", src)
        self.assertNotIn("evidence", src)


if __name__ == "__main__":
    unittest.main()
