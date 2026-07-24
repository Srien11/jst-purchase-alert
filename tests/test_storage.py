import tempfile
import unittest
from pathlib import Path
from app.storage import (
    close_alert,
    closed_order_numbers,
    connect,
    active_buyers,
    buyer_by_token,
    reopen_alert,
    set_buyer_enabled,
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


if __name__ == "__main__":
    unittest.main()
