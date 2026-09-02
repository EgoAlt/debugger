#!/usr/bin/env python3
"""ticket: manual intake and queue management for the debugger system.

A thin, dependency-free CLI over the debugger's SQLite ticket queue. Standard
library only.

Verbs: init, add, list, show, triage, stage, update, close, audit. There is
deliberately no delete: a ticket is closed with a reason (and optionally marked
superseded), never removed, so the queue keeps a full audit trail (the
loop-orchestrator "never delete, only close" discipline). `audit` records the
other half of that discipline: a post-close re-review verdict that report.py
turns into a reopen rate (the self-observability signal).

Usage:
    ./ticket.py init
    ./ticket.py add --repo my-app --severity high --symptom "expected X, got Y"
    ./ticket.py list
    ./ticket.py show 3
    ./ticket.py close 3 --reason "fixed in fix/3-poller-skip"

The database path defaults to ./tickets.db beside this script; override with
--db PATH or the DEBUGGER_DB environment variable (so the dispatcher and tests
can point at their own store from any working directory).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

import sqlite3

SEVERITIES = ("low", "medium", "high", "critical")
SOURCES = ("manual", "email", "logs", "tests")
STATUSES = ("open", "triaged", "staffed", "fix-ready", "qa", "closed")
# statuses `stage` may set: the non-terminal transitions the loop drives a ticket
# through. Reaching 'closed' stays exclusive to `close` (which requires a reason),
# and 'open' is the initial state only.
STAGES = ("triaged", "staffed", "fix-ready", "qa")
# The post-close re-review verdicts `audit` may record (reopen-rate-audit): a fresh
# read of a closed fix either finds it still holds, or would reopen it. That count is
# the reopen-rate signal report.py surfaces.
AUDIT_VERDICTS = ("holds", "reopen")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo            TEXT    NOT NULL,
    source          TEXT    NOT NULL CHECK (source IN ('manual','email','logs','tests')),
    symptom         TEXT    NOT NULL,
    repro           TEXT,
    module          TEXT,
    severity        TEXT    NOT NULL CHECK (severity IN ('low','medium','high','critical')),
    status          TEXT    NOT NULL DEFAULT 'open'
                            CHECK (status IN ('open','triaged','staffed','fix-ready','qa','closed')),
    closed_reason   TEXT,
    superseded_by   INTEGER REFERENCES tickets(id),
    -- Intake gate (debugger-intake-gate): the three questions the filer answers
    -- at add-time, all optional and none blocking `add`. requirement_ref holds a
    -- citation or `floor:<class>` (defect-vs-build); repro_cmd/repro_expect make
    -- triage deterministic when both are present. NULL is a valid state for all.
    requirement_ref TEXT,
    repro_cmd       TEXT,
    repro_expect    TEXT,
    -- Inter-ticket dependency (debugger-ticket-dependencies): the id of the ticket
    -- that must resolve first. "A blocks B" is stored as B.blocked_by = A. NULL =
    -- not blocked. A ticket whose blocker is not yet closed is not ready to staff.
    blocked_by      INTEGER REFERENCES tickets(id),
    -- Reopen-rate audit (reopen-rate-audit): a post-close re-review verdict recorded
    -- by `audit`. audit_verdict is 'holds' or 'reopen' (the fresh-read judgment of
    -- whether the fix still holds); audit_note is one line on why. Both NULL until a
    -- closed ticket is audited. report.py counts these into a reopen rate.
    audit_verdict   TEXT,
    audit_note      TEXT
);
"""

# Columns added after the original signed-off schema (spec: "additions later are
# cheap ALTER TABLEs"). migrate() adds any that an existing tickets.db lacks, so a
# live DB upgrades in place on the next command with no backfill (all are nullable).
ADDED_COLUMNS = (
    ("requirement_ref", "TEXT"),
    ("repro_cmd", "TEXT"),
    ("repro_expect", "TEXT"),
    ("blocked_by", "INTEGER REFERENCES tickets(id)"),
    ("audit_verdict", "TEXT"),
    ("audit_note", "TEXT"),
)

# critical first: the order the dispatcher staffs the queue in.
SEVERITY_ORDER = (
    "CASE severity WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
    "WHEN 'medium' THEN 2 ELSE 3 END"
)

# 1 when a ticket is blocked by another that has not yet closed, else 0. Used to
# sort ready work ahead of blocked work (debugger-ticket-dependencies): a blocking
# lower-severity ticket therefore lands before the higher-severity one waiting on
# it, and dispatch skips a ticket that cannot land yet. A closed blocker satisfies
# the dependency, so it stops counting as blocking.
BLOCKED_EXPR = (
    "CASE WHEN blocked_by IS NOT NULL AND "
    "(SELECT status FROM tickets AS blk WHERE blk.id = tickets.blocked_by) != 'closed' "
    "THEN 1 ELSE 0 END"
)


def resolve_db(arg: str | None) -> Path:
    """Pick the database path: --db, then $DEBUGGER_DB, then ./tickets.db."""
    if arg:
        return Path(arg).expanduser()
    env = os.environ.get("DEBUGGER_DB")
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent / "tickets.db"


def die(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def connect(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")  # tolerate a concurrent CLI/dispatcher access
    except sqlite3.OperationalError as exc:
        die(f"cannot open database {path}: {exc}")
    conn.row_factory = sqlite3.Row
    return conn


def require_ticket(conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if row is None:
        die(f"no ticket #{ticket_id}")
    return row


def migrate(conn: sqlite3.Connection) -> None:
    """Add any post-signoff columns an existing tickets.db lacks (idempotent).

    A fresh DB gets them from SCHEMA; an existing one gets them here on the next
    command. All are nullable, so no backfill is needed and an old ticket simply
    keeps NULL (stays manual-tier, unblocked). Cheap: one PRAGMA plus a conditional
    ALTER per missing column.
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
    added = False
    for name, decl in ADDED_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {decl}")
            added = True
    if added:
        conn.commit()


def ensure_initialized(conn: sqlite3.Connection, path: Path) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tickets'"
    ).fetchone()
    if not exists:
        die(f"{path} is not initialized; run: ticket --db {path} init")
    migrate(conn)  # bring an existing DB up to the current column set in place


def refresh_snapshot(conn: sqlite3.Connection) -> None:
    """Write-through the dashboard snapshot after a mutation.

    Every ticket mutation keeps the dashboard current the instant it changes, rather
    than waiting for the next dispatch/report tick. Best-effort by construction: a
    snapshot failure is logged and swallowed so it can never fail the mutation itself.
    Disabled by DEBUGGER_SNAPSHOT_DISABLE (the test suite, and any caller that must not
    write the snapshot); the target is report.snapshot_path() ($DEBUGGER_SNAPSHOT, else
    the local override, else report.py's DEFAULT_SNAPSHOT).
    """
    if os.environ.get("DEBUGGER_SNAPSHOT_DISABLE"):
        return
    try:
        import report  # lazy: report imports ticket at load, so import here dodges the cycle
        from datetime import datetime

        report.write_snapshot(conn, report.snapshot_path(), datetime.now().strftime("%Y-%m-%d %H:%M"))
    except Exception as exc:  # best-effort: never let a snapshot failure fail the mutation
        print(f"warning: snapshot refresh failed: {exc}", file=sys.stderr)


def mark_worked() -> None:
    """Stamp the last-dispatch marker: record that the debugger just worked the queue.

    Liveness stamp (debugger #14): report.py reads report.dispatch_marker_path() to tell
    how long the queue has gone unworked. The stamp used to be written only by
    dispatch.sh (the detached dispatcher), so the now-primary run paths (run-ticket.sh's
    stage transitions, the driver skill's triage/stage/close through this CLI, a manual
    close) never stamped it, and the report said "never run" while the queue was being
    worked. So the real work transitions stamp it here. A state transition (stage,
    reproduce-triage, close) means the queue was worked; filing (add) or enriching
    (update) a ticket is intake, not work, and must not stamp, or the "days since last
    run" clock would reset on every filed bug and never nudge you to run the debugger.
    Best-effort and hermetic-gated exactly like refresh_snapshot: it never fails the
    mutation, and skips when DEBUGGER_SNAPSHOT_DISABLE is set so the suite writes no side
    files. The marker path is report.dispatch_marker_path() (the single source of truth,
    honouring DEBUGGER_RUNDIR), in the same "%Y-%m-%d %H:%M" format dispatch.sh writes and
    report.dispatch_status_line reads.
    """
    if os.environ.get("DEBUGGER_SNAPSHOT_DISABLE"):
        return
    try:
        import report  # lazy: report imports ticket at load, so import here dodges the cycle
        from datetime import datetime

        marker = report.dispatch_marker_path()
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now().strftime("%Y-%m-%d %H:%M"))
    except Exception as exc:  # best-effort: a stamp failure must never fail the mutation
        print(f"warning: dispatch marker stamp failed: {exc}", file=sys.stderr)


def resolve_blocked_by(conn: sqlite3.Connection, ticket_id: int | None, blocked_by: int) -> None:
    """Validate a proposed blocked_by edge before it is written.

    Rejects a self-block, a non-existent blocker, and any cycle (following the
    blocked_by chain from the proposed blocker must not lead back to ticket_id),
    since a cycle would leave both tickets permanently unready and wedge the queue.
    ticket_id is None at `add` time (the row has no id yet, so it cannot be in any
    existing chain, and only existence is checked).
    """
    if blocked_by == ticket_id:
        die("a ticket cannot block itself")
    require_ticket(conn, blocked_by)  # the blocker must exist
    if ticket_id is None:
        return
    seen: set[int] = set()
    cur: int | None = blocked_by
    while cur is not None and cur not in seen:
        if cur == ticket_id:
            die(f"#{blocked_by} would create a dependency cycle with #{ticket_id}")
        seen.add(cur)
        row = conn.execute("SELECT blocked_by FROM tickets WHERE id = ?", (cur,)).fetchone()
        cur = row["blocked_by"] if row else None


def cmd_init(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    conn.executescript(SCHEMA)
    migrate(conn)  # a DB predating a column set still gains it on re-init
    conn.commit()
    print(f"initialized {args.db_path}")


def cmd_add(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if args.blocked_by is not None:
        resolve_blocked_by(conn, None, args.blocked_by)  # a new row has no id yet
    cur = conn.execute(
        "INSERT INTO tickets (repo, source, symptom, repro, module, severity, "
        "requirement_ref, repro_cmd, repro_expect, blocked_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            args.repo, args.source, args.symptom, args.repro, args.module, args.severity,
            args.requirement_ref, args.repro_cmd, args.repro_expect, args.blocked_by,
        ),
    )
    conn.commit()
    print(f"#{cur.lastrowid} filed ({args.severity}, {args.repo})")
    refresh_snapshot(conn)


def cmd_list(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    where: list[str] = []
    params: list[object] = []
    if args.status:
        where.append("status = ?")
        params.append(args.status)
    elif not args.all:
        where.append("status != 'closed'")
    if args.repo:
        where.append("repo = ?")
        params.append(args.repo)
    if args.ready:
        where.append(f"{BLOCKED_EXPR} = 0")  # only tickets whose blocker (if any) is closed
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    # Ready work sorts ahead of blocked work, then critical-first, then id: a
    # blocking lower-severity ticket lands before the higher one waiting on it.
    rows = conn.execute(
        f"SELECT *, ({BLOCKED_EXPR}) AS is_blocked FROM tickets {clause} "
        f"ORDER BY is_blocked, {SEVERITY_ORDER}, id",
        params,
    ).fetchall()
    if not rows:
        print("no tickets")
        return
    for r in rows:
        symptom = r["symptom"]
        if len(symptom) > 58:
            symptom = symptom[:57] + "…"
        module = r["module"] or "-"
        blocked = f' (blocked by #{r["blocked_by"]})' if r["is_blocked"] else ""
        print(
            f'#{r["id"]:<4} {r["severity"]:<8} {r["status"]:<10} '
            f'{r["repo"]:<14} {module:<16} {symptom}{blocked}'
        )


def cmd_show(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = require_ticket(conn, args.id)
    for key in row.keys():
        value = row[key]
        print(f"{key:<14} {'-' if value is None else value}")


def cmd_stage(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = require_ticket(conn, args.id)
    if row["status"] == "closed":
        die(f"#{args.id} is closed; reopen intentionally rather than staging it")
    conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (args.status, args.id))
    conn.commit()
    print(f"#{args.id} -> {args.status}")
    mark_worked()  # a state transition means the queue was worked (stamp before the snapshot reads it)
    refresh_snapshot(conn)


def _repo_workdir(repo: str) -> str | None:
    """Resolve a ticket's repo to its working directory via config/<repo>.json, or None
    if the config or workdir is missing. Config lives beside this script (config/README.md)."""
    cfg_dir = os.environ.get("DEBUGGER_CONFIG_DIR") or str(Path(__file__).resolve().parent / "config")
    cfg = Path(cfg_dir) / f"{repo}.json"
    try:
        data = json.loads(cfg.read_text())
    except Exception:
        return None
    wd = os.path.expanduser(str(data.get("workdir", "")))
    return wd if wd and os.path.isdir(wd) else None


def _signal_present(expect: str, returncode: int, output: str) -> bool:
    """Whether the repro's 'bug present' signal fired (see `add --repro-expect`):
    'exit:nonzero' means the command exited non-zero; anything else is a substring that
    must appear in the command's combined stdout+stderr."""
    e = (expect or "").strip()
    if e == "exit:nonzero":
        return returncode != 0
    return bool(e) and e in output


def cmd_triage(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Deterministically reproduce-or-bounce one open ticket (playbook/triage.md).

    A ticket carrying repro_cmd + repro_expect is triageable mechanically: run the
    command in its repo's workdir and assert the 'bug present' signal. On a match, stage
    it triaged (eligible for the fix loop). Otherwise leave it open and report that it
    needs manual (model/human) triage, i.e. a reproduction still to be built. Never
    stages a ticket it could not make fail on demand (the red-capable rule, at triage).
    This is the deterministic subset the dispatcher runs before staffing, and the same
    capability the on-demand skill uses; it is not a substitute for model triage of a
    ticket that has no repro yet.

    RED-CAPABILITY IS NOT CHECKED HERE, and cannot be: a substring signal that fires on
    healthy output (debugger #15: 'DUE' from a correctly-working tool) is indistinguishable
    from one that fires only when broken without a known-good baseline to diff against, and
    substring is the preferred repro form, so this could not reject it without false bounces.
    `playbook/triage.md` owns that judgment (would the fix flip this repro red->green?); the
    per-ticket loop's red-capable-test-first invariant is the mechanical backstop that a
    non-red ticket which slips through here still hits before any fix ships.

    SECURITY INVARIANT: this executes `repro_cmd` via `bash -c`, so a `repro_cmd` must only
    ever be set from a TRUSTED source (a manual operator/model intake), never populated
    automatically from untrusted content (an email body, a scraped log line). `add`/`update`
    take it only from an explicit flag today; keep it that way, or an automated intake that
    filled `repro_cmd` from untrusted input would become autonomous code execution here."""
    row = require_ticket(conn, args.id)
    if row["status"] != "open":
        print(f"#{args.id}: status is {row['status']}, not open; nothing to triage")
        return
    cmd, expect = row["repro_cmd"], row["repro_expect"]
    if not (cmd and expect):
        print(f"#{args.id}: no repro_cmd/repro_expect, needs manual triage; left open")
        return
    workdir = _repo_workdir(row["repo"])
    if not workdir:
        print(f"#{args.id}: repo '{row['repo']}' has no resolvable workdir; left open")
        return
    try:
        proc = subprocess.run(["bash", "-c", cmd], cwd=workdir,
                              capture_output=True, text=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        print(f"#{args.id}: repro exceeded {args.timeout}s; left open (manual triage)")
        return
    if _signal_present(expect, proc.returncode, proc.stdout + proc.stderr):
        conn.execute("UPDATE tickets SET status = 'triaged' WHERE id = ?", (args.id,))
        conn.commit()
        print(f"#{args.id}: reproduced (signal fired), staged triaged")
        mark_worked()  # a reproduced triage is the queue being worked
        refresh_snapshot(conn)
    else:
        print(f"#{args.id}: repro ran but the expected signal did not fire; left open (manual triage)")


def cmd_update(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = require_ticket(conn, args.id)
    if row["status"] == "closed":
        die(f"#{args.id} is closed; not editing a closed ticket")
    if args.blocked_by is not None:
        resolve_blocked_by(conn, args.id, args.blocked_by)
    updates = {
        "repro": args.repro, "module": args.module, "severity": args.severity,
        "requirement_ref": args.requirement_ref, "repro_cmd": args.repro_cmd,
        "repro_expect": args.repro_expect, "blocked_by": args.blocked_by,
    }
    setting = {col: val for col, val in updates.items() if val is not None}
    if not setting:
        die("update needs at least one field to set")
    assignments = ", ".join(f"{col} = ?" for col in setting)  # col names are fixed literals, not input
    conn.execute(f"UPDATE tickets SET {assignments} WHERE id = ?", (*setting.values(), args.id))
    conn.commit()
    print(f"#{args.id} updated ({', '.join(setting)})")
    refresh_snapshot(conn)


def cmd_close(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    row = require_ticket(conn, args.id)
    if row["status"] == "closed":
        print(f"#{args.id} already closed ({row['closed_reason']})")
        return
    if args.superseded_by is not None:
        if args.superseded_by == args.id:
            die("a ticket cannot supersede itself")
        require_ticket(conn, args.superseded_by)  # the replacement must exist
    conn.execute(
        "UPDATE tickets SET status = 'closed', closed_reason = ?, superseded_by = ? "
        "WHERE id = ?",
        (args.reason, args.superseded_by, args.id),
    )
    conn.commit()
    print(f"#{args.id} closed")
    mark_worked()  # closing a ticket is the queue being worked
    refresh_snapshot(conn)


def cmd_audit(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Record a post-close re-review verdict on a closed ticket (the reopen-rate signal).

    The loop-orchestrator's self-observability half: periodically re-read the last N
    closed fixes with fresh eyes and record, per ticket, whether you would reopen it.
    report.py turns those verdicts into a reopen rate, the only real signal it is safe
    to run unattended (gauntlet-loop: an agent left to grade its own work drifts, so the
    re-read wants fresh, ideally blind, context, not the closer's). The holds/reopen
    judgment is the reviewer's; this verb only records it.

    Record-only by design: a 'reopen' verdict flags a fix that did not hold, it does not
    itself reopen the ticket. Reopening as an action stays a separate operator decision,
    so the measurement never silently mutates the queue it is measuring. Unlike a stage
    or close, an audit is observing the queue, not working it, so it does not stamp the
    last-worked marker (that clock tracks dispatch work); it does refresh the snapshot,
    since the reopen rate it feeds is a glance surface."""
    row = require_ticket(conn, args.id)
    if row["status"] != "closed":
        die(f"#{args.id} is {row['status']}, not closed; only closed fixes are audited")
    conn.execute(
        "UPDATE tickets SET audit_verdict = ?, audit_note = ? WHERE id = ?",
        (args.verdict, args.note, args.id),
    )
    conn.commit()
    note = f": {args.note}" if args.note else ""
    print(f"#{args.id} audited: {args.verdict}{note}")
    refresh_snapshot(conn)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ticket",
        description="Ticket queue for the debugger system (stdlib-only).",
    )
    parser.add_argument("--db", help="path to the ticket db (default: ./tickets.db or $DEBUGGER_DB)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create the ticket database")
    p_init.set_defaults(func=cmd_init, needs_db=False)

    p_add = sub.add_parser("add", help="file a new ticket (the manual intake adapter)")
    p_add.add_argument("--repo", required=True, help="target repo key, matching config/<repo>.json")
    p_add.add_argument("--symptom", required=True, help="expected vs actual, in one line")
    p_add.add_argument("--severity", required=True, choices=SEVERITIES)
    p_add.add_argument("--source", default="manual", choices=SOURCES, help="how the bug was found")
    p_add.add_argument("--repro", help="command or steps to reproduce, if known")
    p_add.add_argument("--module", help="best-guess module or area")
    # Intake gate (debugger-intake-gate). Optional and never blocking: an unfiled
    # bug is worse than an untriaged one, so a filer who skips these still files.
    p_add.add_argument(
        "--requirement-ref", dest="requirement_ref",
        help="Q1 (defect vs build): cite where this was already required (spec line, "
             "rule number, docstring, the tool's stated purpose), or floor:<crash|"
             "data-loss|wrong-output|security>. Absent means it is a build, not a defect. "
             "A ref that just restates what the code already does on purpose is not a defect "
             "(debugger #15): the requirement must be one the code currently VIOLATES.",
    )
    p_add.add_argument(
        "--repro-cmd", dest="repro_cmd",
        help="Q2a (triage tier): one shell command, run in the repo's workdir, that "
             "exercises the bug. With --repro-expect, makes triage deterministic.",
    )
    p_add.add_argument(
        "--repro-expect", dest="repro_expect",
        help="Q2b: the signal that means the bug is present: 'exit:nonzero' or a string "
             "that must appear in the command's output. Prefer the string form. It must be "
             "RED-CAPABLE: present only when the code is broken, absent once fixed. A string "
             "that also appears in healthy output (debugger #15: 'DUE' from a correctly-"
             "working tool) is not a bug signal, it matches health.",
    )
    p_add.add_argument(
        "--blocked-by", dest="blocked_by", type=int, metavar="ID",
        help="id of a ticket that must resolve before this one can be worked",
    )
    p_add.set_defaults(func=cmd_add, needs_db=True)

    p_list = sub.add_parser("list", help="list tickets (open ones by default)")
    p_list.add_argument("--status", choices=STATUSES, help="filter by exact status")
    p_list.add_argument("--repo", help="filter by repo")
    p_list.add_argument("--all", action="store_true", help="include closed tickets")
    p_list.add_argument(
        "--ready", action="store_true",
        help="only tickets ready to work: not blocked by an unresolved dependency",
    )
    p_list.set_defaults(func=cmd_list, needs_db=True)

    p_show = sub.add_parser("show", help="show one ticket in full")
    p_show.add_argument("id", type=int)
    p_show.set_defaults(func=cmd_show, needs_db=True)

    p_triage = sub.add_parser(
        "triage", help="deterministically reproduce-or-bounce one open ticket (auto-triage)")
    p_triage.add_argument("id", type=int)
    p_triage.add_argument("--timeout", type=int, default=120,
                          help="seconds to allow the repro command (default 120)")
    p_triage.set_defaults(func=cmd_triage, needs_db=True)

    p_stage = sub.add_parser("stage", help="move a ticket to a non-terminal status")
    p_stage.add_argument("id", type=int)
    p_stage.add_argument("status", choices=STAGES)
    p_stage.set_defaults(func=cmd_stage, needs_db=True)

    p_update = sub.add_parser("update", help="enrich a ticket's triage/intake fields")
    p_update.add_argument("id", type=int)
    p_update.add_argument("--repro")
    p_update.add_argument("--module")
    p_update.add_argument("--severity", choices=SEVERITIES)
    p_update.add_argument("--requirement-ref", dest="requirement_ref", help="citation or floor:<class> (see `add`)")
    p_update.add_argument("--repro-cmd", dest="repro_cmd", help="one shell command that exercises the bug")
    p_update.add_argument("--repro-expect", dest="repro_expect", help="'exit:nonzero' or an expected output string")
    p_update.add_argument("--blocked-by", dest="blocked_by", type=int, metavar="ID", help="ticket this one waits on")
    p_update.set_defaults(func=cmd_update, needs_db=True)

    p_close = sub.add_parser("close", help="close a ticket with a reason (never deletes)")
    p_close.add_argument("id", type=int)
    p_close.add_argument("--reason", required=True, help="why it is being closed")
    p_close.add_argument(
        "--superseded-by", type=int, dest="superseded_by",
        help="id of the ticket that replaces this one",
    )
    p_close.set_defaults(func=cmd_close, needs_db=True)

    p_audit = sub.add_parser(
        "audit", help="record a post-close re-review verdict (the reopen-rate signal)")
    p_audit.add_argument("id", type=int)
    p_audit.add_argument(
        "--verdict", required=True, choices=AUDIT_VERDICTS,
        help="'holds' (the fix still holds on a fresh read) or 'reopen' (you would reopen it)",
    )
    p_audit.add_argument(
        "--note", help="one line on the verdict, especially for a 'reopen': what did not hold",
    )
    p_audit.set_defaults(func=cmd_audit, needs_db=True)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.db_path = resolve_db(args.db)
    conn = connect(args.db_path)
    try:
        if args.needs_db:
            ensure_initialized(conn, args.db_path)
        args.func(conn, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
