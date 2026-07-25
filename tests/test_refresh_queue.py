import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.service as service


class RefreshQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_conflicting_refreshes_run_in_sequence_without_dropping_either(self):
        active = 0
        max_active = 0
        calls = []

        async def fake_fetch_orders(lookback_days):
            nonlocal active, max_active
            calls.append(lookback_days)
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return []

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(
                    service.settings,
                    "database_path",
                    str(Path(directory) / "queue.db"),
                    create=True,
                ),
                patch.object(
                    service.settings,
                    "jst_purchase_lookback_days",
                    180,
                    create=True,
                ),
                patch.object(
                    service.settings,
                    "jst_incremental_lookback_days",
                    7,
                    create=True,
                ),
                patch("app.service.fetch_orders", side_effect=fake_fetch_orders),
            ):
                full, incremental = await asyncio.gather(
                    service.refresh_order_cache(full=True),
                    service.refresh_order_cache(full=False),
                )

        self.assertEqual(calls, [180, 7])
        self.assertEqual(max_active, 1)
        self.assertFalse(full["queued"])
        self.assertTrue(incremental["queued"])


if __name__ == "__main__":
    unittest.main()
