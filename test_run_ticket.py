"""Tests for run-ticket.sh's git-worktree isolation (debugger #12).

The per-ticket loop must run inside a throwaway git worktree checked out on the work
branch, so the target repo's live working tree (and any concurrent session's HEAD) is
never touched. The real claude loop is swapped for a fake via DEBUGGER_LOOP_CMD.

Run: python3 -m unittest
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ticket

REPO = Path(__file__).resolve().parent
RUN_TICKET = REPO / "run-ticket.sh"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True).stdout.strip()


class RunTicketWorktreeTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"DEBUGGER_SNAPSHOT_DISABLE": "1"})
        self._env.start()
        self.tmp = Path(tempfile.mkdtemp())
        # a real target git repo on branch main with one commit
        self.repo = self.tmp / "target"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.email", "t@t")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "f.txt").write_text("hello\n")
        git(self.repo, "add", "f.txt")
        git(self.repo, "commit", "-m", "init")
        # config dir pointing repo 'r' at the target repo
        self.cfgdir = self.tmp / "config"
        self.cfgdir.mkdir()
        (self.cfgdir / "r.json").write_text(json.dumps(
            {"repo": "r", "workdir": str(self.repo), "default_branch": "main", "branch_prefix": "fix/"}))
        # a staffed ticket for repo 'r'
        self.db = self.tmp / "tickets.db"
        ticket.main(["--db", str(self.db), "init"])
        ticket.main(["--db", str(self.db), "add", "--repo", "r", "--severity", "high", "--symptom", "x"])
        self.id = self._last_id()
        ticket.main(["--db", str(self.db), "stage", str(self.id), "staffed"])
        self.rec = self.tmp / "loop-record.txt"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._env.stop()

    def _last_id(self):
        conn = ticket.connect(self.db)
        try:
            return conn.execute("SELECT id FROM tickets ORDER BY id DESC LIMIT 1").fetchone()[0]
        finally:
            conn.close()

    def run_ticket(self):
        # fake loop: record the cwd it runs in and the branch checked out there
        loop = f"pwd > {self.rec}; git rev-parse --abbrev-ref HEAD >> {self.rec}"
        env = dict(os.environ,
                   DEBUGGER_DB=str(self.db),
                   DEBUGGER_CONFIG_DIR=str(self.cfgdir),
                   DEBUGGER_LOOP_CMD=loop)
        return subprocess.run(["bash", str(RUN_TICKET), str(self.id)],
                              env=env, capture_output=True, text=True)

    def test_loop_runs_in_an_isolated_worktree_on_the_work_branch(self):
        result = self.run_ticket()
        self.assertEqual(result.returncode, 0, result.stderr)
        cwd, branch = self.rec.read_text().split()
        # the loop ran ON the work branch, NOT in the target's live tree
        self.assertEqual(branch, f"fix/{self.id}", "loop must run on the work branch")
        self.assertNotEqual(Path(cwd).resolve(), self.repo.resolve(),
                            "loop must run in a worktree, not the target's main tree")

    def test_main_tree_head_is_untouched_and_worktree_is_cleaned_up(self):
        before = git(self.repo, "rev-parse", "HEAD")
        self.run_ticket()
        # the target repo's main tree never left main, HEAD unchanged
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before)
        # the throwaway worktree is removed (only the main working tree remains)
        wt_lines = [l for l in git(self.repo, "worktree", "list").splitlines() if l.strip()]
        self.assertEqual(len(wt_lines), 1, f"worktree not cleaned up: {wt_lines}")
        # but the work branch persists for human QA
        branches = git(self.repo, "branch", "--list", f"fix/{self.id}")
        self.assertIn(f"fix/{self.id}", branches, "work branch must persist for QA")

    def _status(self, n):
        conn = ticket.connect(self.db)
        try:
            return conn.execute("SELECT status FROM tickets WHERE id=?", (n,)).fetchone()[0]
        finally:
            conn.close()

    def test_worktree_creation_failure_aborts_instead_of_running_in_the_live_tree(self):
        # Occupy the work branch in another worktree so run-ticket's `worktree add` fails
        # both ways. RED before the fix: it fell back to running the loop in the live tree
        # (recording WORKDIR on 'main'); GREEN after: it aborts and the loop never runs.
        other = self.tmp / "other-wt"
        git(self.repo, "worktree", "add", str(other), "-b", f"fix/{self.id}", "main")
        result = self.run_ticket()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.rec.exists(),
                         "on worktree-creation failure the loop must NOT run in the live tree")
        self.assertEqual(git(self.repo, "rev-parse", "--abbrev-ref", "HEAD"), "main",
                         "the live tree must stay on its own branch")
        self.assertEqual(self._status(self.id), "triaged",
                         "an un-isolatable staffed ticket must be reset, not left staffed or worked in place")

    def test_no_workdir_aborts_loudly_without_a_silent_noop(self):
        # No config for the repo -> no resolvable workdir -> isolation impossible. The loop
        # must not run, the ticket must be reset (not silently wedged), and the abort is logged.
        empty_cfg = self.tmp / "empty-config"
        empty_cfg.mkdir()
        loop = f"pwd > {self.rec}; git rev-parse --abbrev-ref HEAD >> {self.rec}"
        env = dict(os.environ, DEBUGGER_DB=str(self.db),
                   DEBUGGER_CONFIG_DIR=str(empty_cfg), DEBUGGER_LOOP_CMD=loop)
        result = subprocess.run(["bash", str(RUN_TICKET), str(self.id)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.rec.exists(), "loop must not run when isolation cannot be established")
        self.assertIn("ABORT", result.stdout + result.stderr, "an un-isolatable run must log, not silently no-op")
        self.assertEqual(self._status(self.id), "triaged", "the ticket must be reset for retry")


if __name__ == "__main__":
    unittest.main()
