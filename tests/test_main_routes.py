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
        self.assertIn(
            '@app.get("/subscribe/{token}/manager/purchasers")', source
        )
        self.assertIn("purchasers = cached_purchasers(db)", source)
        self.assertIn('id="incremental-order-cache"', source)
        self.assertIn('id="full-order-cache"', source)
        self.assertIn('id="schedule-purchaser"', source)
        self.assertIn("负责人定时收到所选采购员或全团队", source)
        self.assertIn(
            'frequencySelect.value==="weekly" ? "" : "none"', source
        )

    def test_login_uses_authorized_binding_without_purchaser_selection(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("purchaser = authorized_login_identity(name)", source)
        self.assertNotIn("buyer_by_open_id(db, open_id)", source)
        self.assertIn(
            'if buyer and is_authorized_purchaser(buyer["purchaser"]):',
            source,
        )
        self.assertIn("当前飞书账号尚未授权", source)
        self.assertNotIn('name="purchaser" required', source)
        self.assertNotIn('@app.post("/join/confirm"', source)
        self.assertNotIn("if is_procurement_manager(name):", source)
        self.assertIn("authorized_login_identity", source)

    def test_bound_browser_skips_repeated_oauth(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "purchase_alert_token: str | None = Cookie(None)", source
        )
        self.assertIn('"purchase_alert_token"', source)
        self.assertIn("httponly=True", source)
        self.assertIn("secure=True", source)

    def test_scheduled_push_sends_full_current_range_not_warning_days_only(self):
        source = (Path(__file__).parents[1] / "app" / "service.py").read_text(
            encoding="utf-8"
        )
        scheduled = source[
            source.index("async def run_scheduled_checks"):
            source.index("async def send_manual_report")
        ]
        self.assertIn(
            "_current_in_transit(_active_rows(db, buyer, orders))",
            scheduled,
        )
        self.assertIn("_send_order_summaries(buyer, rows)", scheduled)
        self.assertNotIn("_run_for_buyers(personal_buyers", scheduled)
        self.assertIn("completed_buyers", scheduled)

    def test_full_web_report_and_beijing_time_controls_exist(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('@app.get("/subscribe/{token}/report"', source)
        self.assertIn("完整在途数据", source)
        self.assertIn("填入当前北京时间", source)
        self.assertIn('timeZone:"Asia/Shanghai"', source)

    def test_legacy_join_review_routes_are_removed(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("/admin/join-requests", source)
        self.assertNotIn("/admin/review/{request_id}", source)


if __name__ == "__main__":
    unittest.main()
