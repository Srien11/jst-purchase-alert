import unittest
from datetime import date
from decimal import Decimal
from app.logic import build_alerts, due_warning, summary
from app.models import PurchaseOrder
from app.service import build_report_card, render_report


class LogicTests(unittest.TestCase):
    def order(self, no, days, received=0, purchaser="小王"):
        return PurchaseOrder(
            no, purchaser, "供应商A", date(2026, 7, 24).fromordinal(
                date(2026, 7, 24).toordinal() + days
            ), Decimal("100"), Decimal(str(received))
        )

    def test_transport_buffer_and_levels(self):
        rows = build_alerts(
            [self.order("A", 15), self.order("B", 10), self.order("C", 6)],
            date(2026, 7, 24), "小王", 3
        )
        by_no = {r.order.order_no: r for r in rows}
        self.assertEqual(by_no["A"].effective_days_left, 12)
        self.assertEqual(by_no["B"].level, "黄")
        self.assertEqual(by_no["C"].level, "红")

    def test_warning_days(self):
        rows = build_alerts(
            [self.order("A", 15), self.order("B", 10), self.order("C", 6)],
            date(2026, 7, 24), "小王", 3
        )
        self.assertEqual(
            {r.effective_days_left for r in due_warning(rows, (12, 7, 3))},
            {12, 7, 3},
        )

    def test_received_summary(self):
        rows = build_alerts(
            [self.order("A", 15, 100), self.order("B", 10, 30)],
            date(2026, 7, 24), "小王", 3
        )
        stat = summary(rows)
        self.assertEqual(stat["fully_received_orders"], 1)
        self.assertEqual(stat["pending_qty"], Decimal("70"))

    def test_report_only_contains_rows_passed_to_it(self):
        rows = build_alerts(
            [
                self.order("DUE", 15),
                self.order("NOT-DUE", 14),
                self.order("RECEIVED", 15, 100),
            ],
            date(2026, 7, 24), "小王", 3
        )
        report = render_report("小王", due_warning(rows, (12, 7, 3)), "https://example.test")
        self.assertIn("DUE", report)
        self.assertNotIn("NOT-DUE", report)
        self.assertNotIn("RECEIVED", report)
        self.assertIn("【本次在途汇总】", report)
        self.assertIn("【在途明细表】", report)

    def test_card_contains_summary_and_real_table(self):
        rows = build_alerts(
            [self.order("DUE", 15), self.order("NOT-DUE", 14)],
            date(2026, 7, 24), "小王", 3
        )
        card = build_report_card(
            "小王", due_warning(rows, (12, 7, 3)), "https://example.test"
        )
        self.assertEqual(card["header"]["title"]["content"], "采购交期预警｜小王")
        table = next(e for e in card["elements"] if e["tag"] == "table")
        self.assertEqual(table["rows"][0]["order_no"], "DUE")
        self.assertEqual(len(table["rows"]), 1)
        self.assertEqual(table["page_size"], 10)


if __name__ == "__main__":
    unittest.main()
