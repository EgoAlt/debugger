# Reopen-rate audit (self-observability)

The debugger is an unattended self-reviewing loop. The only real signal it is safe to
run that way is not how many tickets it closed, it is how many of those closes would
not survive a fresh read. This is the loop-orchestrator's second half (the first being
never-delete-only-close): periodically re-read the last N you closed and count how many
you would reopen. Left unmeasured, a self-reviewing agent grades its own work
generously, the failure a July-2026 study measured across 54 self-review cycles
(improvement claimed every round, results worse or flat over half the time).

This is a **measurement**, not an autonomous correctness checker. The holds-vs-reopen
call is a judgment; the `audit` verb only records it, the same split as `triage`.

## When to run
On a cadence (weekly is a reasonable default), or whenever a batch of closes has landed
and before trusting the loop to keep running unattended. Not per-ticket.

## Steps
1. List the most recently closed tickets: `./report.py --all` (the Closed section is
   ordered newest-first) or `./ticket.py list --status closed`. Pick the last N that are
   not yet audited (no `[audit: ...]` marker). N ~= the last 5-10 closes is plenty.
2. For each, re-read it with **fresh eyes**: `./ticket.py show $T`, then look at the
   actual fix branch and its test. Judge whether the fix still holds.
   - Do this **blind to the closer's reasoning** where you can (the gauntlet-loop
     blind-critic lesson): the agent that wrote the fix is very good at explaining why
     it is reasonable, which is exactly the judgment you do not want. Prefer a fresh
     reviewer pass (your own code-review agent or a colleague), or at least re-derive the
     verdict from the branch and test, not from the close reason.
   - `holds`: the test is red-capable and real, the fix addresses the root cause, and
     you would not reopen it.
   - `reopen`: the fix is wrong, incomplete, papered over a symptom, or the test does
     not actually fail on the live bug. You would reopen it.
3. Record the verdict:
   `./ticket.py audit $T --verdict <holds|reopen> --note "<one line, esp. what did not hold>"`
4. Read the signal: `./report.py` shows `Reopen rate: R/A audited would reopen (P%)`.
   A low, stable rate is the evidence the loop is safe to run unattended. A rising rate
   is the loop telling you its fixes are not sticking, look at the `reopen`-verdict
   notes for the pattern (which bug class, which repo) before running it further.

## What audit does not do
- It never reopens the ticket. A `reopen` verdict is a flag; whether to re-file or
  reopen the underlying bug is a separate operator decision, so the measurement never
  silently mutates the queue it measures.
- It never stamps the `run/last-dispatch` liveness marker: auditing is observing the
  queue, not working it, so it must not reset the "days since the queue was worked"
  clock. It does refresh the dashboard snapshot, since the rate is a glance surface.
