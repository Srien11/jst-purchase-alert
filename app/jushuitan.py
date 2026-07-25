"""聚水潭采购单与采购入库单适配层。

字段来自开放平台 v2 文档：
  /open/purchase/query
  /open/webapi/wmsapi/purchasein/purchaseinquery

签名规则必须与开放平台“数字签名”文档保持一致，不能凭经验猜测。
"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from itertools import islice
import asyncio
import hashlib
import json
import time
import httpx
from .config import settings
from .models import PurchaseOrder
from .timeutils import business_naive_now


ACTIVE_PURCHASE_STATUSES = ["Confirmed", "WaitDeliver", "WaitReceive"]


def _clean_name(value) -> str:
    return " ".join(str(value or "").split())


def _chunks(values, size):
    iterator = iter(values)
    while chunk := list(islice(iterator, size)):
        yield chunk


def _biz_json(body: dict) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _sign(params: dict, app_secret: str | None = None) -> str:
    secret = app_secret if app_secret is not None else settings.jst_app_secret
    if not secret:
        raise RuntimeError("尚未配置 JST_APP_SECRET")
    canonical = "".join(
        f"{key}{params[key]}"
        for key in sorted(params)
        if key != "sign" and params[key] is not None
    )
    return hashlib.md5((secret + canonical).encode("utf-8")).hexdigest()


def _public_params(body: dict) -> dict:
    params = {
        "app_key": settings.jst_app_key,
        "access_token": settings.jst_access_token,
        "timestamp": int(time.time()),
        "charset": "utf-8",
        "version": "2",
        "biz": _biz_json(body),
    }
    params["sign"] = _sign(params)
    return params


async def _post(client: httpx.AsyncClient, url: str, body: dict) -> dict:
    payload = {}
    for attempt in range(4):
        response = await client.post(url, data=_public_params(body))
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") in (0, "0"):
            return payload.get("data") or {}
        if str(payload.get("code")) != "199" or attempt == 3:
            break
        await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"聚水潭接口失败: {payload.get('code')} {payload.get('msg')}")


async def _purchase_rows(
    client: httpx.AsyncClient, lookback_days: int | None = None
) -> list[dict]:
    rows_by_id: dict[str, dict] = {}
    modified_end = business_naive_now()
    overall_begin = modified_end - timedelta(
        days=(
            settings.jst_purchase_lookback_days
            if lookback_days is None else lookback_days
        )
    )
    window_begin = overall_begin
    while window_begin < modified_end:
        window_end = min(window_begin + timedelta(days=7), modified_end)
        page = 1
        while True:
            data = await _post(
                client,
                settings.jst_purchase_api_url,
                {
                    "page_index": page,
                    "page_size": 50,
                    "modified_begin": window_begin.strftime("%Y-%m-%d %H:%M:%S"),
                    "modified_end": window_end.strftime("%Y-%m-%d %H:%M:%S"),
                    "statuss": ACTIVE_PURCHASE_STATUSES,
                },
            )
            for row in data.get("datas") or []:
                rows_by_id[str(row["po_id"])] = row
            await asyncio.sleep(settings.jst_request_interval_seconds)
            if not data.get("has_next"):
                break
            page += 1
        window_begin = window_end
    return list(rows_by_id.values())


async def _received_quantities(
    client: httpx.AsyncClient, po_ids: list[int]
) -> dict[tuple[str, str, str], Decimal]:
    totals: dict[tuple[str, str, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for po_batch in _chunks(po_ids, 30):
        page = 1
        while True:
            data = await _post(
                client,
                settings.jst_purchase_in_api_url,
                {
                    "page_index": page,
                    "page_size": 50,
                    "po_ids": po_batch,
                    "statuss": ["Confirmed"],
                    "is_get_total": True,
                },
            )
            for inbound in data.get("datas") or []:
                po_id = str(inbound.get("po_id", ""))
                for item in inbound.get("items") or []:
                    key = (po_id, str(item.get("sku_id", "")), str(item.get("i_id", "")))
                    totals[key] += Decimal(str(item.get("qty") or 0))
            if not data.get("has_next"):
                break
            page += 1
    return totals


def _delivery_date(value) -> date:
    if not value:
        raise ValueError("采购明细缺少 delivery_date")
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()


def _flatten(purchases: list[dict], received) -> list[PurchaseOrder]:
    result: list[PurchaseOrder] = []
    for purchase in purchases:
        po_id = str(purchase["po_id"])
        for item in purchase.get("items") or []:
            if not item.get("delivery_date"):
                continue
            sku_id, item_id = str(item.get("sku_id", "")), str(item.get("i_id", ""))
            key = (po_id, sku_id, item_id)
            result.append(
                PurchaseOrder(
                    order_no=po_id,
                    purchaser=_clean_name(purchase.get("purchaser_name", "")),
                    supplier=str(purchase.get("seller", "")).strip(),
                    delivery_date=_delivery_date(item.get("delivery_date")),
                    ordered_qty=Decimal(str(item.get("qty") or 0)),
                    received_qty=min(
                        Decimal(str(item.get("qty") or 0)), received.get(key, Decimal("0"))
                    ),
                    sku=sku_id or item_id,
                    item_name=str(item.get("name", "")).strip(),
                )
            )
    return result


async def fetch_orders(lookback_days: int | None = None) -> list[PurchaseOrder]:
    if settings.demo_mode:
        today = date.today()
        return [
            PurchaseOrder(
                order_no="DEMO-1201",
                purchaser="演示采购员",
                supplier="上海示例供应商",
                delivery_date=date.fromordinal(today.toordinal() + 15),
                ordered_qty=Decimal("100"),
                received_qty=Decimal("20"),
                sku="SKU-RED-01",
                item_name="红色预警演示商品",
            ),
            PurchaseOrder(
                order_no="DEMO-0701",
                purchaser="演示采购员",
                supplier="杭州示例供应商",
                delivery_date=date.fromordinal(today.toordinal() + 10),
                ordered_qty=Decimal("80"),
                received_qty=Decimal("80"),
                sku="SKU-DONE-01",
                item_name="已全部入库演示商品",
            ),
            PurchaseOrder(
                order_no="DEMO-0301",
                purchaser="演示采购员",
                supplier="广州示例供应商",
                delivery_date=date.fromordinal(today.toordinal() + 6),
                ordered_qty=Decimal("60"),
                received_qty=Decimal("10"),
                sku="SKU-URGENT-01",
                item_name="紧急跟进演示商品",
            ),
        ]
    async with httpx.AsyncClient(timeout=45) as client:
        purchases = await _purchase_rows(client, lookback_days)
        po_ids = [int(row["po_id"]) for row in purchases]
        received = await _received_quantities(client, po_ids) if po_ids else {}
    return _flatten(purchases, received)


async def fetch_purchasers() -> list[str]:
    if settings.demo_mode:
        return ["演示采购员"]
    async with httpx.AsyncClient(timeout=45) as client:
        purchases = await _purchase_rows(client)
    return sorted(
        {
            _clean_name(row.get("purchaser_name"))
            for row in purchases
            if _clean_name(row.get("purchaser_name"))
        }
    )
