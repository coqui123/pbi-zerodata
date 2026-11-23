"""Policy writes use the hardened writer and the workspace root, not .pbz as the trust root.

Run: python tests/test_policy_atomic.py
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

SKILL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SKILL)
sys.path.insert(0, os.path.join(SKILL, "tests"))

from pbz.common import PbzError, Workspace, write_text  # noqa: E402
from pbz import policy, tokens  # noqa: E402


def _contention_patch(target_thread_name: str, event: threading.Event, contention_log: list):
    if os.name == "nt":
        import msvcrt
        real_locking = msvcrt.locking

        def hook(fd, mode, nbytes):
            try:
                return real_locking(fd, mode, nbytes)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    cur = threading.current_thread().name
                    if cur == target_thread_name:
                        contention_log.append(cur)
                        event.set()
                raise

        return patch("msvcrt.locking", side_effect=hook)
    else:
        import fcntl
        real_flock = fcntl.flock

        def hook(fd, op):
            try:
                return real_flock(fd, op)
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                    cur = threading.current_thread().name
                    if cur == target_thread_name:
                        contention_log.append(cur)
                        event.set()
                raise

        return patch("fcntl.flock", side_effect=hook)


def scenario_tighten_race_unlocked(root: str, private: str) -> None:
    ws = Workspace(root)
    ws.private = private
    t1_loaded = threading.Event()
    t2_done = threading.Event()
    resume_t1 = threading.Event()
    worker_errors = []
    real_save = policy.save

    def pausing_save(ws_arg, pol_arg):
        cur = threading.current_thread().name
        if cur == "T1":
            t1_loaded.set()
            if not resume_t1.wait(timeout=5.0):
                raise TimeoutError("T1 resume timed out")
        return real_save(ws_arg, pol_arg)

    def run_t1():
        try:
            pol = policy.load(ws)
            pol["sensitive_hosts"].append("host-a.example.com")
            pausing_save(ws, pol)
        except Exception as e:
            worker_errors.append(("T1", e))

    def run_t2():
        try:
            if not t1_loaded.wait(timeout=5.0):
                raise TimeoutError("T1 load signal timed out")
            pol = policy.load(ws)
            pol["sensitive_hosts"].append("host-b.example.com")
            real_save(ws, pol)
            t2_done.set()
        except Exception as e:
            worker_errors.append(("T2", e))

    th1 = threading.Thread(target=run_t1, name="T1")
    th2 = threading.Thread(target=run_t2, name="T2")
    try:
        th1.start()
        th2.start()
        if not t2_done.wait(timeout=5.0):
            raise TimeoutError("T2 failed to finish before T1 resume")
    finally:
        resume_t1.set()
        th1.join(timeout=5.0)
        th2.join(timeout=5.0)

    if worker_errors:
        raise RuntimeError(f"Worker thread error(s): {worker_errors}")

    result = policy.load(ws)
    if result["sensitive_hosts"] != ["host-a.example.com"]:
        raise AssertionError(f"Expected only host-a, got {result['sensitive_hosts']}")
    if "host-b.example.com" in result["sensitive_hosts"]:
        raise AssertionError("host-b was not overwritten as expected in unlocked race")


def scenario_tighten_concurrent_load_save_serialized(root: str, private: str) -> None:
    ws = Workspace(root)
    ws.private = private
    lock_events = []
    active_locks = {}
    contention_log = []
    first_acquired = threading.Event()
    t2_contended = threading.Event()
    resume_t1 = threading.Event()
    worker_errors = []

    real_lock_cls = tokens.WorkspaceLock

    class InstrumentedLock:
        def __init__(self, directory):
            self.real = real_lock_cls(directory)
            self.thread_name = threading.current_thread().name

        def __enter__(self):
            lock_events.append((self.thread_name, "attempt"))
            res = self.real.__enter__()
            active_locks[self.thread_name] = True
            lock_events.append((self.thread_name, "acquired"))
            if self.thread_name == "T1":
                first_acquired.set()
            return res

        def __exit__(self, *exc):
            res = self.real.__exit__(*exc)
            active_locks[self.thread_name] = False
            lock_events.append((self.thread_name, "released"))
            return res

    real_load = policy.load
    real_save = policy.save

    def instrumented_load(ws_arg):
        cur = threading.current_thread().name
        if cur in ("T1", "T2"):
            if not active_locks.get(cur):
                raise AssertionError(f"Thread {cur} called policy.load without holding WorkspaceLock")
        return real_load(ws_arg)

    def instrumented_save(ws_arg, pol_arg):
        cur = threading.current_thread().name
        if cur in ("T1", "T2"):
            if not active_locks.get(cur):
                raise AssertionError(f"Thread {cur} called policy.save without holding WorkspaceLock")
        if cur == "T1":
            if not t2_contended.wait(timeout=5.0):
                raise TimeoutError("T2 failed to encounter OS-level lock contention")
            if not resume_t1.wait(timeout=5.0):
                raise TimeoutError("T1 failed to receive resume signal")
        return real_save(ws_arg, pol_arg)

    def worker_t1():
        try:
            policy.tighten(ws, sensitive_hosts=["host-a.example.com"])
        except Exception as e:
            worker_errors.append(("T1", e))

    def worker_t2():
        try:
            policy.tighten(ws, sensitive_hosts=["host-b.example.com"])
        except Exception as e:
            worker_errors.append(("T2", e))

    started_threads = []
    th1 = threading.Thread(target=worker_t1, name="T1")
    th2 = threading.Thread(target=worker_t2, name="T2")

    with patch.object(tokens, "WorkspaceLock", InstrumentedLock), \
         _contention_patch("T2", t2_contended, contention_log), \
         patch("pbz.policy.load", side_effect=instrumented_load), \
         patch("pbz.policy.save", side_effect=instrumented_save):
        try:
            th1.start()
            started_threads.append(th1)
            if not first_acquired.wait(timeout=5.0):
                raise TimeoutError("T1 failed to acquire lock")
            th2.start()
            started_threads.append(th2)

            if not t2_contended.wait(timeout=5.0):
                raise AssertionError("T2 did not encounter lock contention")
            if "T2" not in contention_log:
                raise AssertionError("T2 not recorded in contention log")
            if not active_locks.get("T1"):
                raise AssertionError("T1 not actively holding lock")
            if active_locks.get("T2"):
                raise AssertionError("T2 should not hold lock while T1 is active")
        finally:
            resume_t1.set()
            t2_contended.set()
            for th in started_threads:
                th.join(timeout=5.0)

    if worker_errors:
        raise RuntimeError(f"Worker thread error(s): {worker_errors}")

    # Verify event count, per-thread lifecycle order, and startup gate order
    if len(lock_events) != 6:
        raise AssertionError(f"Expected exactly 6 lifecycle events, got {len(lock_events)}: {lock_events}")
    if not (lock_events.index(("T1", "attempt")) < lock_events.index(("T1", "acquired")) < lock_events.index(("T1", "released"))):
        raise AssertionError(f"T1 lifecycle order invalid: {lock_events}")
    if not (lock_events.index(("T2", "attempt")) < lock_events.index(("T2", "acquired")) < lock_events.index(("T2", "released"))):
        raise AssertionError(f"T2 lifecycle order invalid: {lock_events}")
    if not (lock_events.index(("T1", "acquired")) < lock_events.index(("T2", "attempt"))):
        raise AssertionError(f"T1 acquisition must precede T2 attempt: {lock_events}")

    result = policy.load(ws)
    if "host-a.example.com" not in result["sensitive_hosts"] or "host-b.example.com" not in result["sensitive_hosts"]:
        raise AssertionError(f"Both host additions must persist: {result['sensitive_hosts']}")


def scenario_cmd_init_concurrent_tighten(root: str, private: str) -> None:
    from pbz import cli
    ws = Workspace(root)
    ws.private = private
    lock_events = []
    active_locks = {}
    contention_log = []
    init_acquired = threading.Event()
    tighten_contended = threading.Event()
    resume_init = threading.Event()
    worker_errors = []

    real_lock_cls = tokens.WorkspaceLock

    class InstrumentedLock:
        def __init__(self, directory):
            self.real = real_lock_cls(directory)
            self.thread_name = threading.current_thread().name

        def __enter__(self):
            lock_events.append((self.thread_name, "attempt"))
            res = self.real.__enter__()
            active_locks[self.thread_name] = True
            lock_events.append((self.thread_name, "acquired"))
            if self.thread_name == "InitThread":
                init_acquired.set()
            return res

        def __exit__(self, *exc):
            res = self.real.__exit__(*exc)
            active_locks[self.thread_name] = False
            lock_events.append((self.thread_name, "released"))
            return res

    real_load = policy.load
    real_save = policy.save

    def instrumented_load(ws_arg):
        cur = threading.current_thread().name
        if cur in ("InitThread", "TightenThread"):
            if not active_locks.get(cur):
                raise AssertionError(f"Thread {cur} called policy.load without holding WorkspaceLock")
        return real_load(ws_arg)

    def instrumented_save(ws_arg, pol_arg):
        cur = threading.current_thread().name
        if cur in ("InitThread", "TightenThread"):
            if not active_locks.get(cur):
                raise AssertionError(f"Thread {cur} called policy.save without holding WorkspaceLock")
        if cur == "InitThread":
            if not tighten_contended.wait(timeout=5.0):
                raise TimeoutError("TightenThread failed to hit OS lock contention")
            if not resume_init.wait(timeout=5.0):
                raise TimeoutError("InitThread failed to receive resume signal")
        return real_save(ws_arg, pol_arg)

    def worker_init():
        try:
            with patch("pbz.cli.dpapi.load_key"), \
                 patch("pbz.cli.scan.scan", return_value={"added": {"deny": [], "deny_scripts": []}}), \
                 patch("pbz.cli.mirror.real_model"), \
                 patch("pbz.cli.mirror.load_manifest", return_value=True):
                cli.cmd_init(ws, None, None)
        except Exception as e:
            worker_errors.append(("InitThread", e))

    def worker_tighten():
        try:
            policy.tighten(ws, sensitive_hosts=["concurrent-init.example.com"])
        except Exception as e:
            worker_errors.append(("TightenThread", e))

    started_threads = []
    th1 = threading.Thread(target=worker_init, name="InitThread")
    th2 = threading.Thread(target=worker_tighten, name="TightenThread")

    with patch.object(tokens, "WorkspaceLock", InstrumentedLock), \
         _contention_patch("TightenThread", tighten_contended, contention_log), \
         patch("pbz.policy.load", side_effect=instrumented_load), \
         patch("pbz.policy.save", side_effect=instrumented_save):
        try:
            th1.start()
            started_threads.append(th1)
            if not init_acquired.wait(timeout=5.0):
                raise TimeoutError("InitThread failed to acquire lock")
            th2.start()
            started_threads.append(th2)

            if not tighten_contended.wait(timeout=5.0):
                raise AssertionError("TightenThread did not encounter lock contention")
            if "TightenThread" not in contention_log:
                raise AssertionError("TightenThread not in contention log")
            if not active_locks.get("InitThread"):
                raise AssertionError("InitThread not active")
            if active_locks.get("TightenThread"):
                raise AssertionError("TightenThread should not be active")
        finally:
            resume_init.set()
            tighten_contended.set()
            for th in started_threads:
                th.join(timeout=5.0)

    if worker_errors:
        raise RuntimeError(f"Worker thread error(s): {worker_errors}")

    result = policy.load(ws)
    if "concurrent-init.example.com" not in result["sensitive_hosts"]:
        raise AssertionError("concurrent-init host missing")


SCENARIOS = {
    "scenario_tighten_race_unlocked": scenario_tighten_race_unlocked,
    "scenario_tighten_concurrent_load_save_serialized": scenario_tighten_concurrent_load_save_serialized,
    "scenario_cmd_init_concurrent_tighten": scenario_cmd_init_concurrent_tighten,
}


class PolicyAtomicTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ws = Workspace(self.tmp.name)
        self.ws.private = os.path.join(self.tmp.name, "private")
        os.makedirs(self.ws.private, exist_ok=True)
        open(os.path.join(self.tmp.name, "Canary.pbip"), "w", encoding="utf-8").close()

    def _junction(self, link: str, target: str) -> None:
        from _junction import make_junction
        make_junction(self, link, target)

    def test_save_writes_policy_under_the_workspace(self):
        policy.save(self.ws, {"version": 1, "disclosure": "strict"})
        body = json.loads(open(self.ws.policy_path, encoding="utf-8").read())
        self.assertEqual(body["disclosure"], "strict")
        self.assertEqual(list(Path(self.ws.pbz_dir).glob("*.pbz-tmp")), [])

    def test_pbz_junction_is_refused_and_external_bytes_stay(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        outside = external.name
        secret = os.path.join(outside, "policy.json")
        with open(secret, "wb") as f:
            f.write(b"EXTERNAL")
        self._junction(self.ws.pbz_dir, outside)
        with self.assertRaises(PbzError) as caught:
            policy.save(self.ws, {"version": 1})
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(open(secret, "rb").read(), b"EXTERNAL")
        self.assertEqual(list(Path(outside).glob("*.pbz-tmp")), [])

    def test_policy_leaf_link_is_refused(self):
        external = tempfile.TemporaryDirectory()
        self.addCleanup(external.cleanup)
        secret = os.path.join(external.name, "policy.json")
        with open(secret, "wb") as f:
            f.write(b"EXTERNAL-LEAF")
        os.makedirs(self.ws.pbz_dir, exist_ok=True)
        try:
            os.symlink(secret, self.ws.policy_path)
        except OSError:
            self.skipTest("file symlink is unavailable")
        with self.assertRaises(PbzError) as caught:
            policy.save(self.ws, {"version": 1, "disclosure": "strict"})
        self.assertEqual(caught.exception.code, "UNSAFE_PATH")
        self.assertEqual(open(secret, "rb").read(), b"EXTERNAL-LEAF")
        self.assertEqual(list(Path(self.ws.pbz_dir).glob("*.pbz-tmp")), [])

    def test_failed_replace_keeps_old_policy_bytes(self):
        policy.save(self.ws, {"version": 1, "disclosure": "shape"})
        before = open(self.ws.policy_path, "rb").read()
        real = os.replace

        def boom(src, dst):
            if str(src).endswith(".pbz-tmp"):
                raise OSError("replace")
            return real(src, dst)

        with patch("os.replace", boom):
            with self.assertRaises(OSError):
                policy.save(self.ws, {"version": 1, "disclosure": "strict"})
        self.assertEqual(open(self.ws.policy_path, "rb").read(), before)
        self.assertEqual(list(Path(self.ws.pbz_dir).glob("*.pbz-tmp")), [])

    def test_zero_progress_write_leaves_no_owned_temp(self):
        path = os.path.join(self.tmp.name, "note.txt")
        real = os.write

        def stall(fd, data):
            if getattr(stall, "once", False):
                return real(fd, data)
            stall.once = True
            return 0

        with patch("os.write", stall):
            with self.assertRaises(OSError):
                write_text(path, "NEW", root=self.ws.root)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(list(Path(self.tmp.name).glob("*.pbz-tmp")), [])

    def test_tighten_round_trip(self):
        policy.save(self.ws, dict(policy.DEFAULT))
        added = policy.tighten(self.ws, deny=["**/*.secret"])
        self.assertEqual(added["deny"], ["**/*.secret"])
        body = json.loads(open(self.ws.policy_path, encoding="utf-8").read())
        self.assertIn("**/*.secret", body["deny"])
        again = policy.tighten(self.ws, deny=["**/*.secret"])
        self.assertEqual(again["deny"], [])
        body = json.loads(open(self.ws.policy_path, encoding="utf-8").read())
        self.assertEqual(body["deny"].count("**/*.secret"), 1)

    def test_workspace_via_junction_still_writes(self):
        real = os.path.join(self.tmp.name, "real-ws")
        os.makedirs(real)
        link = os.path.join(self.tmp.name, "linked-ws")
        self.assertFalse(os.path.exists(os.path.join(real, ".pbz")))
        self._junction(link, real)
        ws = Workspace(link)
        open(os.path.join(link, "Canary.pbip"), "w", encoding="utf-8").close()
        policy.save(ws, {"version": 1, "disclosure": "strict"})
        body = json.loads(open(ws.policy_path, encoding="utf-8").read())
        self.assertEqual(body["disclosure"], "strict")

    def _run_supervised_scenario(self, scenario_name: str, root: str, private: str, timeout: float = 15.0) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = SKILL + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--run-scenario", scenario_name, root, private],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            self.assertEqual(proc.returncode, 0, f"Supervised scenario {scenario_name} failed:\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

    def test_tighten_race_unlocked_reproduced(self):
        """Red baseline demonstration: without locking, an interleaved competing tighten is overwritten."""
        self._run_supervised_scenario("scenario_tighten_race_unlocked", self.ws.root, self.ws.private)
        result = policy.load(self.ws)
        self.assertEqual(result["sensitive_hosts"], ["host-a.example.com"])
        self.assertNotIn("host-b.example.com", result["sensitive_hosts"])

    def test_tighten_concurrent_load_save_serialized(self):
        """With WorkspaceLock, concurrent tighten calls serialize; both additions persist."""
        self._run_supervised_scenario("scenario_tighten_concurrent_load_save_serialized", self.ws.root, self.ws.private)
        result = policy.load(self.ws)
        self.assertIn("host-a.example.com", result["sensitive_hosts"])
        self.assertIn("host-b.example.com", result["sensitive_hosts"])

    def test_cmd_init_fresh_workspace_acquires_lock_and_initializes_policy(self):
        """Fresh cmd_init creates private dir and policy.json under WorkspaceLock."""
        from pbz import cli
        fresh_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(fresh_tmp.cleanup)
        ws = Workspace(fresh_tmp.name)
        ws.private = os.path.join(fresh_tmp.name, "fresh-private")
        open(os.path.join(fresh_tmp.name, "Canary.pbip"), "w", encoding="utf-8").close()
        self.assertFalse(os.path.exists(ws.private))
        self.assertFalse(os.path.exists(ws.policy_path))

        lock_events = []
        active_locks = {}
        real_lock_cls = tokens.WorkspaceLock

        class InstrumentedLock:
            def __init__(self, directory):
                self.real = real_lock_cls(directory)
                self.thread_name = threading.current_thread().name

            def __enter__(self):
                lock_events.append((self.thread_name, "attempt"))
                res = self.real.__enter__()
                active_locks[self.thread_name] = True
                lock_events.append((self.thread_name, "acquired"))
                return res

            def __exit__(self, *exc):
                res = self.real.__exit__(*exc)
                active_locks[self.thread_name] = False
                lock_events.append((self.thread_name, "released"))
                return res

        real_load = policy.load
        real_save = policy.save

        def instrumented_load(ws_arg):
            if not active_locks.get(threading.current_thread().name):
                raise AssertionError("policy.load called outside lock during fresh init")
            return real_load(ws_arg)

        def instrumented_save(ws_arg, pol_arg):
            if not active_locks.get(threading.current_thread().name):
                raise AssertionError("policy.save called outside lock during fresh init")
            return real_save(ws_arg, pol_arg)

        with patch.object(tokens, "WorkspaceLock", InstrumentedLock), \
             patch("pbz.policy.load", side_effect=instrumented_load), \
             patch("pbz.policy.save", side_effect=instrumented_save), \
             patch("pbz.cli.dpapi.load_key"), \
             patch("pbz.cli.scan.scan", return_value={"added": {"deny": [], "deny_scripts": []}}), \
             patch("pbz.cli.mirror.real_model"), \
             patch("pbz.cli.mirror.load_manifest", return_value=True):
            res = cli.cmd_init(ws, None, None)

        self.assertEqual(res["verb"], "init")
        self.assertTrue(os.path.exists(ws.private))
        self.assertTrue(os.path.exists(ws.policy_path))
        expected_events = [
            ("MainThread", "attempt"), ("MainThread", "acquired"), ("MainThread", "released"),
            ("MainThread", "attempt"), ("MainThread", "acquired"), ("MainThread", "released"),
        ]
        self.assertEqual(lock_events, expected_events)
        pol = policy.load(ws)
        self.assertEqual(pol["version"], 1)

    def test_cmd_init_concurrent_tighten_serialized(self):
        """cmd_init initial save and policy.tighten synchronize under WorkspaceLock."""
        fresh_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(fresh_tmp.cleanup)
        ws = Workspace(fresh_tmp.name)
        ws.private = os.path.join(fresh_tmp.name, "fresh-private")
        open(os.path.join(fresh_tmp.name, "Canary.pbip"), "w", encoding="utf-8").close()
        self._run_supervised_scenario("scenario_cmd_init_concurrent_tighten", ws.root, ws.private)
        result = policy.load(ws)
        self.assertIn("concurrent-init.example.com", result["sensitive_hosts"])

    def test_two_process_concurrent_tighten(self):
        """Two subprocesses tightening distinct hosts serialize and preserve both entries."""
        barrier = os.path.join(self.tmp.name, "tighten-barrier")
        code = """
import os, sys, time
from pbz import common, policy
ws = common.Workspace(sys.argv[1])
ws.private = sys.argv[2]
ready = sys.argv[3] + '.' + sys.argv[4]
with open(ready, 'w'): pass
while not os.path.exists(sys.argv[3]): time.sleep(.005)
host = sys.argv[5]
policy.tighten(ws, sensitive_hosts=[host])
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = SKILL + os.pathsep + env.get("PYTHONPATH", "")
        children = [
            subprocess.Popen(
                [sys.executable, "-c", code, self.ws.root, self.ws.private, barrier, str(i), f"proc-{i}.example.com"],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            for i in range(2)
        ]
        try:
            import time
            deadline = time.monotonic() + 20
            while not all(os.path.exists(barrier + '.' + str(i)) for i in range(2)):
                if time.monotonic() > deadline:
                    self.fail("tighten children failed to reach barrier")
                time.sleep(.01)
            with open(barrier, "w"):
                pass
            for child in children:
                stdout, stderr = child.communicate(timeout=30)
                self.assertEqual(child.returncode, 0, stderr)
            result = policy.load(self.ws)
            self.assertIn("proc-0.example.com", result["sensitive_hosts"])
            self.assertIn("proc-1.example.com", result["sensitive_hosts"])
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                    child.communicate()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--run-scenario":
        scenario_name = sys.argv[2]
        args = sys.argv[3:]
        scenario_fn = SCENARIOS.get(scenario_name)
        if not scenario_fn:
            sys.stderr.write(f"Unknown scenario: {scenario_name}\n")
            sys.exit(2)
        scenario_fn(*args)
        sys.exit(0)
    unittest.main()
