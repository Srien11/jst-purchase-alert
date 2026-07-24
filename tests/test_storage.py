import tempfile
import unittest
from pathlib import Path
from app.storage import (
    close_alert,
    closed_order_numbers,
    connect,
    active_buyers,
    buyer_by_token,
    claim_system_event,
    finish_system_event,
    reopen_alert,
    mark_schedule_slot,
    set_buyer_enabled,
    system_event,
    update_buyer_schedule,
    upsert_buyer,
)


class StorageTests(unittest.TestCase):
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
                update_buyer_schedule(db, token, "weekly", 14, 30, 4)
                buyer = buyer_by_token(db, token)
                self.assertEqual(
                    (
                        buyer["schedule_frequency"],
                        buyer["schedule_hour"],
                        buyer["schedule_minute"],
                        buyer["schedule_weekday"],
                    ),
                    ("weekly", 14, 30, 4),
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
