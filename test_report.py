"""Tests for the report renderer. Run: python3 -m unittest"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import report
import ticket


class ReportTests(unittest.TestCase):
    def setUp(self):
        # Hermetic dispatch marker: point RUNDIR at a temp dir so write_snapshot's
        # dispatch-status line never reads the real repo's run/last-dispatch. Point the
        # snapshot override at a nonexistent file too, so a real config/snapshot.local.json
        # on this machine can never change what the default header/path assertions see.
        self.rundir = Path(tempfile.mkdtemp())
        self._env = mock.patch.dict(os.environ, {
            "DEBUGGER_RUNDIR": str(self.rundir),
            "DEBUGGER_SNAPSHOT_CONFIG": str(self.rundir / "no-such-override.json"),
        })
        self._env.start()
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.db = Path(handle.name)
        self.conn = ticket.connect(self.db)
        self.conn.executescript(ticket.SCHEMA)
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.db.unlink(missing_ok=True)
        self._env.stop()
        shutil.rmtree(self.rundir, ignore_errors=True)

    def _add(self, symptom, severity="high", status="open", repo="my-app",
             module=None, blocked_by=None):
        self.conn.execute(
            "INSERT INTO tickets (repo, source, symptom, module, severity, status, blocked_by) "
            "VALUES (?, 'manual', ?, ?, ?, ?, ?)",
            (repo, symptom, module, severity, status, blocked_by),
        )
        self.conn.commit()

    def test_groups_tickets_under_their_status_headers(self):
        self._add("needs looking at", status="open")
        self._add("being worked", status="staffed")
        self._add("ready for you", status="fix-ready")
        out = report.render(self.conn)
        self.assertIn("## Awaiting your QA (fix-ready)", out)
        self.assertIn("## In progress (staffed)", out)
        self.assertIn("## Needs triage (open)", out)
        self.assertIn("3 open", out)  # fix-ready + staffed + open

    def test_open_count_and_clear_message(self):
        out = report.render(self.conn)
        self.assertIn("0 open · 0 closed", out)
        self.assertIn("_queue clear_", out)

    def test_closed_hidden_by_default_shown_with_all(self):
        self._add("done thing", status="open")
        self.conn.execute(
            "UPDATE tickets SET status='closed', closed_reason='fixed' WHERE id=1"
        )
        self.conn.commit()
        self.assertNotIn("## Closed", report.render(self.conn))
        with_all = report.render(self.conn, show_closed=True)
        self.assertIn("## Closed (1)", with_all)
        self.assertIn("fixed", with_all)

    def test_empty_status_sections_are_omitted(self):
        self._add("only open", status="open")
        out = report.render(self.conn)
        self.assertNotIn("staffed", out)  # no staffed tickets -> no staffed header

    def test_severity_orders_within_a_section(self):
        self._add("low one", severity="low", status="open")
        self._add("crit one", severity="critical", status="open")
        out = report.render(self.conn)
        self.assertLess(out.index("crit one"), out.index("low one"))

    # snapshot mode -----------------------------------------------------------
    def _close(self, ticket_id, reason="fixed", superseded_by=None):
        self.conn.execute(
            "UPDATE tickets SET status='closed', closed_reason=?, superseded_by=? WHERE id=?",
            (reason, superseded_by, ticket_id),
        )
        self.conn.commit()

    def test_snapshot_uses_glance_headers(self):
        self._add("ready for you", status="fix-ready")
        self._add("being worked", status="staffed")
        self._add("in review", status="qa")
        md = report.render_snapshot(self.conn, now="2026-08-19 14:00")
        self.assertIn("## Blocked on you (awaiting QA)", md)
        self.assertIn("## In progress", md)
        self.assertIn("## In QA", md)
        self.assertIn("2026-08-19 14:00", md)

    def test_snapshot_starts_with_the_plain_header_by_default(self):
        md = report.render_snapshot(self.conn, now="now")
        self.assertTrue(md.startswith(report.SNAPSHOT_HEADER))
        self.assertEqual(report.snapshot_path(), report.DEFAULT_SNAPSHOT)

    def test_local_override_supplies_header_and_path(self):
        """A deployment's config/snapshot.local.json (untracked) replaces the header
        wholesale, so a dashboard's own frontmatter and links survive every regeneration,
        and redirects the default snapshot path. The header may be a list of lines."""
        override = self.rundir / "snapshot.local.json"
        override.write_text(json.dumps({
            "path": "~/notes/dashboard/debugger-status.md",
            "header": ["---", "type: status", "---", "", "**Summary**: [[dashboard|Dashboard]] embeds this."],
        }))
        with mock.patch.dict(os.environ, {"DEBUGGER_SNAPSHOT_CONFIG": str(override)}):
            md = report.render_snapshot(self.conn, now="now")
            self.assertTrue(md.startswith("---\ntype: status\n---\n\n**Summary**: [[dashboard|Dashboard]]"))
            self.assertEqual(report.snapshot_path(), "~/notes/dashboard/debugger-status.md")
            # $DEBUGGER_SNAPSHOT still wins over the override's path
            with mock.patch.dict(os.environ, {"DEBUGGER_SNAPSHOT": "/elsewhere.md"}):
                self.assertEqual(report.snapshot_path(), "/elsewhere.md")

    def test_malformed_override_falls_back_to_defaults(self):
        override = self.rundir / "snapshot.local.json"
        override.write_text("{not json")
        with mock.patch.dict(os.environ, {"DEBUGGER_SNAPSHOT_CONFIG": str(override)}):
            self.assertEqual(report.snapshot_header(), report.SNAPSHOT_HEADER)
            self.assertEqual(report.snapshot_path(), report.DEFAULT_SNAPSHOT)

    def test_snapshot_queue_clear_message(self):
        self.assertIn("_Queue clear._", report.render_snapshot(self.conn, now="now"))

    def test_snapshot_recently_closed_newest_first_and_capped(self):
        for i in range(report.RECENT_CLOSED_LIMIT + 3):  # more than the cap
            self._add(f"bug {i}", status="open")
            self._close(i + 1, reason=f"fixed {i}")
        md = report.render_snapshot(self.conn, now="now")
        self.assertIn(f"## Recently closed (latest {report.RECENT_CLOSED_LIMIT})", md)
        newest = report.RECENT_CLOSED_LIMIT + 2  # symptom index of the last-closed (id = newest+1)
        self.assertIn(f"fixed {newest}", md)        # newest present
        self.assertNotIn("fixed 0", md)             # oldest, beyond the cap, dropped
        self.assertLess(md.index(f"#{newest + 1} "), md.index(f"#{newest} "))  # id DESC order

    def test_snapshot_recently_closed_shows_superseded_by(self):
        self._add("dupe", status="open")
        self._add("canonical", status="open")
        self._close(1, reason="duplicate of #2", superseded_by=2)
        self.assertIn("(superseded by #2)", report.render_snapshot(self.conn, now="now"))

    def test_snapshot_is_dash_free(self):
        self._add("dashed — en – bar ― figure ‒ here", status="fix-ready")
        md = report.render_snapshot(self.conn, now="now")
        for dash in "—–―‒":
            self.assertNotIn(dash, md)

    def test_snapshot_stays_compact(self):
        """The snapshot is embedded in a dashboard as a glance surface, so it
        must stay scannable no matter how verbose a symptom or close reason is.
        Guards the decision taken after six closures in one evening rendered a
        3.4KB wall of text: every bullet is clipped, and the recently-closed window
        stays small. Both halves matter, since a low cap on unclipped paragraphs is
        still unreadable, and clipped lines still pile up without a cap."""
        long_text = "verbose " * 80  # ~640 chars, the shape a real symptom takes
        for i in range(report.RECENT_CLOSED_LIMIT + 2):
            self._add(long_text + f"open {i}", status="open")
            self._close(i + 1, reason=long_text + f"closed {i}")
        self._add(long_text + "still open", status="open")
        md = report.render_snapshot(self.conn, now="now")

        overlong = [ln for ln in md.splitlines()
                    if ln.startswith("- ") and len(ln) > report.SNAPSHOT_LINE_CHARS + 3]
        self.assertEqual(overlong, [], f"unclipped snapshot bullet(s): {overlong}")
        self.assertLessEqual(report.RECENT_CLOSED_LIMIT, 3,
                             "the recently-closed window is a glance list, not a changelog")
        self.assertIn("...", md)  # something was actually clipped, so the check has teeth

    def test_write_snapshot_roundtrips_and_leaves_no_tmp(self):
        self._add("t1", status="open")
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "sub" / "debugger-status.md"  # parent auto-created
            out = report.write_snapshot(self.conn, str(target), now="run1")
            self.assertEqual(out, target)
            self.assertIn("## Needs triage", target.read_text())
            self._close(1, reason="done in run2")
            report.write_snapshot(self.conn, str(target), now="run2")
            self.assertIn("done in run2", target.read_text())
            self.assertEqual(list(target.parent.glob("*.tmp")), [])  # no litter

    def test_cli_snapshot_writes_file(self):
        self._add("t1", status="open")
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "cc.md"
            report.main(["--db", str(self.db), "--snapshot", str(target)])
            self.assertTrue(target.exists())
            self.assertIn("## Needs triage", target.read_text())

    def test_cli_snapshot_flag_resolves_default_and_none(self):
        # bare --snapshot parses to "" and main resolves it through snapshot_path()
        self.assertEqual(report.build_parser().parse_args(["--snapshot"]).snapshot, "")
        self.assertIsNone(report.build_parser().parse_args([]).snapshot)
        self._add("t1", status="open")
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "cc.md"
            with mock.patch.dict(os.environ, {"DEBUGGER_SNAPSHOT": str(target)}):
                report.main(["--db", str(self.db), "--snapshot"])
            self.assertIn("## Needs triage", target.read_text())

    # reopen-rate audit (reopen-rate-audit) -----------------------------------
    def _audit(self, ticket_id, verdict, note=None):
        self.conn.execute(
            "UPDATE tickets SET audit_verdict=?, audit_note=? WHERE id=?",
            (verdict, note, ticket_id),
        )
        self.conn.commit()

    def test_reopen_line_says_none_when_no_closed_fix_is_audited(self):
        self._add("done", status="open")
        self._close(1)  # closed but not audited
        msg = "no closed fixes audited yet"
        self.assertIn(msg, report.render(self.conn))
        self.assertIn(msg, report.render_snapshot(self.conn, now="now"))

    def test_reopen_line_computes_the_rate_over_audited_closes(self):
        for i in range(4):
            self._add(f"b{i}", status="open")
            self._close(i + 1)
        self._audit(1, "holds")
        self._audit(2, "holds")
        self._audit(3, "reopen")  # 1 of 3 audited would reopen; #4 left un-audited
        out = report.render(self.conn)
        self.assertIn("1/3 audited would reopen (33%)", out)
        self.assertIn("1/3 audited would reopen (33%)", report.render_snapshot(self.conn, now="now"))

    def test_reopen_stats_ignores_unaudited_and_open_tickets(self):
        self._add("still open", status="open")          # open, not counted
        self._add("closed unaudited", status="open")
        self._close(2)                                   # closed, not audited
        self.assertEqual(report.reopen_stats(self.conn), (0, 0))

    def test_closed_all_listing_shows_the_audit_verdict(self):
        self._add("x", status="open")
        self._close(1, reason="fixed")
        self._audit(1, "reopen", note="did not hold")
        out = report.render(self.conn, show_closed=True)
        self.assertIn("[audit: reopen]", out)

    def test_reopen_line_is_dash_free_in_the_snapshot(self):
        self._add("x", status="open")
        self._close(1)
        self._audit(1, "reopen")
        md = report.render_snapshot(self.conn, now="now")
        for dash in "—–―‒":
            self.assertNotIn(dash, md)

    # dependency ordering + blocked marker (debugger-ticket-dependencies) ------
    def test_snapshot_orders_ready_ahead_of_blocked_and_marks_it(self):
        self._add("ready blocker", severity="low", status="open")           # id 1
        self._add("blocked waiter", severity="critical", status="open", blocked_by=1)  # id 2
        md = report.render_snapshot(self.conn, now="now")
        self.assertLess(md.index("ready blocker"), md.index("blocked waiter"))  # ready first
        self.assertIn("(blocked by #1)", md)

    def test_blocked_marker_drops_once_blocker_closes(self):
        self._add("blocker", severity="low", status="open")                 # id 1
        self._add("waiter", severity="high", status="open", blocked_by=1)   # id 2
        self.conn.execute("UPDATE tickets SET status='closed', closed_reason='x' WHERE id=1")
        self.conn.commit()
        md = report.render_snapshot(self.conn, now="now")
        self.assertNotIn("(blocked by #1)", md)  # dependency satisfied -> not blocked

    # dispatch-pause visibility (separate-debugger-routine Section 3) ----------
    def test_dispatch_status_line_when_marker_missing(self):
        line = report.dispatch_status_line(self.rundir / "last-dispatch", datetime(2026, 8, 21, 12, 0))
        self.assertIn("not yet run", line)

    def test_dispatch_status_line_when_fresh(self):
        marker = self.rundir / "last-dispatch"
        now = datetime(2026, 8, 21, 12, 0)
        marker.write_text((now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"))
        line = report.dispatch_status_line(marker, now)
        self.assertIn("last ran", line)
        self.assertNotIn("paused", line)

    def test_dispatch_status_line_when_stale_says_paused(self):
        marker = self.rundir / "last-dispatch"
        now = datetime(2026, 8, 21, 12, 0)
        marker.write_text((now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M"))
        line = report.dispatch_status_line(marker, now)
        self.assertIn("paused", line)
        self.assertIn("3d ago", line)

    def test_snapshot_includes_the_dispatch_status_line(self):
        (self.rundir / "last-dispatch").write_text(
            datetime.now().strftime("%Y-%m-%d %H:%M")
        )
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "cc.md"
            report.write_snapshot(self.conn, str(target), now="now")
            self.assertIn("Dispatch last ran", target.read_text())


if __name__ == "__main__":
    unittest.main()
