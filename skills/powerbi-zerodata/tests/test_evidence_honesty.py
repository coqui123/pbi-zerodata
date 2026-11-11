"""Evidence log anchor, 1 MiB cap, and whole-record review honesty.

Run: python tests/test_evidence_honesty.py
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

from pbz.common import PbzError, Project, Workspace, append_checked  # noqa: E402
from pbz import cli, evidence  # noqa: E402

M = "m" + "a" * 12
R = "r" + "b" * 12
TS = "2026-01-02T03:04:05"
SENTINEL = "SECRET-SENTINEL-VALUE"


def _line(verb, stage, scope, summary, project="Canary", ts=TS, extra=None):
    obj = {"ts": ts, "project": project, "verb": verb, "mirror_rev": M, "real_rev": R,
           "stage": stage, "summary": summary}
    if scope is not None:
        obj["scope"] = scope
    if extra:
        obj.update(extra)
    return json.dumps(obj, ensure_ascii=False)


class EvidenceHonestyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        os.environ["PBZ_PRIVATE_ROOT"] = os.path.join(self.tmp.name, "private")
        self.addCleanup(lambda: os.environ.pop("PBZ_PRIVATE_ROOT", None))
        self.root = os.path.join(self.tmp.name, "ws")
        os.makedirs(self.root)
        open(os.path.join(self.root, "Canary.pbip"), "w", encoding="utf-8").close()
        self.ws = Workspace(self.root)
        self.project = Project(self.ws, "Canary")
        os.makedirs(self.ws.mirror, exist_ok=True)
        self.outside = tempfile.TemporaryDirectory()
        self.addCleanup(self.outside.cleanup)
        self.packets = []
        self.accepts = []

    def _write_log(self, text: str) -> str:
        from pbz import dpapi
        trailing = text.endswith("\n")
        pieces = text[:-1].split("\n") if trailing else text.split("\n")
        if text == "":
            pieces = []
        dicts = []
        for piece in pieces:
            if not piece.strip():
                continue
            try:
                obj = json.loads(piece)
            except (ValueError, RecursionError):
                continue
            if isinstance(obj, dict):
                dicts.append(obj)
        if dicts:
            key = dpapi.load_key(self.ws.private)
            prev = evidence._anchor(self.ws)
            stamped = []
            for obj in dicts:
                clean = evidence._nocr(obj)
                clean.pop("chain_mac", None)
                clean["chain_prev"] = prev
                clean["chain_mac"] = evidence._mac(key, prev, clean)
                prev = clean["chain_mac"]
                stamped.append(clean)
            out_lines = []
            index = 0
            for piece in pieces:
                parsed = None
                if piece.strip():
                    try:
                        parsed = json.loads(piece)
                    except (ValueError, RecursionError):
                        parsed = None
                if isinstance(parsed, dict):
                    out_lines.append(json.dumps(stamped[index], ensure_ascii=False))
                    index += 1
                else:
                    out_lines.append(piece)
            text = "\n".join(out_lines) + ("\n" if trailing else "")
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
        with open(path, "wb") as handle:
            handle.write(text.encode("utf-8"))
        return path

    def _review(self, accept=False):
        def packet(ws, alias, text, folder):
            self.packets.append(text)
            return "pbz-workspace/REVIEW.md"

        def accepted(*args, **kwargs):
            self.accepts.append(args)
            return False

        with patch("pbz.evidence.mirror_rev", return_value=M), \
             patch("pbz.evidence.real_rev", return_value=R), \
             patch("pbz.changes.run", return_value={"result": "NO_HISTORY"}), \
             patch("pbz.consumers.load_registry", return_value={"consumers": []}), \
             patch("pbz.handoff.write_packet", side_effect=packet), \
             patch("pbz.handoff.accept_generated", side_effect=accepted), \
             patch("pbz.cli._is_human", return_value=True):
            return evidence.review(self.ws, self.project, accept=accept)

    def _rendered(self, out) -> str:
        return json.dumps(out, ensure_ascii=False) + "\n".join(self.packets)

    def test_missing_log_is_not_run_and_accept_creates_it(self):
        out = self._review()
        self.assertTrue(out["evidence"])
        self.assertTrue(all(item["status"] == "NOT_RUN" for item in out["evidence"]))
        self.assertFalse(os.path.lexists(os.path.join(self.ws.pbz_dir, "evidence.jsonl")))
        accepted = self._review(accept=True)
        path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
        self.assertTrue(os.path.isfile(path))
        body = open(path, encoding="utf-8").read()
        self.assertNotIn("\r", body)
        self.assertIn("HUMAN_ACCEPTED", body)
        human = [item for item in accepted["evidence"] if item["stage"] == "HUMAN_ACCEPTED"][0]
        self.assertEqual(human["items"][0]["scope"], "review:--accept")
        self.assertEqual(human["items"][0]["summary"], {"accepted": True})

    def test_no_pbz_directory_is_not_run(self):
        out = self._review()
        self.assertTrue(all(item["status"] == "NOT_RUN" for item in out["evidence"]))
        self.assertEqual(self.accepts, [])

    def test_round_trip_shapes_are_byte_identical(self):
        # Distinct stage+scope keys only — colliding shapes are exercised alone below.
        shapes = [
            ("validate", "PARSED", "validate", {"errors": 0, "warnings": 1, "outcome": "PASS"}),
            ("push", "BOUND", "push", {"result": "PUSHED", "errors": 0, "tom": True,
                                       "validation": {"tom": "PASS", "report_schema": "PASS"}, "outcome": "PASS"}),
            ("push", "LOADED", "desktop:reload", {"action": "reload", "ready": True, "outcome": "PASS"}),
            ("desktop open", "LOADED", "desktop:open", {"action": "open", "ready": False, "outcome": "INCOMPLETE"}),
            ("health", "EXECUTED", "health", {"health_errors": 1, "unprocessed_tables": 0, "outcome": "FAIL", "executed": True}),
            ("check", "EXECUTED", "check", {"checks": {"ERROR:SYNTAX": 1, "PASS": 2}, "ids": [None, "c 1.2"],
                                            "outcome": "FAIL", "executed": True}),
            ("check", "EXECUTED", "check:c 1.2", {"id": "c 1.2", "status": "PASS", "outcome": "PASS", "executed": True}),
            ("check", "EXECUTED", "check:?", {"id": None, "status": "PASS", "outcome": "PASS", "executed": True}),
            ("snapshot compare", "EXECUTED", "snapshot:compare",
             {"snapshot": "base snap", "queries": {"SAME": 1}, "outcome": "PASS", "executed": True}),
            ("snapshot compare", "EXECUTED", "snapshot:snap name.v1:q 1.2",
             {"snapshot": "snap name.v1", "id": "q 1.2", "result": "SAME", "outcome": "PASS", "executed": True}),
            ("smoke", "EXECUTED", "smoke", {"smoke": {"PASS": 1}, "partial_filters": False, "outcome": "PASS", "executed": True}),
            ("smoke", "EXECUTED", "smoke:P& (1).x",
             {"page": "P& (1).x", "smoke": {"PASS": 1}, "partial_filters": False,
              "coverage": "QUERY_ONLY", "outcome": "PASS", "executed": True}),
            ("synth-test run", "SYNTHETIC", "synth-test:golden name",
             {"test": "golden name", "result": "NOT_COMPARABLE", "outcome": "INCOMPLETE"}),
            ("handoff", "HANDOFF", "handoff", {"file": "HANDOFF.md", "outcome": "GENERATED"}),
        ]
        lines = [_line(*shape) for shape in shapes]
        lines.append(_line("review --accept", "HUMAN_ACCEPTED", None, {"accepted": True}))
        self._write_log("\n".join(lines) + "\n")
        out = self._review()
        self.assertNotIn("result", out)
        rendered = self._rendered(out)
        for verb, stage, scope, summary in shapes:
            self.assertIn(json.dumps(summary, ensure_ascii=False), rendered)
        human = [item for item in out["evidence"] if item["stage"] == "HUMAN_ACCEPTED"][0]
        self.assertEqual(human["items"][0]["scope"], "review:--accept")
        self.assertEqual(human["items"][0]["summary"], {"accepted": True})

    def test_blocked_health_shape_round_trips_alone(self):
        summary = {"outcome": "BLOCKED", "status": "BUDGET", "executed": False}
        self._write_log(_line("health", "EXECUTED", "health", summary) + "\n")
        out = self._review()
        self.assertIn(json.dumps(summary, ensure_ascii=False), self._rendered(out))

    def test_blocked_snapshot_save_and_null_fallback_scopes(self):
        blocked = _line("snapshot save", "EXECUTED", "snapshot:save",
                        {"outcome": "BLOCKED", "status": "UNSUPPORTED", "executed": False})
        check_q = _line("check", "EXECUTED", "check:?",
                        {"id": None, "status": "PASS", "outcome": "PASS", "executed": True})
        check_none = _line("check", "EXECUTED", "check:None",
                           {"id": None, "status": "PASS", "outcome": "PASS", "executed": True})
        self._write_log("\n".join([blocked, check_q]) + "\n")
        out = self._review()
        scopes = {i["scope"] for stage in out["evidence"] for i in stage.get("items", [])}
        self.assertIn("snapshot:save", scopes)
        self.assertIn("check:?", scopes)
        self.packets.clear()
        self._write_log(check_none + "\n")
        out2 = self._review()
        scopes2 = {i["scope"] for stage in out2["evidence"] for i in stage.get("items", [])}
        self.assertIn("check:None", scopes2)

    def test_producer_record_round_trips_through_review(self):
        with patch("pbz.evidence.mirror_rev", return_value=M), \
             patch("pbz.evidence.real_rev", return_value=R):
            evidence.record(self.ws, self.project, "validate", None,
                            {"errors": [], "warnings": [], "ok": True})
            evidence.record(self.ws, self.project, "snapshot", "save",
                            {"result": "UNSUPPORTED", "status": "UNSUPPORTED"})
            evidence.record(self.ws, self.project, "check", None,
                            {"ok": True, "results": [{"id": None, "status": "PASS"}]})
        out = self._review()
        self.assertNotIn("skipped", out)
        scopes = {i["scope"] for stage in out["evidence"] for i in stage.get("items", [])}
        self.assertIn("validate", scopes)
        self.assertIn("snapshot:save", scopes)
        self.assertIn("check:None", scopes)

    def test_malformed_scope_revision_stage_sentinels_are_skipped(self):
        good = _line("validate", "PARSED", "validate", {"errors": 0, "warnings": 0, "outcome": "PASS"})
        bad = [
            _line("validate", "PARSED", "wrong-scope", {"errors": 0, "warnings": 0, "outcome": "PASS"}),
            _line("validate", "NOPE", "validate", {"errors": 0, "warnings": 0, "outcome": "PASS"}),
            json.dumps({"ts": TS, "project": "Canary", "verb": "validate", "mirror_rev": "bad",
                        "real_rev": R, "stage": "PARSED", "scope": "validate",
                        "summary": {"errors": 0, "warnings": 0, "outcome": "PASS"}}),
            json.dumps({"ts": TS, "project": "Canary", "verb": "validate", "mirror_rev": M,
                        "real_rev": "bad", "stage": "PARSED", "scope": "validate",
                        "summary": {"errors": 0, "warnings": 0, "outcome": "PASS"}}),
            _line("validate", "PARSED", "validate", {"errors": 0, "warnings": 0, "outcome": SENTINEL}),
        ]
        self._write_log("\n".join([good] + bad) + "\n")
        out = self._review()
        self.assertEqual(out["result"], "INCOMPLETE")
        self.assertEqual(out["skipped"], len(bad))
        rendered = self._rendered(out)
        self.assertNotIn(SENTINEL, rendered)
        self.assertNotIn("wrong-scope", rendered)
        self.assertNotIn("NOPE", rendered)

    def test_unicode_line_separators_in_page_round_trip(self):
        for sep in ("\u2028", "\u2029"):
            self.packets.clear()
            page = f"Page{sep}One"
            scope = f"smoke:{page}"
            summary = {"page": page, "smoke": {"PASS": 1}, "partial_filters": False,
                       "coverage": "QUERY_ONLY", "outcome": "PASS", "executed": True}
            self._write_log(_line("smoke", "EXECUTED", scope, summary) + "\n")
            out = self._review()
            self.assertNotIn("result", out)
            self.assertNotIn("skipped", out)
            rendered = self._rendered(out)
            self.assertIn(json.dumps(summary, ensure_ascii=False), rendered)
            smoke = [item for item in out["evidence"] if item["stage"] == "EXECUTED"][0]
            self.assertEqual(smoke["items"][0]["scope"], scope)
            self.assertEqual(smoke["items"][0]["summary"]["page"], page)

    def test_same_scope_keeps_latest_outcome_only(self):
        first = _line("validate", "PARSED", "validate", {"errors": 0, "warnings": 0, "outcome": "PASS"})
        second = _line("validate", "PARSED", "validate", {"errors": 1, "warnings": 0, "outcome": "FAIL"})
        for body, keep, drop in (
            ("\n".join([first, second]) + "\n", '"outcome": "FAIL"', '"outcome": "PASS"'),
            ("\n".join([second, first]) + "\n", '"outcome": "PASS"', '"outcome": "FAIL"'),
        ):
            self.packets.clear()
            self._write_log(body)
            out = self._review()
            rendered = self._rendered(out)
            self.assertIn(keep, rendered)
            self.assertNotIn(drop, rendered)
            parsed = [item for item in out["evidence"] if item["stage"] == "PARSED"][0]
            self.assertEqual(len(parsed["items"]), 1)

    def test_wrong_types_are_skipped_and_accept_still_works(self):
        good = _line("validate", "PARSED", "validate", {"errors": 0, "warnings": 0, "outcome": "PASS"})
        bad = [
            json.dumps({"ts": TS, "project": "Canary", "verb": [], "mirror_rev": M, "real_rev": R,
                        "stage": "PARSED", "scope": "validate",
                        "summary": {"errors": 0, "warnings": 0, "outcome": "PASS"}}),
            _line("push", "BOUND", "push", {"result": "PUSHED", "errors": 0, "tom": 1,
                                            "validation": {"tom": "PASS", "report_schema": "PASS"},
                                            "outcome": "PASS"}),
            _line("push", "BOUND", "push", {"result": "PUSHED", "errors": 0, "tom": True,
                                            "validation": {"tom": [], "report_schema": "PASS"},
                                            "outcome": "PASS"}),
            _line("desktop open", "LOADED", "desktop:open",
                  {"action": ["open"], "ready": True, "outcome": "PASS"}),
        ]
        self._write_log("\n".join([good] + bad) + "\n")
        out = self._review()
        self.assertEqual(out["result"], "INCOMPLETE")
        self.assertEqual(out["skipped"], len(bad))
        rendered = self._rendered(out)
        self.assertNotIn('"tom": 1', rendered)
        self.assertNotIn('"tom": []', rendered)
        accepted = self._review(accept=True)
        self.assertEqual(accepted["result"], "INCOMPLETE")
        self.assertEqual(accepted["skipped"], len(bad))
        self.assertTrue(self.accepts)

    def test_substituted_log_handle_is_not_read(self):
        path = self._write_log(_line("validate", "PARSED", "validate",
                                     {"errors": 0, "warnings": 0, "outcome": "PASS"}) + "\n")
        secret = os.path.join(self.outside.name, "other-evidence.jsonl")
        with open(secret, "wb") as handle:
            handle.write(b"CANARY-EVIDENCE")
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

            def close(self):
                self.raw.close()

        def swapped(target, *args, **kwargs):
            if os.path.normcase(str(target)) != os.path.normcase(path):
                return real_open(target, *args, **kwargs)
            return Guard(real_open(secret, *args, **kwargs))

        before_accepts = list(self.accepts)
        with patch("builtins.open", swapped):
            with self.assertRaises(PbzError) as caught:
                self._review(accept=True)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(reads["n"], 0)
        self.assertEqual(self.packets, [])
        self.assertEqual(self.accepts, before_accepts)
        self.assertEqual(open(secret, "rb").read(), b"CANARY-EVIDENCE")
        self.assertNotIn(b"HUMAN_ACCEPTED", open(path, "rb").read())

    def test_bad_lines_are_skipped_and_not_echoed(self):
        good = _line("validate", "PARSED", "validate", {"errors": 0, "warnings": 0, "outcome": "PASS"})
        bad = [
            "[]",
            _line("check<script>", "EXECUTED", "check", {"checks": {}, "ids": [], "outcome": "PASS", "executed": False}),
            _line("snapshot purge", "EXECUTED", "snapshot:compare",
                  {"snapshot": "n", "queries": {}, "outcome": "PASS", "executed": False}),
            _line("push", "PARSED", "push", {"errors": 0, "warnings": 0, "outcome": "PASS"}),
            _line("synth-test run", "SYNTHETIC", "synth-test:name",
                  {"test": "name", "result": SENTINEL, "outcome": "INCOMPLETE"}),
            _line("validate", "PARSED", "validate", {"errors": 0, "warnings": 0, "outcome": "PASS"}, ts=SENTINEL),
            json.dumps({"project": "Canary", "verb": "nope"}),
        ]
        other = _line("validate", "PARSED", "validate", {"errors": 0, "warnings": 0, "outcome": "PASS"}, project="Other")
        self._write_log("\n".join([good, other] + bad) + "\n")
        out = self._review()
        self.assertEqual(out["result"], "INCOMPLETE")
        self.assertEqual(out["skipped"], len(bad))
        rendered = self._rendered(out)
        self.assertNotIn(SENTINEL, rendered)
        self.assertNotIn("check<script>", rendered)
        self.assertNotIn("snapshot purge", rendered)
        self.assertNotIn("Other", rendered)

    def test_other_project_alone_does_not_set_incomplete(self):
        other = _line("validate", "PARSED", "validate", {"errors": 0, "warnings": 0, "outcome": "PASS"}, project="Other")
        self._write_log(other + "\n")
        out = self._review()
        self.assertNotIn("result", out)
        self.assertNotIn("skipped", out)

    def test_over_cap_refuses_before_packet_and_recovers_when_removed(self):
        path = self._write_log("x" * (evidence.LOG_CAP + 1))
        before = open(path, "rb").read()
        with self.assertRaises(PbzError) as caught:
            self._review(accept=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, evidence.OVER_CAP)
        self.assertNotIn(self.root, caught.exception.detail)
        self.assertEqual(open(path, "rb").read(), before)
        self.assertEqual(self.packets, [])
        self.assertEqual(self.accepts, [])
        os.remove(path)
        out = self._review()
        self.assertTrue(all(item["status"] == "NOT_RUN" for item in out["evidence"]))

    def test_exact_cap_appends_and_one_more_byte_refuses(self):
        payload = "a" * evidence.LOG_CAP
        append_checked(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), payload, root=self.ws.root, max_bytes=evidence.LOG_CAP)
        path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
        self.assertEqual(os.path.getsize(path), evidence.LOG_CAP)
        self.assertNotIn(b"\r", open(path, "rb").read())
        with self.assertRaises(PbzError) as caught:
            append_checked(path, "b", root=self.ws.root, max_bytes=evidence.LOG_CAP)
        self.assertEqual(caught.exception.detail, "SIZE_CAP")
        self.assertEqual(os.path.getsize(path), evidence.LOG_CAP)

    def test_growth_during_final_contained_refuses_cap(self):
        """Cap must use a fresh fstat after identity/containment, not a stale earlier size."""
        import pbz.common as common
        good = _line("validate", "PARSED", "validate", {"errors": 0, "warnings": 0, "outcome": "PASS"}) + "\n"
        path = self._write_log(good)
        # Pad so one acceptance line would fit under cap unless growth is injected mid-check.
        pad = "x" * (evidence.LOG_CAP - os.path.getsize(path) - 40)
        with open(path, "ab") as handle:
            handle.write(pad.encode())
        before = open(path, "rb").read()
        real_contained = common._contained
        grown = {"n": 0}

        def grow(target, root):
            real_contained(target, root)
            if (os.path.normcase(str(target)) == os.path.normcase(path) and grown["n"] == 0):
                grown["n"] += 1
                with open(path, "ab") as handle:
                    handle.write(b"G" * 80)

        with patch("pbz.common._contained", side_effect=grow):
            with self.assertRaises(PbzError) as caught:
                self._review(accept=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(caught.exception.detail, evidence.OVER_CAP)
        body = open(path, "rb").read()
        self.assertTrue(body.startswith(before))
        self.assertNotIn(b"HUMAN_ACCEPTED", body)
        self.assertEqual(self.accepts, [])
        self.assertEqual(self.packets, [])

    def test_accept_near_cap_rotates_and_keeps_segment_bytes(self):
        path = self._write_log("z" * (evidence.LOG_CAP - 10))
        before = open(path, "rb").read()
        out = self._review(accept=True)
        segment = os.path.join(self.ws.pbz_dir, "evidence.d", "0001.jsonl")
        self.assertEqual(open(segment, "rb").read(), before)
        active = open(path, "rb").read()
        self.assertIn(b"HUMAN_ACCEPTED", active)
        self.assertLess(len(active), evidence.LOG_CAP)
        self.assertTrue(self.accepts)
        self.assertTrue(self.packets)
        self.assertEqual(out["coverage"]["human_visual_review"], "CURRENT")
        self.assertEqual(evidence.verify_chain(self.ws)["chain"], "OK")

    def test_growth_past_cap_between_read_and_accept_appends_nothing(self):
        path = self._write_log(_line("validate", "PARSED", "validate",
                                    {"errors": 0, "warnings": 0, "outcome": "PASS"}) + "\n")
        before = open(path, "rb").read()
        real = evidence._append_lines

        def grow(ws, entries):
            with open(path, "ab") as handle:
                handle.write(b"Q" * evidence.LOG_CAP)
            return real(ws, entries)

        with patch("pbz.evidence._append_lines", side_effect=grow):
            with self.assertRaises(PbzError) as caught:
                self._review(accept=True)
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertEqual(self.accepts, [])
        self.assertEqual(self.packets, [])
        self.assertTrue(open(path, "rb").read().startswith(before))
        self.assertNotIn(b"HUMAN_ACCEPTED", open(path, "rb").read())

    def test_under_cap_growth_is_not_rendered(self):
        path = self._write_log(_line("validate", "PARSED", "validate",
                                    {"errors": 0, "warnings": 0, "outcome": "PASS"}) + "\n")
        real = evidence._append_lines

        def grow(ws, entries):
            with open(path, "ab") as handle:
                handle.write((SENTINEL + "\n").encode())
            return real(ws, entries)

        with patch("pbz.evidence._append_lines", side_effect=grow):
            out = self._review(accept=True)
        self.assertIn(SENTINEL.encode(), open(path, "rb").read())
        self.assertNotIn(SENTINEL, self._rendered(out))
        self.assertIn("HUMAN_ACCEPTED", open(path, encoding="utf-8").read())

    def test_created_log_between_read_and_accept_is_preserved(self):
        real = evidence._append_lines
        injected = b"injected-line\n"

        def create(ws, entries):
            os.makedirs(self.ws.pbz_dir, exist_ok=True)
            path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
            with open(path, "wb") as handle:
                handle.write(injected)
            return real(ws, entries)

        with patch("pbz.evidence._append_lines", side_effect=create):
            self._review(accept=True)
        body = open(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), "rb").read()
        self.assertTrue(body.startswith(injected))
        self.assertIn(b"HUMAN_ACCEPTED", body)

    def test_junction_after_makedirs_creates_nothing_outside(self):
        from _junction import make_junction
        external = self.outside.name
        real_makedirs = os.makedirs

        def swap(path, exist_ok=False):
            real_makedirs(path, exist_ok=exist_ok)
            if os.path.basename(path) == ".pbz":
                os.rmdir(path)
                make_junction(self, path, external)

        with patch("os.makedirs", side_effect=swap):
            with self.assertRaises(PbzError) as caught:
                evidence._append_lines(self.ws, [{"ts": TS}])
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(list(Path(external).iterdir()), [])

    def test_junction_and_hardlink_refuse_before_packet(self):
        from _junction import make_junction
        external = os.path.join(self.outside.name, "store")
        os.makedirs(external)
        canary = os.path.join(external, "evidence.jsonl")
        with open(canary, "wb") as handle:
            handle.write(b"CANARY")
        make_junction(self, self.ws.pbz_dir, external)
        with self.assertRaises(PbzError) as caught:
            self._review(accept=True)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(open(canary, "rb").read(), b"CANARY")
        self.assertEqual(self.packets, [])
        os.remove(self.ws.pbz_dir) if os.path.islink(self.ws.pbz_dir) else None

    def test_hardlinked_log_refuses_and_check_is_not_recorded(self):
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        outside = os.path.join(self.outside.name, "evidence.jsonl")
        with open(outside, "wb") as handle:
            handle.write(b"EXTERNAL\n")
        path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
        try:
            os.link(outside, path)
        except OSError:
            self.skipTest("hardlink is unavailable")
        with self.assertRaises(PbzError) as caught:
            self._review()
        self.assertIn(caught.exception.code, ("UNSAFE_PATH", "REFUSED"))
        self.assertEqual(open(outside, "rb").read(), b"EXTERNAL\n")
        self.assertEqual(self.packets, [])
        primary = {"verb": "check", "ok": True, "results": [
            {"id": "a", "status": "PASS"}, {"id": "b", "status": "PASS"}, {"id": "c", "status": "FAIL"}]}
        spec = json.dumps({"checks": [{"id": "a"}]})
        with patch("pbz.checks.run_checks", return_value=primary), \
             patch("pbz.evidence.mirror_rev", return_value=M), \
             patch("pbz.evidence.real_rev", return_value=R):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                old = os.getcwd()
                os.chdir(self.root)
                try:
                    cli.main(["check", "--spec", spec])
                finally:
                    os.chdir(old)
        emitted = json.loads(buf.getvalue())
        self.assertEqual(emitted["results"], primary["results"])
        self.assertEqual(emitted.get("evidence_status"), "NOT_RECORDED")
        self.assertEqual(open(outside, "rb").read(), b"EXTERNAL\n")

    def test_near_cap_check_batch_rotates_and_records(self):
        recent = (dt.date.today() - dt.timedelta(days=1)).isoformat() + "T03:04:05"
        filler = {"ts": recent, "project": "Canary", "verb": "validate", "mirror_rev": M, "real_rev": R,
                  "stage": "PARSED", "scope": "validate", "summary": {"errors": 0, "warnings": 0, "outcome": "PASS"}}
        blob = (json.dumps(filler) + "\n").encode()
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
        with open(path, "wb") as handle:
            handle.write(blob * ((evidence.LOG_CAP // len(blob))))
            if evidence.LOG_CAP - os.path.getsize(path) > 80:
                handle.write(b"x" * (evidence.LOG_CAP - os.path.getsize(path) - 40))
        before = open(path, "rb").read()
        self.assertLess(len(before), evidence.LOG_CAP)
        primary = {"verb": "check", "ok": True, "results": [
            {"id": "a", "status": "PASS"}, {"id": "b", "status": "PASS"}, {"id": "c", "status": "FAIL"}]}
        with patch("pbz.checks.run_checks", return_value=primary), \
             patch("pbz.evidence.mirror_rev", return_value=M), \
             patch("pbz.evidence.real_rev", return_value=R):
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                old = os.getcwd()
                os.chdir(self.root)
                try:
                    cli.main(["check", "--spec", "{}"])
                finally:
                    os.chdir(old)
        emitted = json.loads(buf.getvalue())
        self.assertEqual(emitted["results"], primary["results"])
        self.assertNotEqual(emitted.get("evidence_status"), "NOT_RECORDED")
        segment = os.path.join(self.ws.pbz_dir, "evidence.d", "0001.jsonl")
        self.assertEqual(open(segment, "rb").read(), before)
        active = open(path, encoding="utf-8").read()
        self.assertIn('"verb": "check"', active)
        self.assertLess(len(active.encode("utf-8")), evidence.LOG_CAP)
        self.assertEqual(evidence.verify_chain(self.ws)["chain"], "OK")

    def test_skipped_lines_still_accept(self):
        self._write_log("[]\n" + _line("validate", "PARSED", "validate",
                                       {"errors": 0, "warnings": 0, "outcome": "PASS"}) + "\n")
        out = self._review(accept=True)
        self.assertEqual(out["result"], "INCOMPLETE")
        self.assertEqual(out["skipped"], 1)
        self.assertTrue(self.accepts)
        self.assertIn("HUMAN_ACCEPTED", open(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), encoding="utf-8").read())

    def test_suggested_junction_appends_nothing(self):
        from _junction import make_junction
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        external = os.path.join(self.outside.name, "snaps")
        os.makedirs(external)
        canary = os.path.join(external, "kept.txt")
        with open(canary, "wb") as handle:
            handle.write(b"SNAP")
        make_junction(self, os.path.join(self.ws.pbz_dir, "snapshots"), external)
        with self.assertRaises(PbzError) as caught:
            self._review(accept=True)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(open(canary, "rb").read(), b"SNAP")
        self.assertEqual(self.accepts, [])
        self.assertEqual(self.packets, [])
        self.assertFalse(os.path.lexists(os.path.join(self.ws.pbz_dir, "evidence.jsonl")))

    def test_suggested_synth_tests_junction_appends_nothing(self):
        from _junction import make_junction
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        external = os.path.join(self.outside.name, "goldens")
        os.makedirs(external)
        canary = os.path.join(external, "kept.txt")
        with open(canary, "wb") as handle:
            handle.write(b"SYN")
        make_junction(self, os.path.join(self.ws.pbz_dir, "synth-tests"), external)
        with self.assertRaises(PbzError) as caught:
            self._review(accept=True)
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(open(canary, "rb").read(), b"SYN")
        self.assertEqual(self.accepts, [])
        self.assertEqual(self.packets, [])
        self.assertFalse(os.path.lexists(os.path.join(self.ws.pbz_dir, "evidence.jsonl")))

    def test_non_utf8_log_refuses_before_accept(self):
        path = self._write_log("")
        with open(path, "wb") as handle:
            handle.write(b"\xff\xfe{" + b"not-utf8")
        before = open(path, "rb").read()
        with self.assertRaises(PbzError) as caught:
            self._review(accept=True)
        self.assertIn(caught.exception.code, ("REFUSED", "UNSAFE_PATH"))
        self.assertEqual(open(path, "rb").read(), before)
        self.assertEqual(self.accepts, [])
        self.assertEqual(self.packets, [])

    def test_nested_array_line_is_skipped_and_accepts(self):
        good = _line("validate", "PARSED", "validate", {"errors": 0, "warnings": 0, "outcome": "PASS"})
        nested = "[" * 10000 + "0" + "]" * 10000
        self.assertLess(len(nested.encode()), evidence.LOG_CAP)
        self._write_log(nested + "\n" + good + "\n")
        out = self._review(accept=True)
        self.assertEqual(out["result"], "INCOMPLETE")
        self.assertEqual(out["skipped"], 1)
        self.assertTrue(self.accepts)
        rendered = self._rendered(out)
        self.assertNotIn("[[" * 20, rendered)
        self.assertIn("HUMAN_ACCEPTED", open(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), encoding="utf-8").read())

    def test_hardlinked_synth_test_is_not_opened(self):
        os.makedirs(os.path.join(self.ws.pbz_dir, "synth-tests"), exist_ok=True)
        outside = os.path.join(self.outside.name, "golden.json")
        with open(outside, "wb") as handle:
            handle.write(b'{"secret":"OPENED"}')
        leaf = os.path.join(self.ws.pbz_dir, "synth-tests", "golden.json")
        try:
            os.link(outside, leaf)
        except OSError:
            self.skipTest("hardlink is unavailable")
        opened = []
        real = open

        def watch(path, *args, **kwargs):
            if os.path.normcase(str(path)) == os.path.normcase(leaf):
                opened.append(path)
            return real(path, *args, **kwargs)

        with patch("builtins.open", side_effect=watch):
            out = self._review(accept=True)
        self.assertEqual(opened, [])
        self.assertNotIn("golden", " ".join(out["suggested_tests"]))
        self.assertEqual(open(outside, "rb").read(), b'{"secret":"OPENED"}')
        self.assertTrue(self.accepts)

    def test_permission_error_is_refused(self):
        self._write_log("{}\n")
        real = open

        def boom(path, *args, **kwargs):
            if str(path).endswith("evidence.jsonl"):
                raise PermissionError("denied")
            return real(path, *args, **kwargs)

        with patch("builtins.open", side_effect=boom):
            with self.assertRaises(PbzError) as caught:
                self._review()
        self.assertEqual(caught.exception.code, "REFUSED")
        self.assertNotEqual(caught.exception.code, "LOCAL_STATE_UNAVAILABLE")
        self.assertEqual(self.packets, [])

    def test_non_human_accept_writes_nothing(self):
        with patch("pbz.cli._is_human", return_value=False):
            with self.assertRaises(PbzError) as caught:
                evidence.review(self.ws, self.project, accept=True)
        self.assertEqual(caught.exception.code, "HUMAN_ONLY")
        self.assertFalse(os.path.lexists(os.path.join(self.ws.pbz_dir, "evidence.jsonl")))

    def test_crlf_batch_is_stored_as_lf(self):
        evidence.record.__doc__
        evidence._append_lines(self.ws, [{"k": "a\r\nb"}])
        body = open(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), "rb").read()
        self.assertNotIn(b"\r", body)


if __name__ == "__main__":
    unittest.main()
