"""Tests for the ticket CLI. Stdlib unittest, each test on its own temp db.

Run: python3 -m unittest
"""
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

import ticket


class TicketTests(unittest.TestCase):
    def setUp(self):
        # Mutations write-through the dashboard snapshot; disable that here so the
        # suite never touches a real snapshot file. The dedicated write-through
        # behaviour is exercised in SnapshotOnMutationTests below.
        self._env = mock.patch.dict(os.environ, {"DEBUGGER_SNAPSHOT_DISABLE": "1"})
        self._env.start()
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db = Path(handle.name)
        self.run_cmd("init")

    def tearDown(self):
        self.db.unlink(missing_ok=True)
        self._env.stop()

    # helpers -----------------------------------------------------------------
    def add(self, repo="my-app", symptom="expected X, got Y", severity="high", **extra):
        argv = ["add", "--repo", repo, "--symptom", symptom, "--severity", severity]
        for key, value in extra.items():
            argv += [f"--{key}", value]
        self.run_cmd(*argv)

    def run_cmd(self, *cli_args):
        """Run the CLI against this test's db, capturing (and discarding) stdout."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            ticket.main(["--db", str(self.db), *cli_args])
        return buf.getvalue()

    def rows(self):
        conn = ticket.connect(self.db)
        try:
            return conn.execute("SELECT * FROM tickets ORDER BY id").fetchall()
        finally:
            conn.close()

    # tests -------------------------------------------------------------------
    def test_add_creates_open_ticket(self):
        self.add(module="poller")
        (row,) = self.rows()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["source"], "manual")  # default
        self.assertEqual(row["module"], "poller")
        self.assertIsNone(row["closed_reason"])

    def test_close_sets_reason_and_keeps_the_row(self):
        self.add()
        self.run_cmd("close", "1", "--reason", "fixed in fix/1-x")
        (row,) = self.rows()  # still exactly one row: closed, not deleted
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["closed_reason"], "fixed in fix/1-x")

    def test_close_records_superseded_by_and_validates_target(self):
        self.add(symptom="dup A")
        self.add(symptom="dup B")
        self.run_cmd("close", "1", "--reason", "duplicate", "--superseded-by", "2")
        self.assertEqual(self.rows()[0]["superseded_by"], 2)
        with self.assertRaises(SystemExit):  # target must exist
            self.run_cmd("close", "2", "--reason", "x", "--superseded-by", "999")

    def test_close_rejects_self_supersede(self):
        self.add()
        with self.assertRaises(SystemExit):
            self.run_cmd("close", "1", "--reason", "x", "--superseded-by", "1")

    def test_close_missing_ticket_exits(self):
        with self.assertRaises(SystemExit):
            self.run_cmd("close", "42", "--reason", "x")

    def test_close_is_idempotent_noop_when_already_closed(self):
        self.add()
        self.run_cmd("close", "1", "--reason", "first")
        out = self.run_cmd("close", "1", "--reason", "second")
        self.assertIn("already closed", out)
        self.assertEqual(self.rows()[0]["closed_reason"], "first")  # unchanged

    def test_list_hides_closed_by_default_and_all_shows_them(self):
        self.add(symptom="open one")
        self.add(symptom="to close")
        self.run_cmd("close", "2", "--reason", "done")
        self.assertIn("#1", self.run_cmd("list"))
        self.assertNotIn("#2", self.run_cmd("list"))
        self.assertIn("#2", self.run_cmd("list", "--all"))

    def test_list_orders_critical_first(self):
        self.add(symptom="low one", severity="low")
        self.add(symptom="crit one", severity="critical")
        ids = [line.split()[0] for line in self.run_cmd("list").splitlines()]
        self.assertEqual(ids, ["#2", "#1"])  # critical (#2) before low (#1), by line order

    def test_list_filters_by_status(self):
        self.add(symptom="stays open")
        self.add(symptom="to close")
        self.run_cmd("close", "2", "--reason", "done")
        out = self.run_cmd("list", "--status", "closed")  # exercises the --status WHERE branch
        self.assertIn("#2", out)
        self.assertNotIn("#1", out)

    def test_list_filters_by_repo(self):
        self.add(repo="my-app", symptom="a")
        self.add(repo="other-app", symptom="b")
        out = self.run_cmd("list", "--repo", "other-app")  # exercises the --repo WHERE branch
        self.assertIn("#2", out)
        self.assertNotIn("#1", out)

    def test_list_reports_no_tickets_when_empty(self):
        self.assertIn("no tickets", self.run_cmd("list"))

    def test_reinit_preserves_existing_tickets(self):
        self.add()
        self.run_cmd("init")  # CREATE TABLE IF NOT EXISTS must not wipe the audit trail
        self.assertEqual(len(self.rows()), 1)

    def test_db_path_resolves_from_env(self):
        with mock.patch.dict(os.environ, {"DEBUGGER_DB": str(self.db)}):
            self.assertEqual(ticket.resolve_db(None), self.db)

    def test_invalid_severity_is_rejected(self):
        with self.assertRaises(SystemExit):
            self.add(severity="urgent")

    def test_stage_moves_ticket_through_statuses(self):
        self.add()
        self.run_cmd("stage", "1", "staffed")
        self.assertEqual(self.rows()[0]["status"], "staffed")
        self.run_cmd("stage", "1", "fix-ready")
        self.assertEqual(self.rows()[0]["status"], "fix-ready")

    def test_stage_cannot_reach_closed(self):
        self.add()
        with self.assertRaises(SystemExit):  # 'closed' is not a valid stage target
            self.run_cmd("stage", "1", "closed")

    def test_stage_refuses_a_closed_ticket(self):
        self.add()
        self.run_cmd("close", "1", "--reason", "done")
        with self.assertRaises(SystemExit):
            self.run_cmd("stage", "1", "staffed")

    def test_update_enriches_triage_fields(self):
        self.add(module=None)
        self.run_cmd("update", "1", "--repro", "python3 -m app x", "--module", "poller", "--severity", "critical")
        row = self.rows()[0]
        self.assertEqual(row["repro"], "python3 -m app x")
        self.assertEqual(row["module"], "poller")
        self.assertEqual(row["severity"], "critical")

    def test_update_requires_at_least_one_field(self):
        self.add()
        with self.assertRaises(SystemExit):
            self.run_cmd("update", "1")

    def test_update_refuses_a_closed_ticket(self):
        self.add()
        self.run_cmd("close", "1", "--reason", "done")
        with self.assertRaises(SystemExit):
            self.run_cmd("update", "1", "--module", "x")

    def test_show_missing_ticket_exits(self):
        with self.assertRaises(SystemExit):
            self.run_cmd("show", "7")

    def test_commands_error_before_init(self):
        empty = Path(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)
        try:
            with self.assertRaises(SystemExit):
                ticket.main(["--db", str(empty), "list"])
        finally:
            empty.unlink(missing_ok=True)

    # migration ---------------------------------------------------------------
    def test_migrate_adds_missing_columns_to_an_existing_db(self):
        """An old tickets.db (pre-intake-gate/pre-dependencies) gains the new
        columns in place on the next command, with existing rows preserved."""
        old = Path(tempfile.NamedTemporaryFile(suffix=".db", delete=False).name)
        try:
            conn = ticket.connect(old)
            conn.executescript(  # the original signed-off schema, no added columns
                "CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "repo TEXT NOT NULL, source TEXT NOT NULL, symptom TEXT NOT NULL, "
                "repro TEXT, module TEXT, severity TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'open', closed_reason TEXT, "
                "superseded_by INTEGER)"
            )
            conn.execute("INSERT INTO tickets (repo, source, symptom, severity) "
                         "VALUES ('r', 'manual', 'old row', 'low')")
            conn.commit()
            conn.close()
            ticket.main(["--db", str(old), "list", "--all"])  # any command triggers migrate
            conn = ticket.connect(old)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(tickets)")}
            row = conn.execute("SELECT * FROM tickets").fetchone()
            conn.close()
            self.assertLessEqual(
                {"requirement_ref", "repro_cmd", "repro_expect", "blocked_by",
                 "audit_verdict", "audit_note"}, cols
            )
            self.assertEqual(row["symptom"], "old row")   # audit trail preserved
            self.assertIsNone(row["requirement_ref"])     # nullable, no backfill
        finally:
            old.unlink(missing_ok=True)

    # intake gate (debugger-intake-gate) --------------------------------------
    def test_add_stores_intake_gate_fields(self):
        self.run_cmd("add", "--repo", "r", "--symptom", "x", "--severity", "low",
                     "--requirement-ref", "docs/spec.md line 3", "--repro-cmd", "pytest -q",
                     "--repro-expect", "exit:nonzero")
        row = self.rows()[0]
        self.assertEqual(row["requirement_ref"], "docs/spec.md line 3")
        self.assertEqual(row["repro_cmd"], "pytest -q")
        self.assertEqual(row["repro_expect"], "exit:nonzero")

    def test_intake_gate_fields_default_null_and_never_block_add(self):
        self.add()  # no gate flags at all: add still succeeds
        row = self.rows()[0]
        self.assertIsNone(row["requirement_ref"])
        self.assertIsNone(row["repro_cmd"])
        self.assertIsNone(row["repro_expect"])

    def test_update_enriches_intake_fields(self):
        self.add()
        self.run_cmd("update", "1", "--requirement-ref", "floor:data-loss",
                     "--repro-cmd", "python3 -m app run", "--repro-expect", "corrupt")
        row = self.rows()[0]
        self.assertEqual(row["requirement_ref"], "floor:data-loss")
        self.assertEqual(row["repro_cmd"], "python3 -m app run")
        self.assertEqual(row["repro_expect"], "corrupt")

    # dependencies (debugger-ticket-dependencies) -----------------------------
    def test_add_with_blocked_by_stores_the_edge(self):
        self.add(symptom="blocker")
        self.run_cmd("add", "--repo", "r", "--symptom", "dependent", "--severity",
                     "high", "--blocked-by", "1")
        self.assertEqual(self.rows()[1]["blocked_by"], 1)

    def test_blocked_by_rejects_self_block(self):
        self.add()
        with self.assertRaises(SystemExit):
            self.run_cmd("update", "1", "--blocked-by", "1")

    def test_blocked_by_rejects_missing_target(self):
        self.add()
        with self.assertRaises(SystemExit):
            self.run_cmd("update", "1", "--blocked-by", "999")

    def test_blocked_by_rejects_a_cycle(self):
        self.add(symptom="a")
        self.add(symptom="b")
        self.run_cmd("update", "2", "--blocked-by", "1")  # 2 waits on 1
        with self.assertRaises(SystemExit):               # 1 waiting on 2 closes the loop
            self.run_cmd("update", "1", "--blocked-by", "2")

    def test_list_orders_ready_ahead_of_blocked_even_at_lower_severity(self):
        self.add(symptom="ready blocker", severity="low")            # #1
        self.run_cmd("add", "--repo", "r", "--symptom", "blocked crit",
                     "--severity", "critical", "--blocked-by", "1")   # #2, waits on #1
        ids = [line.split()[0] for line in self.run_cmd("list").splitlines()]
        self.assertEqual(ids, ["#1", "#2"])  # low-but-ready blocker before crit-but-blocked

    def test_list_ready_filter_excludes_blocked_and_marks_them(self):
        self.add(symptom="blocker", severity="low")                   # #1
        self.run_cmd("add", "--repo", "r", "--symptom", "waiter",
                     "--severity", "critical", "--blocked-by", "1")    # #2
        full = self.run_cmd("list")
        self.assertIn("(blocked by #1)", full)          # blocked marker shown
        ready = self.run_cmd("list", "--ready")
        self.assertIn("#1", ready)
        self.assertNotIn("#2", ready)                   # blocked ticket filtered out

    def test_ready_filter_includes_a_ticket_once_its_blocker_closes(self):
        self.add(symptom="blocker", severity="low")                   # #1
        self.run_cmd("add", "--repo", "r", "--symptom", "waiter",
                     "--severity", "critical", "--blocked-by", "1")    # #2
        self.run_cmd("close", "1", "--reason", "done")
        ready = self.run_cmd("list", "--ready")
        self.assertIn("#2", ready)                      # dependency satisfied -> ready

    # reopen-rate audit (reopen-rate-audit) -----------------------------------
    def test_audit_records_verdict_and_note_on_a_closed_ticket(self):
        self.add()
        self.run_cmd("close", "1", "--reason", "fixed in fix/1-x")
        self.run_cmd("audit", "1", "--verdict", "reopen", "--note", "regressed on empty input")
        row = self.rows()[0]
        self.assertEqual(row["audit_verdict"], "reopen")
        self.assertEqual(row["audit_note"], "regressed on empty input")
        self.assertEqual(row["status"], "closed")  # record-only: audit never reopens the ticket

    def test_audit_note_is_optional(self):
        self.add()
        self.run_cmd("close", "1", "--reason", "fixed")
        self.run_cmd("audit", "1", "--verdict", "holds")
        row = self.rows()[0]
        self.assertEqual(row["audit_verdict"], "holds")
        self.assertIsNone(row["audit_note"])

    def test_audit_refuses_a_non_closed_ticket(self):
        self.add()  # still open
        with self.assertRaises(SystemExit):  # only closed fixes are audited
            self.run_cmd("audit", "1", "--verdict", "holds")
        self.assertIsNone(self.rows()[0]["audit_verdict"])

    def test_audit_rejects_an_unknown_verdict(self):
        self.add()
        self.run_cmd("close", "1", "--reason", "fixed")
        with self.assertRaises(SystemExit):  # argparse choices: holds | reopen only
            self.run_cmd("audit", "1", "--verdict", "maybe")

    def test_re_audit_overwrites_the_prior_verdict(self):
        # a re-review can change its mind (e.g. a 'holds' later found not to hold): the
        # latest audit wins rather than accumulating, since the rate reflects current judgment.
        self.add()
        self.run_cmd("close", "1", "--reason", "fixed")
        self.run_cmd("audit", "1", "--verdict", "holds")
        self.run_cmd("audit", "1", "--verdict", "reopen", "--note", "found a regression")
        row = self.rows()[0]
        self.assertEqual(row["audit_verdict"], "reopen")
        self.assertEqual(row["audit_note"], "found a regression")


class SnapshotOnMutationTests(unittest.TestCase):
    """Snapshot freshness: every mutation writes the snapshot through, best-effort,
    never failing the mutation. Points the write at a temp file so it never touches a
    real snapshot."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.snap = self.tmp / "debugger-status.md"
        self.db = self.tmp / "tickets.db"
        # enable write-through (unset the suite-wide disable) and redirect the path.
        # A work transition also stamps the liveness marker now (ticket-liveness-stamp),
        # so redirect DEBUGGER_RUNDIR too or a real ./run/last-dispatch would be written.
        os.environ.pop("DEBUGGER_SNAPSHOT_DISABLE", None)
        self._env = mock.patch.dict(os.environ, {
            "DEBUGGER_SNAPSHOT": str(self.snap),
            "DEBUGGER_RUNDIR": str(self.tmp / "run"),
            "DEBUGGER_SNAPSHOT_CONFIG": str(self.tmp / "no-such-override.json"),  # never the machine's real override
        })
        self._env.start()
        self._run("init")

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *cli_args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ticket.main(["--db", str(self.db), *cli_args])
        return buf.getvalue()

    def test_add_writes_the_snapshot(self):
        self.assertFalse(self.snap.exists())  # init alone writes nothing
        self._run("add", "--repo", "r", "--symptom", "fresh bug", "--severity", "high")
        self.assertTrue(self.snap.exists())
        self.assertIn("fresh bug", self.snap.read_text())

    def test_stage_and_close_keep_the_snapshot_current(self):
        self._run("add", "--repo", "r", "--symptom", "b", "--severity", "high")
        self._run("stage", "1", "triaged")
        self.assertIn("Queued to staff", self.snap.read_text())
        self._run("close", "1", "--reason", "fixed it")
        self.assertIn("fixed it", self.snap.read_text())

    def test_snapshot_failure_never_fails_the_mutation(self):
        self._run("add", "--repo", "r", "--symptom", "one", "--severity", "low")
        self.assertTrue(self.snap.is_file())  # snap now exists as a regular file
        # writing UNDER a regular file forces a mkdir failure in write_snapshot
        with mock.patch.dict(os.environ, {"DEBUGGER_SNAPSHOT": str(self.snap / "under-a-file.md")}):
            out = self._run("add", "--repo", "r", "--symptom", "still filed", "--severity", "low")
        self.assertIn("filed", out)  # the add succeeded despite the snapshot failure
        conn = ticket.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0], 2)
        finally:
            conn.close()


class LivenessStampOnWorkTests(unittest.TestCase):
    """Liveness stamp (debugger #14): a real work transition (stage, reproduce-triage,
    close) stamps report.dispatch_marker_path(), so the report can tell the queue was
    worked even when dispatch.sh never ran. Filing (add) or enriching (update) a ticket
    is intake, not work, and must NOT stamp: otherwise the "days since last run" clock
    would reset every time a bug is filed and never nudge you to actually run the
    debugger. Hermetic: rundir + snapshot are temp, so nothing touches real files."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.rundir = self.tmp / "run"
        self.snap = self.tmp / "debugger-status.md"
        self.db = self.tmp / "tickets.db"
        self.marker = self.rundir / "last-dispatch"
        # enable side-file writes (unset the suite-wide disable), but redirect the rundir
        # (the marker) and the snapshot to temp so nothing touches the real repo.
        os.environ.pop("DEBUGGER_SNAPSHOT_DISABLE", None)
        self._env = mock.patch.dict(os.environ, {
            "DEBUGGER_RUNDIR": str(self.rundir),
            "DEBUGGER_SNAPSHOT": str(self.snap),
            "DEBUGGER_SNAPSHOT_CONFIG": str(self.tmp / "no-such-override.json"),  # never the machine's real override
        })
        self._env.start()
        self._run("init")

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, *cli_args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            ticket.main(["--db", str(self.db), *cli_args])
        return buf.getvalue()

    def _config_dir(self, repo="r"):
        cfgdir = self.tmp / "config"
        cfgdir.mkdir(exist_ok=True)
        import json
        (cfgdir / f"{repo}.json").write_text(json.dumps({"repo": repo, "workdir": str(self.tmp)}))
        return str(cfgdir)

    def test_stage_stamps_the_dispatch_marker(self):
        self._run("add", "--repo", "r", "--symptom", "b", "--severity", "high")
        self.assertFalse(self.marker.exists(), "filing a ticket must not stamp: add is not working the queue")
        self._run("stage", "1", "staffed")
        self.assertTrue(self.marker.exists(), "a stage transition must stamp the last-worked marker")
        # a parseable YYYY-MM-DD HH:MM stamp, the exact format report.dispatch_status_line reads
        datetime.strptime(self.marker.read_text().strip(), "%Y-%m-%d %H:%M")

    def test_close_stamps_the_dispatch_marker(self):
        self._run("add", "--repo", "r", "--symptom", "b", "--severity", "high")
        self.marker.unlink(missing_ok=True)
        self._run("close", "1", "--reason", "fixed")
        self.assertTrue(self.marker.exists(), "closing a ticket is working the queue and must stamp")

    def test_reproduced_triage_stamps_the_dispatch_marker(self):
        cfg = self._config_dir()
        with mock.patch.dict(os.environ, {"DEBUGGER_CONFIG_DIR": cfg}):
            self._run("add", "--repo", "r", "--severity", "high", "--symptom", "x",
                      "--repro-cmd", "echo BUG_PRESENT", "--repro-expect", "BUG_PRESENT")
            self.marker.unlink(missing_ok=True)
            self._run("triage", "1")
        self.assertEqual(self._status(1), "triaged", "the repro fired, so triage staged it")
        self.assertTrue(self.marker.exists(), "a reproduced triage is working the queue and must stamp")

    def test_add_and_update_do_not_stamp(self):
        self._run("add", "--repo", "r", "--symptom", "b", "--severity", "high")
        self._run("update", "1", "--module", "m")
        self.assertFalse(self.marker.exists(), "add/update are intake, not work: they must not reset the liveness clock")

    def test_snapshot_render_does_not_stamp(self):
        # a scheduled run renders the snapshot read-only via report.py --snapshot even when no
        # debugging happened; rendering must never fake liveness by stamping the marker.
        self._run("add", "--repo", "r", "--symptom", "b", "--severity", "high")
        self.marker.unlink(missing_ok=True)
        import report
        conn = ticket.connect(self.db)
        try:
            report.write_snapshot(conn, str(self.snap), "2026-08-25 12:00")
        finally:
            conn.close()
        self.assertFalse(self.marker.exists(), "rendering the snapshot must not stamp the liveness marker")

    def test_audit_does_not_stamp_the_marker(self):
        # auditing a closed fix is observing the queue, not working it, so it must not
        # reset the last-worked clock (reopen-rate-audit); it still refreshes the snapshot.
        self._run("add", "--repo", "r", "--symptom", "b", "--severity", "high")
        self._run("close", "1", "--reason", "fixed")
        self.marker.unlink(missing_ok=True)
        self._run("audit", "1", "--verdict", "holds")
        self.assertFalse(self.marker.exists(), "audit is observation, not work: it must not stamp the liveness marker")
        # the snapshot still refreshed: the closed fix now counts as audited (1), none reopen
        self.assertIn("Reopen rate: 0/1 audited would reopen (0%)", self.snap.read_text())

    def _status(self, n):
        conn = ticket.connect(self.db)
        try:
            return conn.execute("SELECT status FROM tickets WHERE id=?", (n,)).fetchone()[0]
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
