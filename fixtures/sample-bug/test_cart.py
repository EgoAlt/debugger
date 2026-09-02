import unittest

from cart import apply_discount


class CartTests(unittest.TestCase):
    def test_zero_discount_is_a_noop(self):
        # Passes even WITH the bug (100 - 100*0 == 100). Deliberately too weak to
        # catch the real defect: the loop's debugger must add the failing-first
        # test, exactly the "green suite that shares the bug's assumption" trap.
        self.assertEqual(apply_discount(100, 0), 100)


if __name__ == "__main__":
    unittest.main()
