"""Tests for CI-7 Option A: git pre-push hook advisory scope and working-tree status."""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


def _clean_git_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Scrub ambient GIT_* variables and global git config so tests are strictly isolated."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    if extra:
        env.update(extra)
    return env


class PrePushHookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[3]
        cls.hook_path = cls.repo_root / "scripts" / "git-hooks" / "pre-push"
        cls.sh_exe = shutil.which("sh")
        if not cls.sh_exe:
            for candidate in (
                r"C:\Program Files\Git\bin\sh.exe",
                r"C:\Program Files\Git\usr\bin\sh.exe",
            ):
                if os.path.exists(candidate):
                    cls.sh_exe = candidate
                    break
        if not cls.sh_exe:
            raise unittest.SkipTest("POSIX sh shell not available")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.test_root = Path(self.tmp.name)
        self.env = _clean_git_env()

        # Initialize test git repository with clean environment
        subprocess.run(["git", "init", str(self.test_root)], check=True, capture_output=True, env=self.env)
        subprocess.run(["git", "-C", str(self.test_root), "config", "user.name", "Tester"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.test_root), "config", "user.email", "tester@test.local"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.test_root), "config", "commit.gpgsign", "false"], check=True, env=self.env)

        # Create skills/powerbi-zerodata dummy tree and mock tests/run_offline.py
        mock_pbi = self.test_root / "skills" / "powerbi-zerodata"
        (mock_pbi / "tests").mkdir(parents=True, exist_ok=True)
        (mock_pbi / "engine").mkdir(parents=True, exist_ok=True)
        (mock_pbi / "engine" / "bin").mkdir(parents=True, exist_ok=True)

        # Mock dotnet and python run_offline so the hook runs fast without building
        (mock_pbi / "tests" / "run_offline.py").write_text("print('MOCK_OFFLINE_RAN')\n", encoding="utf-8")

        # Copy pre-push hook to test repo
        self.test_hook = self.test_root / "scripts" / "git-hooks" / "pre-push"
        self.test_hook.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.hook_path, self.test_hook)

    def _run_hook(
        self,
        mock_dotnet_success: bool = True,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        hook_path: Path | None = None,
    ) -> subprocess.CompletedProcess:
        stub_dir = Path(tempfile.mkdtemp(prefix="pbz-stub-bin-"))
        exit_code = 0 if mock_dotnet_success else 1
        # Create sh script stub for POSIX/Git bash
        stub_sh = stub_dir / "dotnet"
        stub_sh.write_text(f"#!/bin/sh\nexit {exit_code}\n", encoding="utf-8")
        try:
            os.chmod(stub_sh, 0o755)
        except OSError:
            pass
        # Also create cmd stub for Windows
        stub_cmd = stub_dir / "dotnet.cmd"
        stub_cmd.write_text(f"@exit /b {exit_code}\n", encoding="utf-8")

        env = _clean_git_env(extra_env)
        env["PATH"] = str(stub_dir) + os.pathsep + env.get("PATH", "")
        target_cwd = str(cwd or self.test_root)
        target_hook = str(hook_path or self.test_hook)
        try:
            return subprocess.run(
                [self.sh_exe, target_hook],
                cwd=target_cwd,
                capture_output=True,
                text=True,
                env=env,
            )
        finally:
            shutil.rmtree(stub_dir, ignore_errors=True)

    def test_unborn_repo_reports_unknown(self):
        """Unborn HEAD reports unknown tree and unknown dirty status."""
        proc = self._run_hook()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("MOCK_OFFLINE_RAN", proc.stdout)
        self.assertIn("HEAD tree: unknown", proc.stderr)
        self.assertIn("working tree: unknown", proc.stderr)
        self.assertIn("protected hosted CI is the merge authority", proc.stderr)

    def test_clean_repo_reports_clean_and_head_tree(self):
        """Clean working tree reports 40-hex HEAD tree and clean working tree."""
        f = self.test_root / "README.md"
        f.write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.test_root), "add", "."], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.test_root), "commit", "-m", "init"], check=True, env=self.env)

        proc = self._run_hook()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("MOCK_OFFLINE_RAN", proc.stdout)
        self.assertRegex(proc.stderr, r"HEAD tree: [0-9a-f]{40}")
        self.assertIn("working tree: clean", proc.stderr)
        self.assertIn("protected hosted CI is the merge authority", proc.stderr)

    def test_suite_does_not_keep_hook_git_dir(self):
        """The offline suite must not inherit the hook git directory or index."""
        mock = self.test_root / "skills" / "powerbi-zerodata" / "tests" / "run_offline.py"
        mock.write_text(
            "import os\n"
            "print('DIR|' + os.environ.get('GIT_DIR', ''))\n"
            "print('IDX|' + os.environ.get('GIT_INDEX_FILE', ''))\n",
            encoding="utf-8",
        )
        readme = self.test_root / "README.md"
        readme.write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.test_root), "add", "."], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.test_root), "commit", "-m", "init"], check=True, env=self.env)
        git_dir = subprocess.run(
            ["git", "-C", str(self.test_root), "rev-parse", "--absolute-git-dir"],
            check=True, capture_output=True, text=True, env=self.env,
        ).stdout.strip()
        proc = self._run_hook(extra_env={"GIT_DIR": git_dir, "GIT_INDEX_FILE": str(Path(git_dir) / "index")})
        text = proc.stdout.replace("\r\n", "\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("DIR|\n", text)
        self.assertIn("IDX|\n", text)
        self.assertNotIn(git_dir, text)

    def test_unstaged_modification_reports_dirty(self):
        """Unstaged modification causes working tree to report dirty."""
        f = self.test_root / "README.md"
        f.write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.test_root), "add", "."], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.test_root), "commit", "-m", "init"], check=True, env=self.env)

        # Modify file without staging
        f.write_text("hello world\n", encoding="utf-8")
        proc = self._run_hook()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("MOCK_OFFLINE_RAN", proc.stdout)
        self.assertRegex(proc.stderr, r"HEAD tree: [0-9a-f]{40}")
        self.assertIn("working tree: dirty", proc.stderr)
        self.assertIn("protected hosted CI is the merge authority", proc.stderr)

    def test_untracked_file_reports_dirty(self):
        """Untracked file causes working tree to report dirty."""
        f = self.test_root / "README.md"
        f.write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.test_root), "add", "."], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.test_root), "commit", "-m", "init"], check=True, env=self.env)

        # Create untracked file
        (self.test_root / "untracked.txt").write_text("new\n", encoding="utf-8")
        proc = self._run_hook()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("MOCK_OFFLINE_RAN", proc.stdout)
        self.assertIn("working tree: dirty", proc.stderr)

    def test_untracked_files_hidden_config_still_reports_dirty(self):
        """status.showUntrackedFiles=no does not hide untracked files from dirty status."""
        f = self.test_root / "README.md"
        f.write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.test_root), "add", "."], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.test_root), "commit", "-m", "init"], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.test_root), "config", "status.showUntrackedFiles", "no"], check=True, env=self.env)

        # Create untracked file
        (self.test_root / "untracked.txt").write_text("new\n", encoding="utf-8")
        proc = self._run_hook()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("MOCK_OFFLINE_RAN", proc.stdout)
        self.assertIn("working tree: dirty", proc.stderr)

    def test_status_failure_reports_unknown_and_continues(self):
        """When git status fails (e.g. index corrupted), working tree reports unknown and hook proceeds."""
        f = self.test_root / "README.md"
        f.write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.test_root), "add", "."], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.test_root), "commit", "-m", "init"], check=True, env=self.env)

        # Corrupt the index file so git status fails
        (self.test_root / ".git" / "index").write_text("corrupted_junk", encoding="utf-8")
        proc = self._run_hook()
        self.assertEqual(proc.returncode, 0)
        self.assertIn("MOCK_OFFLINE_RAN", proc.stdout)
        self.assertIn("working tree: unknown", proc.stderr)

    def test_linked_worktree_reports_clean_when_clean(self):
        """Linked git worktrees report clean working tree and resolve root correctly."""
        f = self.test_root / "README.md"
        f.write_text("hello\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.test_root), "add", "."], check=True, env=self.env)
        subprocess.run(["git", "-C", str(self.test_root), "commit", "-m", "init"], check=True, env=self.env)

        # Create a linked worktree
        wt_dir = Path(tempfile.mkdtemp(prefix="pbz-test-wt-"))
        self.addCleanup(lambda: shutil.rmtree(wt_dir, ignore_errors=True))
        subprocess.run(["git", "-C", str(self.test_root), "worktree", "add", str(wt_dir), "-b", "branch-wt"], check=True, env=self.env)

        # Set up mock skills tree in the worktree
        wt_mock_pbi = wt_dir / "skills" / "powerbi-zerodata"
        (wt_mock_pbi / "tests").mkdir(parents=True, exist_ok=True)
        (wt_mock_pbi / "engine" / "bin").mkdir(parents=True, exist_ok=True)
        (wt_mock_pbi / "tests" / "run_offline.py").write_text("print('MOCK_OFFLINE_RAN')\n", encoding="utf-8")

        # Copy hook to worktree
        wt_hook = wt_dir / "scripts" / "git-hooks" / "pre-push"
        wt_hook.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(self.hook_path, wt_hook)

        # Simulate git pre-push execution from linked worktree (git sets GIT_DIR)
        git_dir = (wt_dir / ".git").resolve()
        proc = self._run_hook(cwd=wt_dir, hook_path=wt_hook, extra_env={"GIT_DIR": str(git_dir)})
        self.assertEqual(proc.returncode, 0)
        self.assertIn("MOCK_OFFLINE_RAN", proc.stdout)
        self.assertRegex(proc.stderr, r"HEAD tree: [0-9a-f]{40}")
        self.assertIn("working tree: clean", proc.stderr)

    def test_failing_suite_still_exits_nonzero(self):
        """Local hook gate fails with exact runner code if the offline runner fails."""
        mock_pbi = self.test_root / "skills" / "powerbi-zerodata"
        # Make mock run_offline exit 42
        (mock_pbi / "tests" / "run_offline.py").write_text("import sys; sys.exit(42)\n", encoding="utf-8")
        proc = self._run_hook()
        self.assertEqual(proc.returncode, 42)
        self.assertIn("protected hosted CI is the merge authority", proc.stderr)

    def test_build_failure_stops_before_runner(self):
        """If engine build fails, hook aborts before runner executes."""
        proc = self._run_hook(mock_dotnet_success=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("MOCK_OFFLINE_RAN", proc.stdout)

    def test_hook_header_contains_advisory_clarification(self):
        """Header documents CI-7 Option A advisory scope."""
        text = self.hook_path.read_text(encoding="utf-8")
        self.assertIn("CI-7 Option A", text)
        self.assertIn("advisory", text.lower())


if __name__ == "__main__":
    unittest.main()
