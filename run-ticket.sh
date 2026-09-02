#!/usr/bin/env bash
# run-ticket.sh <id>: the daemon body for one ticket.
#
# Runs the per-ticket loop as a headless claude session that reads the playbook and
# spawns the debugger/reviewer children. The loop runs INSIDE an isolated git worktree
# checked out on the per-ticket work branch (debugger #12): the branch checkout and all
# commits happen in a throwaway worktree, never in the target repo's live working tree,
# so a fix never moves HEAD for a concurrent session sharing that tree. The
# worktree is removed at the end; the branch persists for human QA.
#
# Isolation is REQUIRED, not best-effort: if a worktree cannot be established (no git
# workdir, or `worktree add` fails), the loop does NOT run in the live tree, it is
# aborted and the safety net resets the ticket for a later retry. Silently degrading to
# the live tree would reintroduce the exact shared-tree hazard this ticket removes.
#
# Safety net: if the loop left the ticket `staffed` (a crash, an exit without staging, or
# an aborted run), reset it to `triaged` so the queue never wedges. Never merges, pushes.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TICKET="${DEBUGGER_TICKET:-$REPO/ticket.py}"
CONFIG_DIR="${DEBUGGER_CONFIG_DIR:-$REPO/config}"
id="$1"

log() { echo "[run-ticket] $*"; }

repo="$("$TICKET" show "$id" 2>/dev/null | awk '/^repo /{print $2; exit}')"

# Resolve the repo's workdir / default_branch / branch_prefix (config/<repo>.json). JSON
# parsing stays in Python; -c form (not `python3 -`) so it is never confused with stdin.
cfg="$CONFIG_DIR/$repo.json"
WORKDIR=""; DEFAULT_BRANCH="main"; BRANCH_PREFIX="fix/"
if [ -n "$repo" ] && [ -f "$cfg" ]; then
    IFS=$'\t' read -r WORKDIR DEFAULT_BRANCH BRANCH_PREFIX < <(python3 -c '
import json, os, sys
d = json.load(open(sys.argv[1]))
print("\t".join([
    os.path.expanduser(d.get("workdir", "")),
    d.get("default_branch", "main"),
    d.get("branch_prefix", "fix/"),
]))' "$cfg") || true
fi

branch="${BRANCH_PREFIX}${id}"
worktree=""

# Establish the isolated worktree. On any failure, leave $worktree empty: the loop below
# is then skipped entirely rather than run in the live tree.
if [ -z "$WORKDIR" ] || ! git -C "$WORKDIR" rev-parse --git-dir >/dev/null 2>&1; then
    log "ABORT: repo '$repo' has no resolvable git workdir (workdir='$WORKDIR'); not running the loop (worktree isolation required)"
else
    git -C "$WORKDIR" worktree prune >/dev/null 2>&1 || true   # clear a crashed run's stale worktree
    candidate="$(mktemp -d)/wt"
    if git -C "$WORKDIR" worktree add "$candidate" -b "$branch" "$DEFAULT_BRANCH" >/dev/null 2>&1 \
       || git -C "$WORKDIR" worktree add "$candidate" "$branch" >/dev/null 2>&1; then
        worktree="$candidate"
        log "isolated worktree at $worktree on '$branch' (off '$DEFAULT_BRANCH')"
    else
        rm -rf "$(dirname "$candidate")" 2>/dev/null || true
        log "ABORT: could not create an isolated worktree for '$repo' on '$branch'; not running the loop in the live tree"
    fi
fi

cleanup() {
    if [ -n "$worktree" ]; then
        git -C "$WORKDIR" worktree remove "$worktree" --force >/dev/null 2>&1 || true
        rm -rf "$(dirname "$worktree")" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Only run the loop when a real isolated worktree exists, so the prompt's isolation claim
# is always true. No worktree => no loop; the safety net below resets the staffed ticket.
if [ -n "$worktree" ]; then
    PROMPT="Read $REPO/playbook/per-ticket-loop.md and work ticket $id end to end, following it exactly. You are ALREADY in an isolated git worktree at $worktree, checked out on the work branch '$branch' off '$DEFAULT_BRANCH', so do NOT create another branch: make the fix on the current branch here. Use $TICKET for every queue update. On success stage the ticket fix-ready; on escalation stage it triaged and record why. Never merge, never push."
    # The loop command is swappable (DEBUGGER_LOOP_CMD) so tests can exercise the worktree
    # lifecycle without a real claude run; default is the headless session with an explicit
    # tool allowlist (never --dangerously-skip-permissions). The model is overridable
    # (DEBUGGER_MODEL) so the default can be swapped without editing this file.
    if [ -n "${DEBUGGER_LOOP_CMD:-}" ]; then
        ( cd "$worktree" && bash -c "$DEBUGGER_LOOP_CMD" ) || true
    else
        ( cd "$worktree" && claude -p "$PROMPT" --allowedTools "Task,Bash,Read,Edit,Write" --model "${DEBUGGER_MODEL:-claude-opus-4-8}" ) || true
    fi
fi

# safety net: a finished or aborted run must not leave the ticket `staffed`.
status="$("$TICKET" show "$id" 2>/dev/null | awk '/^status/{print $2}')"
if [ "$status" = "staffed" ]; then
    "$TICKET" stage "$id" triaged >/dev/null 2>&1 || true
fi
