"""Tests for dispatch.sh, using a fake launcher so no real claude run happens.

Run: python3 -m unittest
"""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

import ticket

REPO = Path(__file__).resolve().parent
DISPATCH = REPO / "dispatch.sh"
DAEMONIZE = REPO / "daemonize.py"


class DispatchTests(unittest.TestCase):
    def setUp(self):
        # The in-process ticket.main add/stage calls below would otherwise write
        # the real snapshot; disable that here. The dispatch subprocess still
        # refreshes its own (temp) snapshot via report.py, which ignores this flag.
        self._env = mock.patch.dict(os.environ, {"DEBUGGER_SNAPSHOT_DISABLE": "1"})
        self._env.start()
        self.tmp = Path(tempfile.mkdtemp())
        self.db = self.tmp / "tickets.db"
        self.rundir = self.tmp / "run"
        self.rundir.mkdir()
        ticket.main(["--db", str(self.db), "init"])
        # fake launcher: records the id it was asked to launch, writes a pidfile.
        self.launcher = self.tmp / "fake_launcher.sh"
        self.launcher.write_text(textwrap.dedent(f"""\
            #!/usr/bin/env bash
            echo "$1" >> "{self.tmp}/launched.txt"
            echo 999999 > "$2"
        """))
        self.launcher.chmod(0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        self._env.stop()

    # helpers -----------------------------------------------------------------
    def add(self, severity="high", status="open", blocked_by=None):
        argv = ["--db", str(self.db), "add", "--repo", "r", "--severity", severity, "--symptom", "x"]
        if blocked_by is not None:
            argv += ["--blocked-by", str(blocked_by)]
        ticket.main(argv)
        n = self._rows()[-1]["id"]
        if status not in ("open", "closed"):
            ticket.main(["--db", str(self.db), "stage", str(n), status])
        return n

    def add_with_repro(self, repro_cmd, repro_expect, repo="r", severity="high"):
        ticket.main(["--db", str(self.db), "add", "--repo", repo, "--severity", severity,
                     "--symptom", "x", "--repro-cmd", repro_cmd, "--repro-expect", repro_expect])
        return self._rows()[-1]["id"]

    def _write_config(self, repo="r"):
        """A config dir with <repo>.json whose workdir is the hermetic temp dir, so
        `ticket.py triage` can resolve a workdir and run the repro there."""
        cfgdir = self.tmp / "config"
        cfgdir.mkdir(exist_ok=True)
        (cfgdir / f"{repo}.json").write_text(json.dumps({"repo": repo, "workdir": str(self.tmp)}))
        return str(cfgdir)

    def _rows(self):
        conn = ticket.connect(self.db)
        try:
            return conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
        finally:
            conn.close()

    def status_of(self, n):
        conn = ticket.connect(self.db)
        try:
            return conn.execute("SELECT status FROM tickets WHERE id=?", (n,)).fetchone()[0]
        finally:
            conn.close()

    def run_dispatch(self, config_dir=None):
        env = dict(
            os.environ,
            DEBUGGER_DB=str(self.db),
            DEBUGGER_LAUNCHER=str(self.launcher),
            DEBUGGER_RUNDIR=str(self.rundir),
            DEBUGGER_SNAPSHOT=str(self.tmp / "debugger-status.md"),  # hermetic: never the real snapshot
            DEBUGGER_SNAPSHOT_CONFIG=str(self.tmp / "no-such-override.json"),  # nor the machine's override
        )
        if config_dir is not None:
            env["DEBUGGER_CONFIG_DIR"] = config_dir  # hermetic config for the triage stage
        return subprocess.run(["bash", str(DISPATCH)], env=env, capture_output=True, text=True)

    def launched(self):
        f = self.tmp / "launched.txt"
        return f.read_text().split() if f.exists() else []

    # tests -------------------------------------------------------------------
    def test_quiet_tick_launches_nothing(self):
        result = self.run_dispatch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.launched(), [])

    def test_tick_refreshes_the_snapshot(self):
        self.add(status="open")  # something to render
        result = self.run_dispatch()
        self.assertEqual(result.returncode, 0, result.stderr)
        snap = self.tmp / "debugger-status.md"
        self.assertTrue(snap.exists(), "dispatch did not refresh the snapshot")
        self.assertIn("## Needs triage", snap.read_text())

    def test_launch_path_also_refreshes_the_snapshot(self):
        n = self.add(status="triaged")  # reaches the staff+launch exit path
        result = self.run_dispatch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.launched(), [str(n)])
        self.assertTrue((self.tmp / "debugger-status.md").exists())

    def test_snapshot_failure_is_nonfatal(self):
        self.add(status="open")
        bad = self.tmp / "bad_report.sh"
        bad.write_text("#!/usr/bin/env bash\nexit 1\n")
        bad.chmod(0o755)
        env = dict(
            os.environ,
            DEBUGGER_DB=str(self.db),
            DEBUGGER_LAUNCHER=str(self.launcher),
            DEBUGGER_RUNDIR=str(self.rundir),
            DEBUGGER_SNAPSHOT=str(self.tmp / "debugger-status.md"),
            DEBUGGER_REPORT=str(bad),  # report that always fails
        )
        result = subprocess.run(["bash", str(DISPATCH)], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)  # best-effort never wedges dispatch
        self.assertFalse((self.tmp / "debugger-status.md").exists())
        self.assertIn("snapshot refresh failed", result.stdout)

    def test_triage_reproduces_and_staffs_an_open_repro_ticket(self):
        # debugger #3: an OPEN ticket carrying repro_cmd + repro_expect is auto-triaged
        # (reproduced) then staffed in the same run. Red before the fix: no triage stage,
        # so an open ticket never becomes triaged and is never launched.
        cfg = self._write_config()
        n = self.add_with_repro(repro_cmd="echo BUG_PRESENT", repro_expect="BUG_PRESENT")
        result = self.run_dispatch(config_dir=cfg)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.launched(), [str(n)], "an open repro ticket must be triaged then staffed")
        self.assertEqual(self.status_of(n), "staffed")

    def test_triage_leaves_an_open_ticket_without_a_repro_open(self):
        # the bounce direction: a ticket with no deterministic repro cannot be auto-triaged,
        # so it stays open (a person/model must build a reproduction) and nothing is staffed.
        cfg = self._write_config()
        n = self.add(status="open")  # no repro_cmd/repro_expect
        result = self.run_dispatch(config_dir=cfg)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.launched(), [], "a repro-less ticket cannot be auto-triaged")
        self.assertEqual(self.status_of(n), "open")

    def test_staffs_the_highest_severity_triaged_ticket(self):
        low = self.add(severity="low", status="triaged")
        crit = self.add(severity="critical", status="triaged")
        self.run_dispatch()
        self.assertEqual(self.launched(), [str(crit)])       # critical picked first
        self.assertEqual(self.status_of(crit), "staffed")
        self.assertEqual(self.status_of(low), "triaged")     # left for the next tick

    def test_concurrency_skips_when_a_live_daemon_exists(self):
        busy = self.add(status="staffed")
        waiting = self.add(status="triaged")
        sleeper = subprocess.Popen(["sleep", "30"])          # a real, live pid
        (self.rundir / f"{busy}.pid").write_text(str(sleeper.pid))
        try:
            self.run_dispatch()
            self.assertEqual(self.launched(), [])            # nothing new dispatched
            self.assertEqual(self.status_of(waiting), "triaged")
            self.assertEqual(self.status_of(busy), "staffed")
        finally:
            sleeper.terminate()

    def test_writes_the_last_dispatch_marker(self):
        self.run_dispatch()
        marker = self.rundir / "last-dispatch"
        self.assertTrue(marker.exists(), "dispatch did not stamp its last-run marker")

    def test_skips_a_blocked_triaged_ticket_for_a_ready_lower_severity_one(self):
        blocker = self.add(severity="low", status="triaged")            # ready, low
        self.add(severity="critical", status="triaged", blocked_by=blocker)  # blocked, crit
        self.run_dispatch()
        # dependency beats severity: the ready blocker is staffed, not the blocked crit
        self.assertEqual(self.launched(), [str(blocker)])
        self.assertEqual(self.status_of(blocker), "staffed")

    def test_quiet_when_the_only_triaged_ticket_is_blocked(self):
        blocker = self.add(severity="low", status="open")               # not triaged: still blocking
        self.add(severity="high", status="triaged", blocked_by=blocker)  # blocked
        result = self.run_dispatch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.launched(), [])  # nothing ready to staff

    def test_staleness_guard_resets_a_dead_daemon(self):
        wedged = self.add(severity="high", status="staffed")
        (self.rundir / f"{wedged}.pid").write_text("999999")   # a dead pid
        other = self.add(severity="critical", status="triaged")  # higher priority
        self.run_dispatch()
        # the wedged ticket is freed (reset to triaged); the higher-priority ticket runs
        self.assertEqual(self.status_of(wedged), "triaged")
        self.assertEqual(self.launched(), [str(other)])

    def test_daemonize_runs_a_command_detached(self):
        marker = self.tmp / "daemon-marker"
        pidfile = self.tmp / "d.pid"
        subprocess.run(
            ["python3", str(DAEMONIZE), str(pidfile), "bash", "-c", f"echo ok > {marker}"],
            check=True,
        )
        for _ in range(50):  # daemonize returns immediately; the child writes async
            if marker.exists():
                break
            time.sleep(0.1)
        self.assertTrue(marker.exists(), "daemonized command did not run")
        self.assertEqual(marker.read_text().strip(), "ok")


if __name__ == "__main__":
    unittest.main()
