import unittest

from app.core.admin_auth import admin_token_matches


class AdminAuthTests(unittest.TestCase):
    def test_rejects_missing_or_short_configured_token(self):
        self.assertFalse(admin_token_matches("", "anything"))
        self.assertFalse(admin_token_matches("short", "short"))

    def test_requires_exact_strong_token(self):
        configured = "0123456789abcdef0123456789abcdef"
        self.assertTrue(admin_token_matches(configured, configured))
        self.assertFalse(admin_token_matches(configured, configured + "x"))
        self.assertFalse(admin_token_matches(configured, None))


if __name__ == "__main__":
    unittest.main()
