# sample-bug fixture

A minimal, self-contained bug for exercising the per-ticket loop end to end.

**Planted bug:** `apply_discount(price, percent)` in `cart.py` never divides
`percent` by 100, so `apply_discount(100, 10)` returns `-900` instead of `90`.

**The trap (deliberate):** `test_cart.py` ships one passing test that only checks
the 0% case, so the suite is green while the bug is live, the failure shape of a
real suite where hundreds of green tests all shared the wrong assumption. A
correct loop run must **write a new failing-first test** proving the defect, then
fix `cart.py`, then leave both green.

To try the loop on it, copy this directory somewhere as a throwaway git repo, add a
`config/<name>.json` pointing at it, file a ticket, and run the loop to produce a
`fix/<id>` branch.
