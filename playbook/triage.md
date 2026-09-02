# Triage (pre-staffing)

Runs on an `open` ticket before it can be staffed. Turns a raw report into a
workable ticket, or bounces it back to the human. The dispatcher triages before it
staffs; a ticket is never staffed straight from `open`.

## Steps
1. Read the ticket (`./ticket.py show $T`) and resolve its repo config
   (`config/<repo>.json`).
2. Attempt to reproduce:
   - If the ticket has a `repro`, run it in the repo's `workdir`.
   - If not, try to construct a minimal reproduction from the `symptom`.
3. **Reproduced** (the command goes **red** on the bug: it fails now and would pass once fixed):
   - **Red-capable, not merely present.** The signal must fire *because the code is
     broken*, not on healthy output. The test: *would applying the fix flip this repro
     from red to green?* A `repro_expect` substring that also appears in the tool's
     normal, passing output is not a reproduction, it is health matched by a string
     (debugger #15: `fetch-tracker-text.py --url {section-root}` printing `DUE` is the
     tool working correctly, so `repro_expect: DUE` fires when nothing is broken). If
     the repro passes on the current, unfixed tree, it is not red-capable.
   - Enrich the ticket:
     `./ticket.py update $T --repro "<command>" --module "<area>"`
     (add `--severity` if the real impact differs from what was filed).
   - `./ticket.py stage $T triaged`. It is now eligible for the loop.
4. **Not reproduced, not red-capable, or a prose fix**:
   - Leave the ticket `open`. Do NOT stage it.
   - **No code test seam means it is a prose fix, not a debugger bug (debugger #13, #15).**
     If the fix is spec wording, run-discipline, or documentation, with nothing that goes
     red→green in code, it does not belong in this queue: close it (`--reason` naming the
     real fix) and bounce it to the human to route as a documentation edit (a spec, a
     playbook, a convention). Never staff a ticket whose fix touches no code.
   - Surface a specific question to the human in the run report / chat: what you
     tried, what you saw, and exactly what you need to proceed. Never burn a
     debugging run on a ticket you cannot make fail on demand, the red-capable
     rule (see `per-ticket-loop.md`) starts here, at triage.

## Severity ordering
The dispatcher staffs the highest-severity `triaged` ticket first (the queue's
own `list` order). Triage's severity re-assessment is therefore what actually
decides what gets worked next, so re-grade honestly rather than accepting the
reporter's guess.
