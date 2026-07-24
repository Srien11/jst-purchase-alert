import unittest
import sys
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

sys.modules.setdefault("httpx", SimpleNamespace(AsyncClient=object))
sys.modules.setdefault("app.config", SimpleNamespace(settings=SimpleNamespace()))
from unittest.mock import AsyncMock, patch
from app.jushuitan import _flatten, _purchase_rows, _sign
from app.jushuitan import settings as jst_settings


class JushuitanMappingTests(unittest.TestCase):
    def test_official_signature_vector(self):
        params = {
            "app_key": "5b53060f23d84ddf9703056e84fa5a2d",
            "access_token": "d7b01bf0842a4742a9450e21ffd95f60",
            "timestamp": 1639128407,
            "version": 2,
            "charset": "utf-8",
            "biz": '{"page_index":"1","page_size":"100","nicks":["老板"]}',
        }
        self.assertEqual(
            _sign(params, "e9c5ca33fecb404b8e6cdbd0ef4a6d25"),
            "395f5a78b446be465ac03a02491296c7",
        )

    def test_purchase_and_inbound_quantities_are_joined(self):
        purchases = [{
            "po_id": 113622,
            "purchaser_name": "肺棘",
            "seller": "供应商A",
            "items": [{
                "sku_id": "1234560",
                "i_id": "102025",
                "name": "羊毛双面围巾",
                "qty": 10,
                "delivery_date": "2026-08-10T00:00:00",
            }],
        }]
        received = {
            ("113622", "1234560", "102025"): Decimal("4")
        }
        rows = _flatten(purchases, received)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].delivery_date, date(2026, 8, 10))
        self.assertEqual(rows[0].ordered_qty, Decimal("10"))
        self.assertEqual(rows[0].received_qty, Decimal("4"))
        self.assertEqual(rows[0].pending_qty, Decimal("6"))

    def test_purchase_item_without_delivery_date_is_skipped(self):
        purchases = [{
            "po_id": 113623,
            "purchaser_name": "采购员",
            "seller": "供应商B",
            "items": [{"sku_id": "MISSING-DATE", "qty": 10}],
        }]
        self.assertEqual(_flatten(purchases, {}), [])

    def test_purchaser_whitespace_is_normalized(self):
        purchases = [{
            "po_id": 113624,
            "purchaser_name": "张小薇&花卷\n 桐乡",
            "seller": "供应商C",
            "items": [{
                "sku_id": "SKU-1",
                "qty": 1,
                "delivery_date": "2026-08-10T00:00:00",
            }],
        }]
        self.assertEqual(
            _flatten(purchases, {})[0].purchaser, "张小薇&花卷 桐乡"
        )

    async def _windowed_rows(self):
        responses = [
            {"datas": [{"po_id": 1, "modified": "old"}], "has_next": False},
            {
                "datas": [
                    {"po_id": 1, "modified": "new"},
                    {"po_id": 2, "modified": "new"},
                ],
                "has_next": False,
            },
        ]
        jst_settings.jst_purchase_api_url = "https://example.test/purchase"
        jst_settings.jst_purchase_lookback_days = 8
        jst_settings.jst_request_interval_seconds = 0
        with patch("app.jushuitan._post", new=AsyncMock(side_effect=responses)):
            rows = await _purchase_rows(object())
        return rows

    def test_purchase_query_uses_multiple_windows_and_deduplicates(self):
        import asyncio

        rows = asyncio.run(self._windowed_rows())
        self.assertEqual({row["po_id"] for row in rows}, {1, 2})
        self.assertEqual(next(row for row in rows if row["po_id"] == 1)["modified"], "new")


if __name__ == "__main__":
    unittest.main()
