# debugger

A ticket queue and fix loop for an AI coding agent, so a bug can be sent away instead of hijacking the session that noticed it.

You file a bug as a ticket. The debugger reproduces it, fixes it on its own branch inside an isolated git worktree, proves the fix with a test that failed before and passes after, and hands you the branch. You review and merge. It never merges, never pushes a default branch, and never deletes a ticket.

Everything is standard-library Python and bash. There is nothing to install beyond `git`, `python3` and the `claude` CLI.

## What this repo adds

The debugging method itself is not new, and this repo does not pretend otherwise (see the next section). What it builds is the orchestration around it:

- **A ticket queue that never forgets.** One SQLite file, `tickets.db`. Tickets are closed with a reason, never deleted, so the queue is also its own audit trail. Statuses: `open`, `triaged`, `staffed`, `fix-ready`, `qa`, `closed`.
- **A three-question intake gate.** At filing time you can cite where the requirement already existed (a spec line, a docstring, the tool's stated purpose), give one shell command that exercises the bug, and name the signal that means the bug is present. The first question keeps the queue defects-only: if no prior document is violated, the item is a feature request and belongs somewhere else. The other two make triage deterministic.
- **Deterministic triage.** `ticket.py triage` runs the repro command in the target repo and only stages a ticket whose bug signal actually fires. A ticket the machine cannot make fail on demand is bounced back to you with a specific question, never staffed on faith.
- **An isolated-worktree fix loop.** Each ticket is fixed in a throwaway `git worktree` checked out on its own branch. Your live working tree never moves. If a worktree cannot be created, the loop aborts rather than degrading to the live tree.
- **Red-capable test first.** No fix branch exists without a test that failed on the live bug before the fix and passes after. When no correct test seam exists, that is reported as the finding instead of shipping a fix nobody can lock down.
- **QA before merge.** The loop ends at a committed branch. Review and merge are yours.
- **Self-observability.** A reopen-rate audit records, for closed fixes you re-read later, whether you would reopen them. A self-reviewing loop that grades its own work generously is the failure this measures.
- **A glance snapshot.** `report.py --snapshot` writes a short markdown block for whatever dashboard you look at, refreshed on every ticket change, with a line saying when the queue was last actually worked so a frozen queue never looks fresh.

## The debugging discipline is Matt Pocock's

The method the fixer follows is the `diagnosing-bugs` skill from [Matt Pocock's skills collection](https://github.com/mattpocock/skills), MIT licensed, vendored verbatim at `.claude/skills/diagnosing-bugs/` (version 1.2.3, see `THIRD_PARTY_NOTICES.md`). It is the part that makes a fixer build a tight red feedback loop before theorising, minimise the repro, rank falsifiable hypotheses before touching code, instrument one variable at a time, and clean up after itself.

It is vendored rather than referenced so a fresh clone debugs as well as the setup it came from, with nothing to install. This repo builds the queue, the gate, the isolation and the hand-off around that discipline.

## How a ticket flows

```
add ──▶ open ──triage──▶ triaged ──dispatch──▶ staffed ──fix loop──▶ (branch)
                │                                                       │
                └─ bounced: no repro, or a prose fix, not a code bug    ▼
                                            your review ──▶ fix-ready ──merge──▶ closed
```

1. **File.** `ticket.py add` with a symptom, a severity and, ideally, the three intake answers.
2. **Triage.** `ticket.py triage <id>` reproduces or bounces. `playbook/triage.md` covers the judgment the machine cannot make, such as a repro that also fires on healthy output.
3. **Fix.** The per-ticket loop (`playbook/per-ticket-loop.md`) runs a headless `claude` session inside an isolated worktree, following the vendored discipline, and commits to `<branch_prefix><id>`.
4. **Review.** Yours. Point your own reviewer at the branch, then `ticket.py stage <id> fix-ready`.
5. **Merge and close.** `ticket.py close <id> --reason "merged into main"`.
6. **Audit, later.** `ticket.py audit <id> --verdict holds|reopen` on a few closed fixes, with fresh eyes. `report.py` shows the resulting reopen rate.

## Quick start

```sh
git clone <this repo> debugger && cd debugger
python3 -m unittest                     # everything green, stdlib only

./ticket.py init
cp config/example.json config/my-app.json   # edit workdir, test_command, branches

./ticket.py add --repo my-app --severity high \
  --symptom "apply_discount(100, 10) returns -900, expected 90" \
  --requirement-ref "cart.py docstring: percent is 0-100" \
  --repro-cmd "python3 -m pytest -q tests/test_cart.py" \
  --repro-expect "exit:nonzero"

./ticket.py triage 1                    # reproduces, stages triaged
./report.py                             # what is open, grouped by status
```

Then work the queue. Two ways:

- **Interactively**, from Claude Code opened in this repo: `/debugger`. The skill at `.claude/skills/debugger/SKILL.md` reconciles merged branches, triages, fixes every ready ticket in order, refreshes the snapshot, and ends with a per-branch summary and a visible hand-off to your reviewer.
- **Unattended**, from a scheduler: `./dispatch.sh`. One tick triages the top open ticket and staffs the top ready one into a daemonized run that survives the tick ending. Concurrency is one ticket at a time.

`fixtures/sample-bug/` is a tiny planted bug with a deliberately weak green test, for trying the loop end to end on something harmless.

## Layout

| Path | What it is |
|---|---|
| `ticket.py` | the queue CLI: `init`, `add`, `list`, `show`, `triage`, `stage`, `update`, `close`, `audit` |
| `report.py` | the rendered queue report, and `--snapshot` for the dashboard block |
| `dispatch.sh` | the unattended dispatcher: staleness guard, triage, staff one ticket, launch |
| `run-ticket.sh` | the daemon body: isolated worktree plus a headless `claude` loop plus a reset-if-stuck safety net |
| `launch-ticket.sh`, `daemonize.py` | detach a per-ticket run from the tick that started it |
| `playbook/` | the protocols the agent reads: `triage.md`, `per-ticket-loop.md`, `audit.md` |
| `.claude/skills/debugger/` | the interactive driver skill |
| `.claude/skills/diagnosing-bugs/` | the vendored debugging discipline (Matt Pocock, MIT) |
| `config/` | one `<repo>.json` per target repo (local-only) plus `example.json`; see `config/README.md` |
| `fixtures/sample-bug/` | a self-contained planted bug for exercising the loop |
| `test_*.py` | the suite, `python3 -m unittest`, each test on its own temp database |

The database path defaults to `./tickets.db` beside the script; override with `--db PATH` or `DEBUGGER_DB`. Other environment knobs: `DEBUGGER_CONFIG_DIR`, `DEBUGGER_RUNDIR`, `DEBUGGER_SNAPSHOT`, `DEBUGGER_SNAPSHOT_DISABLE`, `DEBUGGER_MODEL`, and for tests `DEBUGGER_LAUNCHER` and `DEBUGGER_LOOP_CMD`.

## Where review and QA happen

Deliberately outside this repo. The public loop ends at a committed fix branch with a red-capable regression test. The driver skill says so out loud at the end of every run and tells you which branches to look at. Bring your own reviewer: a code-review agent, a pull request, a colleague. Any of them works, as long as a branch reaches `fix-ready` only after someone other than its author has read it.

One safety note. `ticket.py triage` executes `repro_cmd` with `bash -c` in the target repo. Only ever set that field from a trusted source (you, or an agent you are driving), never from untrusted content such as an email body or a scraped log line. `add` and `update` take it only from an explicit flag; keep it that way.

## Tests

```sh
python3 -m unittest
```

Stdlib `unittest`. The suite never touches a real queue, snapshot, or repo: every test runs against its own temp database, temp run directory, and a fake launcher or loop command.

## License

MIT, see `LICENSE`. The vendored `diagnosing-bugs` skill is MIT as well, copyright Matt Pocock; its notice is in `THIRD_PARTY_NOTICES.md`.
