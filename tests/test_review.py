import unittest

from app.review import review_signature, valid_review_signature


class ReviewSignatureTests(unittest.TestCase):
    def test_signature_is_valid_only_for_matching_request(self):
        signature = review_signature(12, "secret")
        self.assertTrue(valid_review_signature(12, signature, "secret"))
        self.assertFalse(valid_review_signature(13, signature, "secret"))
        self.assertFalse(valid_review_signature(12, signature, "other-secret"))


if __name__ == "__main__":
    unittest.main()
