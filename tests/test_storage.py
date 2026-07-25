import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path
from app.models import PurchaseOrder
from app.storage import (
    close_alert,
    closed_order_numbers,
    connect,
    active_buyers,
    buyer_by_open_id,
    buyer_by_token,
    cached_orders,
    cached_purchasers,
    claim_system_event,
    finish_system_event,
    reopen_alert,
    replace_order_cache,
    merge_order_cache,
    mark_schedule_slot,
    set_buyer_enabled,
    system_event,
    update_buyer_schedule,
    upsert_buyer,
)


class StorageTests(unittest.TestCase):
    def order(self, order_no, purchaser, ordered, received):
        return PurchaseOrder(
            order_no=order_no,
            purchaser=purchaser,
            supplier="供应商",
            delivery_date=date(2026, 8, 1),
            ordered_qty=Decimal(ordered),
            received_qty=Decimal(received),
            sku=f"SKU-{order_no}",
            item_name=f"商品-{order_no}",
        )

    def test_close_and_reopen_isolated_by_buyer(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(str(Path(tmp) / "test.db"))
            try:
                upsert_buyer(db, "采购员甲", "ou_a")
                upsert_buyer(db, "采购员乙", "ou_b")
                close_alert(db, "PO-001", "采购员甲")
                self.assertEqual(closed_order_numbers(db, "采购员甲"), {"PO-001"})
                self.assertEqual(closed_order_numbers(db, "采购员乙"), set())
                reopen_alert(db, "PO-001", "采购员甲")
                self.assertEqual(closed_order_numbers(db, "采购员甲"), set())
            finally:
                db.close()

    def test_global_notification_switch_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(str(Path(tmp) / "switch.db"))
            try:
                token = upsert_buyer(db, "采购员甲", "ou_a")
                self.assertEqual(len(active_buyers(db)), 0)
                set_buyer_enabled(db, token, True)
                self.assertEqual(len(active_buyers(db)), 1)
                set_buyer_enabled(db, token, False)
                self.assertEqual(len(active_buyers(db)), 0)
                self.assertEqual(buyer_by_token(db, token)["enabled"], 0)
            finally:
                db.close()

    def test_authorized_buyer_can_be_found_by_feishu_open_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(str(Path(tmp) / "authorization.db"))
            try:
                token = upsert_buyer(db, "采购员甲", "ou_authorized")
                buyer = buyer_by_open_id(db, "ou_authorized")
                self.assertEqual(buyer["token"], token)
                self.assertEqual(buyer["purchaser"], "采购员甲")
                self.assertIsNone(buyer_by_open_id(db, "ou_unknown"))
            finally:
                db.close()

    def test_order_cache_full_replace_and_incremental_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(str(Path(tmp) / "cache.db"))
            try:
                first = self.order("PO-1", "采购员甲", "10", "2")
                replace_order_cache(db, [first])
                self.assertEqual(cached_purchasers(db), ["采购员甲"])
                self.assertEqual(cached_orders(db)[0].received_qty, Decimal("2"))
                changed = self.order("PO-1", "采购员甲", "10", "6")
                second = self.order("PO-2", "采购员乙", "20", "0")
                merge_order_cache(db, [changed, second])
                orders = {order.order_no: order for order in cached_orders(db)}
                self.assertEqual(orders["PO-1"].received_qty, Decimal("6"))
                self.assertEqual(cached_purchasers(db), ["采购员乙", "采购员甲"])
                replace_order_cache(db, [second])
                self.assertEqual(
                    [order.order_no for order in cached_orders(db)], ["PO-2"]
                )
            finally:
                db.close()

    def test_manager_role_is_persisted_and_cannot_be_downgraded(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(str(Path(tmp) / "manager.db"))
            try:
                token = upsert_buyer(
                    db, "吴子杰&茴香", "ou_manager", is_manager=True
                )
                self.assertEqual(buyer_by_token(db, token)["is_manager"], 1)
                upsert_buyer(db, "吴子杰&茴香", "ou_manager_new")
                buyer = buyer_by_token(db, token)
                self.assertEqual(buyer["is_manager"], 1)
                self.assertEqual(buyer["feishu_open_id"], "ou_manager_new")
            finally:
                db.close()

    def test_one_time_system_event_cannot_be_claimed_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(str(Path(tmp) / "events.db"))
            try:
                self.assertTrue(claim_system_event(db, "test-once"))
                self.assertFalse(claim_system_event(db, "test-once"))
                finish_system_event(db, "test-once", "成功 1/1")
                event = system_event(db, "test-once")
                self.assertEqual(event["status"], "completed")
                self.assertEqual(event["detail"], "成功 1/1")
            finally:
                db.close()

    def test_buyer_schedule_defaults_and_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = connect(str(Path(tmp) / "schedule.db"))
            try:
                token = upsert_buyer(db, "采购员甲", "ou_a")
                buyer = buyer_by_token(db, token)
                self.assertEqual(buyer["schedule_frequency"], "daily")
                self.assertEqual((buyer["schedule_hour"], buyer["schedule_minute"]), (9, 0))
                update_buyer_schedule(
                    db, token, "weekly", 14, 30, 4, "采购员乙"
                )
                buyer = buyer_by_token(db, token)
                self.assertEqual(
                    (
                        buyer["schedule_frequency"],
                        buyer["schedule_hour"],
                        buyer["schedule_minute"],
                        buyer["schedule_weekday"],
                        buyer["schedule_purchaser"],
                    ),
                    ("weekly", 14, 30, 4, "采购员乙"),
                )
                mark_schedule_slot(db, token, "2026-07-24T14:30")
                self.assertEqual(
                    buyer_by_token(db, token)["last_schedule_slot"],
                    "2026-07-24T14:30",
                )
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
