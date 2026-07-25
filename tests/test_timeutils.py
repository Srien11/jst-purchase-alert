import unittest
from datetime import timedelta

from app.timeutils import business_now


class BusinessTimeTests(unittest.TestCase):
    def test_business_time_is_explicitly_shanghai_timezone(self):
        now = business_now()
        self.assertEqual(now.utcoffset(), timedelta(hours=8))
        self.assertEqual(getattr(now.tzinfo, "key", ""), "Asia/Shanghai")


if __name__ == "__main__":
    unittest.main()
