import unittest

from app.authorization import (
    AUTHORIZED_USERS,
    authorized_login_identity,
    is_authorized_purchaser,
)


class AuthorizationTests(unittest.TestCase):
    def test_fixed_roster_contains_six_purchasers_and_two_managers(self):
        self.assertEqual(len(AUTHORIZED_USERS), 8)

    def test_dumpling_can_login_by_nickname(self):
        self.assertEqual(
            authorized_login_identity("饺子"),
            "张利兰&饺子 桐乡",
        )

    def test_unlisted_old_registration_is_not_authorized(self):
        self.assertIsNone(authorized_login_identity("旧注册用户"))
        self.assertFalse(is_authorized_purchaser("旧注册用户"))


if __name__ == "__main__":
    unittest.main()
