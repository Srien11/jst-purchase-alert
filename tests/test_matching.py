import unittest

from app.matching import normalize_person_name, unique_purchaser_match


class MatchingTests(unittest.TestCase):
    def test_removes_tongxiang_suffix(self):
        self.assertEqual(normalize_person_name(" 张三桐乡 "), "张三")

    def test_unique_suffix_match(self):
        self.assertEqual(
            unique_purchaser_match("张三", {"张三桐乡", "李四桐乡"}),
            "张三桐乡",
        )

    def test_duplicate_normalized_names_require_review(self):
        self.assertIsNone(
            unique_purchaser_match("张三", {"张三", "张三桐乡"})
        )

    def test_non_match_requires_review(self):
        self.assertIsNone(unique_purchaser_match("王五", {"张三桐乡"}))


if __name__ == "__main__":
    unittest.main()
