# Per-ticket loop (the debugger's fix protocol)

The protocol for fixing ONE ticket to a committed fix branch. The driver skill
(`.claude/skills/debugger/SKILL.md`) runs this synchronously for each ready ticket, then hands
every branch to review in ONE batched pass: review is not per-ticket (debugger #3/#12). This
file covers fixing; the skill owns triage, the hand-off to review, and child lifecycle.

## Inputs
- A ticket id (`$T`) and its row (`./ticket.py show $T`).
- The ticket's `repo`, resolved to a working directory + test command + branch prefix via
  the per-repo config (`config/<repo>.json`, see `config/README.md`).

## Invariants (never violated)
- **Red-capable test first.** No fix branch exists without a test that FAILED on the live
  bug before the fix and PASSES after. A fix with no failing-first test is not done:
  escalate as "no correct test seam" instead of shipping it.
- **Scoped, not open-ended.** Fix the named symptom/findings only. Never weaken or delete a
  test, and never broaden scope, to silence the reviewer.
- **Isolated.** The fix happens in the isolated `git worktree` `run-ticket.sh` created, never
  in the target repo's live tree (debugger #12), so a concurrent session sharing that tree
  never sees HEAD move under it.
- **QA-before-merge.** The loop ends at a committed branch; the ticket reaches `fix-ready`
  only after review passes. Never merge, never push a default branch.

## Steps
1. `./ticket.py stage $T staffed`.
2. Spawn the **fixer child** (`Agent`, working dir = the isolated worktree `run-ticket.sh`
   created, already checked out on the work branch `<branch_prefix><T>`). Brief it with the
   ticket's `symptom`/`repro` and these rules:
   - Run the full `diagnosing-bugs` discipline (vendored in this repo at
     `.claude/skills/diagnosing-bugs/SKILL.md`, read it first), not just its red-signal
     phase. A ticket carrying `repro_cmd`/`repro_expect` already has Phase 1's tight red loop;
     a fuzzy ticket (no clear repro) builds one first (a fast, deterministic command that
     asserts the exact symptom). Then follow the phases a fixer most often skips:
     - **Minimise** the repro to the smallest scenario that still goes red before theorising.
     - **Rank 3-5 falsifiable hypotheses** before changing any code. Going straight from red
       signal to a fix anchors on the first idea, the exact failure this discipline prevents.
     - **Instrument** one variable at a time; tag every probe `[DEBUG-<T>]` so cleanup is one
       grep; prefer a debugger/REPL over logs; for a perf ticket, measure a baseline first.
   - Do NOT create a branch: the worktree is already on the work branch. Work in place here.
   - Write the failing test, run it, SHOW it red.
   - Fix the real cause (not the symptom). Run the test green, then the repo's full
     `test_command` green. Remove every `[DEBUG-<T>]` probe (one grep), then commit to the
     work branch with the confirmed root-cause hypothesis in the message.
   - Report: branch name, the test, the fix per finding. If the real cause was architectural
     (no clean test seam, tangled callers, hidden coupling), name it for the human: the
     debugger fixes the defect, it never self-invokes an architecture change.
3. The loop ends here. The ticket stays `staffed` with its committed branch, and the fixer
   child stays alive for the batched review. Do NOT review or stage `fix-ready` here: the
   skill hands ALL branches to one reviewer, routes findings back to each live fixer child
   (bounded 3 rounds per ticket), and stages `fix-ready` only when a branch is clean.
4. Escalation (no test seam): leave the ticket `staffed`, state what is blocking, and surface
   it to the human. Never fake `fix-ready`.

## Why the fixer child lives until the batched review
The skill keeps each ticket's fixer child alive from its fix through the batched review, so a
review finding routes back to the child that wrote the fix (context intact, cheaper and
sharper than a fresh spawn); all children terminate at the end of the run. The reviewer is a
single batched agent over every branch, never one per ticket: that split is the batch's whole
economy. A child carried across separate RUNS re-accumulates stale context, the exact failure
per-ticket isolation exists to avoid, so children never outlive the run.
