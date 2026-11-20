"""Evidence log rotation, HMAC chain, and the 30-day segment prune.

Run: python tests/test_evidence_rotation.py
"""
from __future__ import annotations

import datetime as dt
import json
import multiprocessing as mp
from contextlib import contextmanager
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)

from pbz.common import PbzError, Project, Workspace  # noqa: E402
from pbz.checks import LEDGER_DAYS  # noqa: E402
from pbz import evidence  # noqa: E402


def _entry(ts: str, n: int) -> dict:
    return {"ts": ts, "project": "Canary", "verb": "validate", "mirror_rev": "m" + "a" * 12,
            "real_rev": "r" + "b" * 12, "stage": "PARSED", "scope": "validate",
            "summary": {"errors": 0, "warnings": n, "outcome": "PASS"}}


def _race_writer(root, private, kind, entered, release, ready, acknowledgements, archived):
    ws = Workspace(root)
    ws.private = private
    project = Project(ws, "Canary")
    evidence.LOG_CAP = 700
    original_lock = evidence._workspace_lock
    original_remove = evidence.os.remove
    @contextmanager
    def at_lock(ws):
        ready.set()  # Signal at the lock attempt, while the rotating writer holds it.
        with original_lock(ws):
            yield
    def archive_remove(path):
        if str(path).endswith(".jsonl") and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                archived.put(handle.read())
        return original_remove(path)
    evidence._workspace_lock = at_lock
    evidence.os.remove = archive_remove
    if kind != "prune":
        evidence._prune = lambda ws: {}  # Only the concurrent pruner may delete segments.
    original_append = evidence._append_lines
    written = []
    def record_append(ws, entries):
        original_append(ws, entries)
        written.extend(json.loads(json.dumps(entries)))
    evidence._append_lines = record_append
    try:
        if kind == "rotate":
            original = evidence._rotate
            def rotate(ws):
                entered.set()
                if not release.wait(20):
                    raise RuntimeError("release timeout")
                return original(ws)
            with patch.object(evidence, "_rotate", rotate):
                evidence._append_lines(ws, [_entry(dt.date.today().isoformat() + "T00:00:01", 101)])
            acknowledgements.put(("write", written))
        elif kind == "accept":
            with patch("pbz.cli._is_human", return_value=True), \
                    patch("pbz.evidence.mirror_rev", return_value="m" + "a" * 12), \
                    patch("pbz.evidence.real_rev", return_value="r" + "b" * 12), \
                    patch("pbz.changes.run", return_value={"result": "UNCHANGED"}), \
                    patch("pbz.evidence._suggested_tests", return_value=[]), \
                    patch("pbz.consumers.load_registry", return_value={"consumers": []}), \
                    patch("pbz.handoff.write_packet", return_value={}), \
                    patch("pbz.handoff.accept_generated"):
                out = evidence.review(ws, project, accept=True)
                if out["coverage"]["human_visual_review"] != "CURRENT":
                    raise RuntimeError("acceptance lost")
            acknowledgements.put(("accept", written))
        else:
            result = evidence.prune(ws)
            acknowledgements.put(("prune", result["pruned_segments"]))
    except Exception as err:
        acknowledgements.put(("error", type(err).__name__, str(err)))
        raise


def _hold(root, private, phase, entered, release):
    ws = Workspace(root)
    ws.private = private
    def stop(*args, **kwargs):
        entered.set()
        release.wait(30)
    if phase == "lock":
        with evidence._workspace_lock(ws):
            stop()
    else:
        original_remove = evidence.os.remove
        def delete_then_stop(path):
            original_remove(path)
            stop()
        with patch.object(evidence.os, "remove", delete_then_stop):
            evidence.prune(ws)


def _waiting_writer(root, private, ready, completed):
    ws = Workspace(root)
    ws.private = private
    ready.set()
    evidence._append_lines(ws, [_entry(dt.date.today().isoformat() + "T00:00:01", 1)])
    completed.set()


class EvidenceRotationTests(unittest.TestCase):
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

    def _cap(self, n):
        return patch.object(evidence, "LOG_CAP", n)

    def test_rotation_keeps_the_chain_and_legacy_lines(self):
        with self._cap(700):
            day = (dt.date.today() - dt.timedelta(days=1)).isoformat()
            evidence._append_lines(self.ws, [_entry(day + "T00:00:01", 1)])
            path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
            with open(path, "a", encoding="utf-8") as handle:
                handle.write('{"legacy": true}\n')
            evidence._append_lines(self.ws, [_entry(day + "T00:00:02", 2)])
            # Fill until the next record cannot fit, which rotates.
            while os.path.getsize(path) + 400 <= evidence.LOG_CAP:
                evidence._append_lines(self.ws, [_entry(day + "T00:00:03", 3)])
            evidence._append_lines(self.ws, [_entry(day + "T00:00:04", 4)])
            self.assertLessEqual(len(os.listdir(os.path.join(self.ws.pbz_dir, "evidence.d"))), 4)
            segment = os.path.join(self.ws.pbz_dir, "evidence.d", "0001.jsonl")
            self.assertTrue(os.path.isfile(segment))
            self.assertLess(os.path.getsize(path), evidence.LOG_CAP)
            self.assertIn(b"legacy", open(segment, "rb").read())
            checked = evidence.verify_chain(self.ws)
            self.assertEqual(checked["chain"], "BROKEN")
            self.assertEqual(checked["reason"], "UNCHAINED")
            self.assertEqual(checked["records"], 1)
            # A chained line must not follow a forged previous MAC.
            lines = open(path, encoding="utf-8").read().splitlines()
            forged = json.loads(lines[-1])
            forged["chain_prev"] = "f" * 64
            lines[-1] = json.dumps(forged)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            self.assertEqual(evidence.verify_chain(self.ws)["chain"], "BROKEN")

    def test_over_cap_does_not_rotate(self):
        with self._cap(200):
            os.makedirs(self.ws.pbz_dir, exist_ok=True)
            path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
            with open(path, "wb") as handle:
                handle.write(b"x" * 201)
            before = open(path, "rb").read()
            with self.assertRaises(PbzError) as caught:
                evidence._append_lines(self.ws, [_entry("2026-11-08T00:00:01", 1)])
            self.assertEqual(caught.exception.detail, evidence.OVER_CAP)
            self.assertEqual(open(path, "rb").read(), before)
            self.assertFalse(os.path.lexists(os.path.join(self.ws.pbz_dir, "evidence.d")))

    def test_prune_drops_only_an_old_prefix_and_keeps_the_anchor(self):
        # 2026-01-02 is outside the live 30-day window, so an append dated that day is pruned
        # immediately. Hold the clock on a day when that record is still inside the window long
        # enough to rotate it, and date the segment that must survive as yesterday.
        yesterday = dt.date.today() - dt.timedelta(days=1)
        held = dt.date(2026, 1, 2) + dt.timedelta(days=LEDGER_DAYS - 1)

        class _HeldDate(dt.date):
            @classmethod
            def today(cls):
                return cls(held.year, held.month, held.day)

        def near_cap(path):
            slack = evidence.LOG_CAP - os.path.getsize(path) - 30
            self.assertGreater(slack, 0)
            with open(path, "ab") as handle:
                handle.write(b"\n" * slack)

        with self._cap(4000), patch("pbz.evidence.dt.date", _HeldDate):
            evidence._append_lines(self.ws, [_entry("2026-01-02T00:00:01", 1)])
            path = os.path.join(self.ws.pbz_dir, "evidence.jsonl")
            near_cap(path)
            evidence._append_lines(self.ws, [_entry(yesterday.isoformat() + "T00:00:03", 2)])
            old_segment = os.path.join(self.ws.pbz_dir, "evidence.d", "0001.jsonl")
            self.assertTrue(os.path.isfile(old_segment))
            near_cap(path)
            evidence._append_lines(self.ws, [_entry(yesterday.isoformat() + "T00:00:04", 3)])
            keep_segment = os.path.join(self.ws.pbz_dir, "evidence.d", "0002.jsonl")
            self.assertTrue(os.path.isfile(keep_segment))
            with open(old_segment, encoding="utf-8") as handle:
                old_text = handle.read()

        self.assertIn("2026-01-02", old_text)
        pruned = evidence.prune(self.ws)
        self.assertGreaterEqual(pruned["pruned_segments"], 1)
        self.assertFalse(os.path.lexists(old_segment))
        self.assertTrue(os.path.isfile(keep_segment))
        self.assertEqual(evidence._segment_paths(self.ws), [keep_segment])
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(evidence.verify_chain(self.ws)["chain"], "OK")
        with open(os.path.join(self.ws.pbz_dir, "evidence.chain.json"), encoding="utf-8") as handle:
            anchor = json.loads(handle.read())
        with open(keep_segment, encoding="utf-8") as handle:
            kept_text = handle.read()
        kept = next(json.loads(line) for line in kept_text.splitlines() if line.startswith("{"))
        self.assertEqual(kept["chain_prev"], anchor["anchor"])
        self.assertEqual(anchor["anchor"], evidence._last_mac(old_text))
        self.assertIn(yesterday.isoformat(), kept_text)

    def test_forced_process_rotation_prune_and_review_accept(self):
        ctx = mp.get_context("spawn")
        entered, release = ctx.Event(), ctx.Event()
        ack = ctx.Queue()
        archived = ctx.Queue()
        # A seeded old segment is the only record eligible for pruning.
        seed = [_entry("2020-01-01T00:00:01", 0)]
        with patch.object(evidence, "_prune", return_value={}), self._cap(700):
            evidence._append_lines(self.ws, seed)
        processes = []
        for kind in ("rotate", "accept", "prune"):
            ready = ctx.Event()
            proc = ctx.Process(target=_race_writer, args=(self.root, self.ws.private, kind,
                               entered, release, ready, ack, archived))
            proc.start()
            processes.append(proc)
            self.addCleanup(lambda p=proc: p.terminate() if p.is_alive() else None)
            self.assertTrue(ready.wait(15))
            if kind == "rotate":
                self.assertTrue(entered.wait(15))  # Writer holds the lock at the rotation boundary.
        release.set()  # Both other processes have reached their ledger operations.
        results = [ack.get(timeout=30) for _ in processes]
        for proc in processes:
            proc.join(15)
            self.assertEqual(proc.exitcode, 0, results)
        self.assertEqual({r[0] for r in results}, {"accept", "prune", "write"})
        self.assertEqual(dict(results)["prune"], 1)
        self.assertEqual(evidence.verify_chain(self.ws)["chain"], "OK")
        # Compare all acknowledged writes independently, not just the retained count.
        removed = archived.get(timeout=15)  # Capture the pruned seed, independently of retained records.
        records = [json.loads(line) for line in removed.splitlines() if line.strip()]
        records += [json.loads(line) for path in evidence._log_files(self.ws)
                   for line in evidence._read_log(self.ws, path).splitlines() if line.strip()]
        expected = seed + dict(results)["write"] + dict(results)["accept"]
        identity = lambda r: json.dumps(r, sort_keys=True)
        self.assertEqual(sorted(map(identity, records)), sorted(map(identity, expected)))
        entries, skipped = evidence._load_entries(self.ws, self.project)
        self.assertEqual(skipped, 0)
        self.assertTrue(any(e["stage"] == "HUMAN_ACCEPTED" for e in entries))

    def test_kill_during_prune_recovers_before_read_or_write(self):
        with patch.object(evidence, "_prune", return_value={}), self._cap(700):
            evidence._append_lines(self.ws, [_entry("2020-01-01T00:00:01", 0)])
            evidence._append_lines(self.ws, [_entry("2020-01-02T00:00:01", 0)])
            evidence._append_lines(self.ws, [_entry(dt.date.today().isoformat() + "T00:00:01", 1)])
        self.assertEqual(len(evidence._segment_paths(self.ws)), 2)
        ctx = mp.get_context("spawn")
        entered, release = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_hold, args=(self.root, self.ws.private, "prune", entered, release))
        proc.start()
        self.addCleanup(lambda: proc.terminate() if proc.is_alive() else None)
        self.assertTrue(entered.wait(15))  # Journal committed; first deletion finished, second pending.
        self.assertEqual(len(evidence._segment_paths(self.ws)), 1)
        proc.terminate()
        proc.join(15)
        self.assertEqual(evidence.verify_chain(self.ws)["chain"], "OK")
        self.assertEqual(evidence.prune(self.ws)["pruned_segments"], 0)
        evidence._append_lines(self.ws, [_entry(dt.date.today().isoformat() + "T00:00:02", 2)])
        self.assertEqual(evidence.verify_chain(self.ws)["records"], 2)

    def test_killed_lock_holder_releases_workspace_lock(self):
        ctx = mp.get_context("spawn")
        entered, release = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_hold, args=(self.root, self.ws.private, "lock", entered, release))
        proc.start()
        self.addCleanup(lambda: proc.terminate() if proc.is_alive() else None)
        self.assertTrue(entered.wait(15))
        writer_ready, completed = ctx.Event(), ctx.Event()
        writer = ctx.Process(target=_waiting_writer, args=(self.root, self.ws.private, writer_ready, completed))
        writer.start()
        self.addCleanup(lambda: writer.terminate() if writer.is_alive() else None)
        self.assertTrue(writer_ready.wait(15))
        self.assertFalse(completed.wait(0.5), "writer completed while lock was held")
        proc.terminate()
        proc.join(15)
        self.assertTrue(completed.wait(15))
        writer.join(15)
        self.assertEqual(writer.exitcode, 0)
        self.assertEqual(evidence.verify_chain(self.ws)["chain"], "OK")

    def test_corrupt_tip_refuses_append_and_io_errors_are_closed(self):
        evidence._append_lines(self.ws, [_entry(dt.date.today().isoformat() + "T00:00:01", 1)])
        path = evidence._log_path(self.ws)
        original = open(path, "rb").read()
        obj = json.loads(original)
        obj["chain_mac"] = "f" * 64
        with open(path, "w") as handle:
            handle.write(json.dumps(obj) + "\n")
        corrupt = open(path, "rb").read()
        with self.assertRaises(PbzError):
            evidence._append_lines(self.ws, [_entry(dt.date.today().isoformat() + "T00:00:02", 2)])
        self.assertEqual(open(path, "rb").read(), corrupt)
        with open(path, "wb") as handle:
            handle.write(original)
        for target in ("_stamp", "_rotate"):
            with self._cap(700), patch.object(evidence, target, side_effect=OSError("fixture")):
                with self.assertRaises(PbzError):
                    evidence._append_lines(self.ws, [_entry(dt.date.today().isoformat() + "T00:00:02", 2)])
        with patch.object(evidence, "_prune", side_effect=OSError("fixture")):
            evidence._append_lines(self.ws, [_entry(dt.date.today().isoformat() + "T00:00:03", 3)])
        self.assertEqual(evidence.verify_chain(self.ws)["chain"], "OK")

    def test_cr_is_normalized_before_the_mac(self):
        evidence._append_lines(self.ws, [{"k": "a\r\nb"}])
        body = open(os.path.join(self.ws.pbz_dir, "evidence.jsonl"), "rb").read()
        self.assertNotIn(b"\r", body)
        self.assertEqual(evidence.verify_chain(self.ws)["chain"], "OK")


if __name__ == "__main__":
    unittest.main()
