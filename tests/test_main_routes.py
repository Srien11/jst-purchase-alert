import unittest
from pathlib import Path


class MainRouteTests(unittest.TestCase):
    def test_personal_actions_are_not_shadowed_by_toggle_route(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('@app.post("/subscribe/{token}/fetch-now"', source)
        self.assertIn('@app.post("/subscribe/{token}/schedule")', source)
        self.assertIn(
            '@app.post("/subscribe/{token}/notifications/{action}")', source
        )
        self.assertNotIn('@app.post("/subscribe/{token}/{action}")', source)

    def test_manager_route_has_server_side_role_check(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '@app.post("/subscribe/{token}/manager/fetch-now"', source
        )
        self.assertIn('if not buyer["is_manager"]:', source)
        self.assertIn("仅采购部负责人可查看团队数据", source)

    def test_manager_can_join_without_jushuitan_purchaser_record(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "{o.purchaser for o in orders if o.purchaser} | PROCUREMENT_MANAGERS",
            source,
        )


if __name__ == "__main__":
    unittest.main()
