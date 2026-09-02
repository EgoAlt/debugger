---
name: debugger
description: Work the debugger's ticket queue in one pass. Reconcile merged fixes, triage every open ticket, fix each ready one on its own branch in an isolated worktree, refresh the snapshot, then hand the fix branches to your reviewer. Triggers /debugger, "work the bug queue", "run the debugger", "clear the debugger queue".
disable-model-invocation: true
---

# Debugger: work the queue in one pass

Work the whole ticket queue on demand: reconcile, triage, fix each ready ticket in isolation, report. The run is synchronous in this session so that one session sees every ticket, which is what lets a single reviewer look at all the fix branches at once afterwards. Fixers are per-ticket (each bug has its own root cause); review is per-batch (one pass over every branch).

Run everything from the debugger repo (the folder holding `ticket.py`):

```bash
cd /path/to/debugger
```

## 1. Reconcile, then triage

**Reconcile first: close what already landed.** For each `fix-ready` ticket (`python3 ticket.py list --status fix-ready`), resolve its repo's `workdir` and `default_branch` from `config/<repo>.json` and check whether its work branch `<branch_prefix><id>` is merged (`git -C <workdir> branch --merged <default_branch>`). If it is, `python3 ticket.py close <id> --reason "merged into <default_branch>"`. A merged fix must never linger as open work.

**Then triage.** For each `open` ticket (`python3 ticket.py list --status open`), run `python3 ticket.py triage <id>`. It reproduces a ticket carrying `repro_cmd` and `repro_expect` and stages it `triaged`, or leaves it `open` and reports that a reproduction must be built. Follow `playbook/triage.md` for anything left open: an unreproducible ticket is a question for the human, not a debugging session. Done when every merged fix-ready ticket is closed and every open ticket is `triaged` or surfaced.

## 2. Fix each ready ticket, isolated

For each ready ticket (`python3 ticket.py list --status triaged --ready`, severity-ordered), in order, run the per-ticket loop (`playbook/per-ticket-loop.md`) to a committed branch. Each ticket is fixed by its own fixer child in an isolated `git worktree` (`run-ticket.sh` creates and removes it), so a fix never touches the target's live tree. Invariant: no fix branch without a **red-capable test** that failed before the fix and passes after. Do not review here; the ticket stays `staffed` with its branch. Done when every ready ticket has a committed fix branch or is escalated (no test seam) and surfaced.

## 3. Refresh the snapshot

```bash
python3 report.py --snapshot
```

Every `stage`, `close` and reproduced `triage` already writes the snapshot through, so this is a belt-and-braces refresh at the end of the run.

## 4. Report, then hand off to review

One message: what triaged, which tickets have a committed branch, what escalated or needs the human, and the exact `python3 ticket.py close <id> --reason "merged into <default_branch>"` to run for each branch once it merges.

Then the visible seam:

> **Review these branches with your own reviewer, then merge.**
>
> This repo deliberately ships no reviewer. Point your code-review agent, a colleague, or your PR flow at each `<branch_prefix><id>` branch, scoped per ticket, never blended. Route findings back to the branch's fixer child (context intact) for up to 3 rounds. When a branch is clean, `python3 ticket.py stage <id> fix-ready`. A branch still dirty after 3 rounds stays `staffed` and is escalated, never faked `fix-ready`.

## QA gate, never crossed autonomously

Every fix ends at a branch for a human's QA. This skill never merges and never pushes a default branch. A branch touching the tooling that protects the target repo (its hooks, guards, CI config) is always handed to a human explicitly.

## Cost discipline

Never let triage grow into an open-ended investigation. When the queue has nothing ready, report quiet and stop; do not manufacture work.

## Keeping this skill current

Thin wrapper: the procedure lives in the repo (`playbook/per-ticket-loop.md`, `playbook/triage.md`, `ticket.py`, `run-ticket.sh`). Fix triage or loop behavior there; fix orchestration or reporting here.
