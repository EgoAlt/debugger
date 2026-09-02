#!/usr/bin/env bash
# dispatch.sh: the debugger's on-demand queue dispatcher.
#
# Invoked when you want the queue advanced without a driving session (reporting is
# separate, report.py --snapshot, so a scheduler can run this as a tick). Each run,
# in order: reset any crashed run, then, concurrency-1,
# TRIAGE the top open ticket, then STAFF the top ready triaged ticket into ONE
# daemonized per-ticket run and exit. The expensive fix loop happens in the daemon,
# never here. Triaging before staffing matches playbook/triage.md (debugger #3).
#
# Concurrency 1: at most one ticket `staffed` at a time, one triage per run.
# It never merges and never pushes; the loop ends at `fix-ready` for human QA.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TICKET="${DEBUGGER_TICKET:-$REPO/ticket.py}"
LAUNCHER="${DEBUGGER_LAUNCHER:-$REPO/launch-ticket.sh}"
REPORT="${DEBUGGER_REPORT:-$REPO/report.py}"
# The snapshot's default path is owned by report.py (its DEFAULT_SNAPSHOT), the
# single source of truth. We pass a path only to override it for hermetic tests.
SNAPSHOT="${DEBUGGER_SNAPSHOT:-}"
RUNDIR="${DEBUGGER_RUNDIR:-$REPO/run}"
mkdir -p "$RUNDIR"

log() { echo "[dispatch] $*"; }

# Record when the queue was last actually worked, so report.py (which may run on a
# different schedule) can show a paused dispatcher instead of a fresh-looking-but-
# frozen queue. Best-effort.
mark_dispatch() { date '+%Y-%m-%d %H:%M' > "$RUNDIR/last-dispatch" 2>/dev/null || true; }

# Refresh the dashboard snapshot on every tick, whatever exit path we take
# (quiet tick, in-flight skip, or a launch). Best-effort: a snapshot failure must
# never wedge the dispatcher, so it is trapped and logged, never fatal. Stamp the
# dispatch marker first so the refreshed snapshot reflects this tick's run time.
refresh_snapshot() {
    mark_dispatch
    local snap_args=(--snapshot)
    [ -n "$SNAPSHOT" ] && snap_args+=("$SNAPSHOT")
    "$REPORT" "${snap_args[@]}" >/dev/null 2>&1 || log "snapshot refresh failed"
}
trap refresh_snapshot EXIT

ids_with_status() { "$TICKET" list --status "$1" 2>/dev/null | sed -n 's/^#\([0-9][0-9]*\).*/\1/p'; }
# Triaged tickets that are actually ready (no unresolved blocker), highest-severity
# first: dispatch never staffs a ticket that cannot land (debugger-ticket-dependencies).
ready_triaged_ids() { "$TICKET" list --status triaged --ready 2>/dev/null | sed -n 's/^#\([0-9][0-9]*\).*/\1/p'; }

# 1. staleness guard: a `staffed` ticket whose daemon is gone (crash) is reset,
#    so a dead run can never wedge the queue.
for id in $(ids_with_status staffed); do
    pidfile="$RUNDIR/$id.pid"
    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile" 2>/dev/null)" 2>/dev/null; then
        continue  # daemon alive: genuinely in flight
    fi
    log "ticket #$id staffed but its daemon is gone; resetting to triaged"
    "$TICKET" stage "$id" triaged >/dev/null 2>&1 || true
    rm -f "$pidfile"
done

# 2. concurrency-1: if a live run remains, do nothing further.
if [ -n "$(ids_with_status staffed)" ]; then
    log "a ticket is in flight; nothing to dispatch"
    exit 0
fi

# 2.5 triage stage (debugger #3): before staffing, deterministically reproduce-or-bounce
#     the top open ticket (concurrency-1, one per run). `ticket.py triage` auto-stages a
#     ticket carrying repro_cmd + repro_expect when its 'bug present' signal fires, and
#     leaves one lacking a repro open for manual (model/human) triage. This is what lets an
#     open ticket advance without a human running triage by hand first, and is why the
#     dispatcher "triages before it staffs" (playbook/triage.md), resolving the old
#     dispatch.sh/README vs triage.md contradiction. A just-triaged top ticket is eligible
#     for staffing in this same run below.
top_open="$(ids_with_status open | head -1)"
if [ -n "$top_open" ]; then
    log "triaging top open #$top_open"
    "$TICKET" triage "$top_open" || log "triage of #$top_open errored (left open)"
fi

# 3. pick the highest-severity READY triaged ticket (unblocked, list is severity-ordered).
next="$(ready_triaged_ids | head -1)"
if [ -z "$next" ]; then
    log "no ready triaged tickets; quiet tick"
    exit 0
fi

# 4. stage it staffed, then hand off to the launcher.
"$TICKET" stage "$next" staffed >/dev/null
log "staffing #$next; launching daemonized loop"
"$LAUNCHER" "$next" "$RUNDIR/$next.pid"
log "#$next dispatched"
