import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
NEW_HOST = "purchase-alert.kktree.cn"
OLD_HOST = "jushuitan.skills.kktree.cn"


class DomainConfigTests(unittest.TestCase):
    def test_compose_routes_only_the_new_host_with_existing_path_prefix(self):
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn(f"Host(`{NEW_HOST}`)", compose)
        self.assertIn("PathPrefix(`/purchase-alert`)", compose)
        self.assertNotIn(OLD_HOST, compose)

    def test_readme_documents_the_new_entry_and_oauth_callback(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")

        self.assertIn(
            f"https://{NEW_HOST}/purchase-alert/join",
            readme,
        )
        self.assertIn(
            f"https://{NEW_HOST}/purchase-alert/join/callback",
            readme,
        )
        self.assertNotIn(OLD_HOST, readme)


if __name__ == "__main__":
    unittest.main()
