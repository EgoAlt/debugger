"""A tiny cart module with one planted bug, for exercising the per-ticket loop."""


def apply_discount(price, percent):
    """Return the price after a percent discount. `percent` is 0-100."""
    return price - price * percent  # planted bug: should divide percent by 100
