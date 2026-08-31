import unittest

from calculator import divide


class CalculatorTests(unittest.TestCase):
    def test_divide_returns_amount_per_item(self) -> None:
        self.assertEqual(divide(12, 3), 4)


if __name__ == "__main__":
    unittest.main()
