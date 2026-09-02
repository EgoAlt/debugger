#!/usr/bin/env bash
# launch-ticket.sh <id> <pidfile>: the default launcher.
# Daemonizes the per-ticket run so it outlives the dispatch tick. Tests swap this
# out via $DEBUGGER_LAUNCHER to exercise dispatch.sh without a real claude run.
set -uo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$REPO/daemonize.py" "$2" bash "$REPO/run-ticket.sh" "$1"
