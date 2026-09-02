#!/usr/bin/env python3
"""report: render the ticket queue as a human-readable status report.

Reads the same SQLite queue as ticket.py and prints a markdown report grouped by
status, so a scheduled run (or you, on demand) sees what is awaiting QA, what is
in progress, and what needs triage. The rendered human view of the queue.

    ./report.py            # current queue, open work grouped by status
    ./report.py --all      # also list closed tickets with their reasons
    ./report.py --snapshot # write the glance snapshot file instead of printing

The snapshot is a markdown file meant to be embedded in a dashboard (a notes app, a
wiki page, a status board). Its path and header are configurable through a local,
untracked override file (config/snapshot.local.json, see config/README.md), so the
public defaults stay neutral while a real deployment writes wherever it likes.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

import ticket  # reuse resolve_db / connect / ensure_initialized / SEVERITY_ORDER

# (status, human header), in the order a reader cares about them
SECTIONS = (
    ("fix-ready", "Awaiting your QA (fix-ready)"),
    ("qa", "In QA"),
    ("staffed", "In progress (staffed)"),
    ("triaged", "Triaged, queued to staff"),
    ("open", "Needs triage (open)"),
)

# Where the snapshot lands when nothing overrides it: beside the dispatch state, inside
# the gitignored run/ folder, so a fresh clone never writes outside itself.
DEFAULT_SNAPSHOT = str(Path(__file__).resolve().parent / "run" / "debugger-status.md")

# Local override for the snapshot path and header. Untracked (config/*.json is
# gitignored except example.json), so a deployment points the snapshot at its own
# dashboard file and prepends whatever frontmatter that dashboard needs, while the
# defaults above stay neutral. Location overridable for tests via DEBUGGER_SNAPSHOT_CONFIG.
#
#   {"path": "~/notes/dashboard/debugger-status.md",
#    "header": ["---", "type: status", "---", "", "**Summary**: ..."]}
#
# "header" may be a string or a list of lines. Either key may be omitted.
DEFAULT_SNAPSHOT_CONFIG = str(Path(__file__).resolve().parent / "config" / "snapshot.local.json")

# Snapshot section labels, phrased for a glance rather than the CLI reader.
# `fix-ready` is the state where a fix branch waits on your QA, so it reads "blocked on you".
SNAPSHOT_SECTIONS = (
    ("fix-ready", "Blocked on you (awaiting QA)"),
    ("qa", "In QA"),
    ("staffed", "In progress"),
    ("triaged", "Queued to staff"),
    ("open", "Needs triage"),
)

# Written at the top of every snapshot. The local override replaces it wholesale (a
# dashboard usually needs its own frontmatter and links there), so keep the default plain.
SNAPSHOT_HEADER = """\
**Summary**: Machine-generated snapshot of the debugger's SQLite ticket queue, overwritten by `report.py` on each run. Do not edit by hand."""


def _local_override() -> dict:
    """Read config/snapshot.local.json (or $DEBUGGER_SNAPSHOT_CONFIG) if present.

    Absent, unreadable, or malformed all mean "no override": the snapshot is a
    best-effort side effect and must never fail a ticket mutation over a config file."""
    cfg = Path(os.environ.get("DEBUGGER_SNAPSHOT_CONFIG") or DEFAULT_SNAPSHOT_CONFIG).expanduser()
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def snapshot_path() -> str:
    """Where `--snapshot` (no PATH) and the write-through land: $DEBUGGER_SNAPSHOT, else the
    local override's "path", else DEFAULT_SNAPSHOT."""
    env = os.environ.get("DEBUGGER_SNAPSHOT")
    if env:
        return env
    path = _local_override().get("path")
    return path if isinstance(path, str) and path else DEFAULT_SNAPSHOT


def snapshot_header() -> str:
    """The snapshot's leading block: the local override's "header" (string or list of
    lines), else SNAPSHOT_HEADER."""
    header = _local_override().get("header")
    if isinstance(header, list):
        return "\n".join(str(line) for line in header)
    return str(header) if header else SNAPSHOT_HEADER

# How many recently-closed tickets the snapshot lists. A rolling "latest N by id"
# window rather than a per-run delta: the schema has no timestamps (kept out by
# design), and a delta flashes a closure on one snapshot then drops it, so on a
# glance dashboard a rolling window is what stays useful and visible.
RECENT_CLOSED_LIMIT = 3

# The snapshot is a glance surface embedded in a dashboard, so a line is clipped to
# one scannable sentence; the full text is one `ticket.py show <id>` away. Lowered
# from 8 recently-closed to 3 and clipping added after six closures in one evening
# turned the block into an unreadable wall (a close reason can be a full paragraph,
# and an open ticket's symptom usually is).
SNAPSHOT_LINE_CHARS = 160


def _clip(text: str, limit: int = SNAPSHOT_LINE_CHARS) -> str:
    """Clip to `limit` chars on a word boundary, marking that text was dropped."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,.;:-")
    return f"{cut}..."

# Every Unicode dash that could arrive in a log/email-sourced symptom. The snapshot
# is written into someone's notes, and many note-taking setups lint against long
# dashes, so the generated file is kept dash-free by construction.
_DASH_RE = re.compile("[‒–—―]")


def _no_emdash(text: str) -> str:
    """Keep the generated snapshot free of em/en/figure/bar dashes."""
    return _DASH_RE.sub("-", text)


def _by_status(conn, status):
    # Ready work before blocked work, then critical-first (debugger-ticket-dependencies):
    # is_blocked also drives the "(blocked by #N)" marker in _line.
    return conn.execute(
        f"SELECT *, ({ticket.BLOCKED_EXPR}) AS is_blocked FROM tickets WHERE status = ? "
        f"ORDER BY is_blocked, {ticket.SEVERITY_ORDER}, id",
        (status,),
    ).fetchall()


def _line(row):
    where = f'{row["repo"]}/{row["module"]}' if row["module"] else row["repo"]
    keys = row.keys()
    blocked = ("is_blocked" not in keys or row["is_blocked"]) and (
        "blocked_by" in keys and row["blocked_by"]
    )
    suffix = f' (blocked by #{row["blocked_by"]})' if blocked else ""
    return f'- #{row["id"]} [{row["severity"]}] {where} — {row["symptom"]}{suffix}'


def _superseded_suffix(row) -> str:
    return f' (superseded by #{row["superseded_by"]})' if row["superseded_by"] else ""


# Dispatch freshness. Reporting and working the queue run on separate schedules, so
# report.py keeps the snapshot's data fresh even while nobody is fixing anything, and
# a frozen queue and a worked one would look identical: "confidently wrong". Every
# work transition (dispatch tick, stage, close, reproduced triage) stamps this marker;
# report.py reads it and states, in the block itself, when the queue was last actually
# worked. Costs one timestamp file, no new mechanism.
DISPATCH_STALE_HOURS = 48


def dispatch_marker_path() -> Path:
    """Where dispatch.sh records its last run. Mirrors dispatch.sh's own RUNDIR
    resolution (DEBUGGER_RUNDIR, else ./run beside this script) so both agree."""
    rundir = os.environ.get("DEBUGGER_RUNDIR")
    base = Path(rundir).expanduser() if rundir else Path(__file__).resolve().parent / "run"
    return base / "last-dispatch"


def dispatch_status_line(marker: Path, now: datetime) -> str:
    """One glance line stating when the queue was last worked, flagging a paused
    dispatcher. `now` is passed in so the staleness math is deterministic in tests."""
    if not marker.exists():
        return "_Dispatch: not yet run. Queue is not worked until the Debugger routine runs._"
    try:
        stamp = marker.read_text().strip()
        ran = datetime.strptime(stamp, "%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "_Dispatch: last-run time unreadable._"
    hours = (now - ran).total_seconds() / 3600
    if hours > DISPATCH_STALE_HOURS:
        days = max(1, int(hours // 24))
        return f"_Dispatch paused: last ran {stamp} ({days}d ago)._"
    return f"_Dispatch last ran {stamp}._"


def reopen_stats(conn) -> tuple[int, int]:
    """(audited, would_reopen) over closed tickets carrying an audit verdict.

    The reopen-rate signal (loop-orchestrator, reopen-rate-audit): of the closed fixes
    a fresh read re-reviewed, how many it would reopen. A closed ticket counts as
    audited once its audit_verdict is non-NULL; audit_verdict='reopen' is a would-reopen.
    Un-audited closes are simply not yet measured, so they are excluded from both counts
    rather than assumed good."""
    row = conn.execute(
        "SELECT COUNT(*) AS audited, "
        "COALESCE(SUM(CASE WHEN audit_verdict = 'reopen' THEN 1 ELSE 0 END), 0) AS reopen "
        "FROM tickets WHERE status = 'closed' AND audit_verdict IS NOT NULL"
    ).fetchone()
    return row["audited"], row["reopen"]


def reopen_line(conn) -> str:
    """One glance line stating the reopen rate, or that no audit has run yet.

    Shared by the CLI report and the snapshot: the count is the self-observability
    signal (how many closed fixes a re-read would reopen), so it belongs on both the
    terminal view and the dashboard block. Dash-free by construction."""
    audited, reopen = reopen_stats(conn)
    if audited == 0:
        return "_Reopen rate: no closed fixes audited yet._"
    pct = round(100 * reopen / audited)
    return f"_Reopen rate: {reopen}/{audited} audited would reopen ({pct}%)._"


def _section_block(conn, sections, line_fn) -> list[str]:
    """Render each non-empty status section as a '## header' plus its formatted rows.

    Shared by the CLI report and the snapshot; they differ only in the
    section table and the per-row formatter (the snapshot strips long dashes).
    """
    out: list[str] = []
    for status, header in sections:
        rows = _by_status(conn, status)
        if not rows:
            continue
        out.append(f"## {header}")
        out += [line_fn(r) for r in rows]
        out.append("")
    return out


def render(conn, show_closed: bool = False) -> str:
    open_count = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE status != 'closed'"
    ).fetchone()[0]
    closed_count = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE status = 'closed'"
    ).fetchone()[0]

    lines = ["# debugger queue report", "",
             f"_{open_count} open · {closed_count} closed_", reopen_line(conn), ""]
    lines += _section_block(conn, SECTIONS, _line)

    if show_closed:
        rows = _by_status(conn, "closed")
        if rows:
            lines.append(f"## Closed ({len(rows)})")
            for r in rows:
                audit = f' [audit: {r["audit_verdict"]}]' if r["audit_verdict"] else ""
                lines.append(f'- #{r["id"]} {r["repo"]} — {r["closed_reason"] or "?"}{_superseded_suffix(r)}{audit}')
            lines.append("")

    if open_count == 0 and not show_closed:
        lines.append("_queue clear_")
    return "\n".join(lines).rstrip() + "\n"


def render_snapshot(conn, now: str, dispatch_line: str | None = None,
                    header: str | None = None) -> str:
    """Render the snapshot markdown a dashboard embeds.

    `now` stamps when this snapshot's data was rendered; the optional dispatch_line
    states when the queue was last actually worked, which can differ since reporting
    and working run on separate schedules. `header` defaults to snapshot_header().
    """
    open_count = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE status != 'closed'"
    ).fetchone()[0]
    closed_count = conn.execute(
        "SELECT COUNT(*) FROM tickets WHERE status = 'closed'"
    ).fetchone()[0]

    lines = [
        snapshot_header() if header is None else header,
        "",
        f"_Snapshot {now} · {open_count} open · {closed_count} closed_",
    ]
    if dispatch_line:
        lines.append(dispatch_line)
    # _no_emdash even though reopen_line is dash-free today: keep the snapshot's dash-free
    # guarantee structural, not resting on that one line staying dash-free (the CLI render
    # keeps it raw, where dashes are fine).
    lines.append(_no_emdash(reopen_line(conn)))
    lines.append("")
    lines += _section_block(conn, SNAPSHOT_SECTIONS, lambda r: _clip(_no_emdash(_line(r))))

    # "Recently closed" orders by id DESC as a proxy for closure recency: it holds
    # while tickets close roughly monotonically (there is no closed-at timestamp,
    # kept out of the schema by design). A reopened-then-reclosed ticket would sort
    # by its original id, not by when it last closed.
    recent = conn.execute(
        "SELECT * FROM tickets WHERE status = 'closed' ORDER BY id DESC LIMIT ?",
        (RECENT_CLOSED_LIMIT,),
    ).fetchall()
    if recent:
        lines.append(f"## Recently closed (latest {len(recent)})")
        for r in recent:
            lines.append(_clip(_no_emdash(f'- #{r["id"]} {r["repo"]}: {r["closed_reason"] or "?"}{_superseded_suffix(r)}')))
        lines.append("")

    if open_count == 0 and not recent:
        lines += ["_Queue clear._", ""]

    return "\n".join(lines).rstrip() + "\n"


def write_snapshot(conn, path: str, now: str) -> Path:
    """Render and atomically write the snapshot (unique tmp in the target dir)."""
    target = Path(path).expanduser()
    dispatch_line = dispatch_status_line(dispatch_marker_path(), datetime.now())
    md = render_snapshot(conn, now, dispatch_line)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".debugger-status-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(md)
        os.replace(tmp, target)  # atomic on the same filesystem
    except BaseException:
        try:
            os.unlink(tmp)  # no torn .tmp left in the target folder
        except OSError:
            pass
        raise
    return target


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="report", description="Render the ticket queue as a status report."
    )
    parser.add_argument("--db", help="path to the ticket db (default: ./tickets.db or $DEBUGGER_DB)")
    parser.add_argument("--all", action="store_true", help="also list closed tickets")
    parser.add_argument(
        "--snapshot", nargs="?", const="", default=None, metavar="PATH",
        help="write the dashboard snapshot to PATH instead of printing (default: $DEBUGGER_SNAPSHOT, "
             "else config/snapshot.local.json's path, else run/debugger-status.md)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    path = ticket.resolve_db(args.db)
    conn = ticket.connect(path)
    try:
        ticket.ensure_initialized(conn, path)
        if args.snapshot is not None:
            out = write_snapshot(conn, args.snapshot or snapshot_path(),
                                 datetime.now().strftime("%Y-%m-%d %H:%M"))
            print(f"snapshot written: {out}")
        else:
            print(render(conn, show_closed=args.all), end="")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
