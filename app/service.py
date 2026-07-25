import asyncio
from datetime import date, datetime
from .config import settings
from .feishu import send_card, send_message
from .jushuitan import fetch_orders
from .logic import MAX_EFFECTIVE_DAYS, MIN_EFFECTIVE_DAYS, build_alerts, due_warning, summary
from .storage import (
    active_buyers,
    buyer_by_token,
    cached_orders,
    closed_order_numbers,
    connect,
    merge_order_cache,
    mark_schedule_slot,
    mark_sent,
    replace_order_cache,
    was_sent,
)


CARD_TABLE_ROW_LIMIT = 50
_cache_refresh_lock = asyncio.Lock()


def in_transit_percentage(pending_qty, ordered_qty) -> str:
    if not ordered_qty:
        return "0.0%"
    return f"{pending_qty / ordered_qty * 100:.1f}%"


async def refresh_order_cache(full: bool = False) -> dict:
    if _cache_refresh_lock.locked():
        async with _cache_refresh_lock:
            return {"waited": True}
    async with _cache_refresh_lock:
        lookback_days = (
            settings.jst_purchase_lookback_days
            if full else settings.jst_incremental_lookback_days
        )
        orders = await fetch_orders(lookback_days=lookback_days)
        db = connect(settings.database_path)
        try:
            if full:
                replace_order_cache(db, orders)
            else:
                merge_order_cache(db, orders)
        finally:
            db.close()
        return {"orders": len(orders), "full": full}


async def get_cached_orders() -> list:
    db = connect(settings.database_path)
    try:
        orders = cached_orders(db)
    finally:
        db.close()
    if orders:
        return orders
    await refresh_order_cache(full=True)
    db = connect(settings.database_path)
    try:
        return cached_orders(db)
    finally:
        db.close()


def render_report(purchaser: str, rows, manage_url: str) -> str:
    stat = summary(rows)
    order_count = len({r.order.order_no for r in rows})
    lines = [
        f"采购员：{purchaser}",
        "",
        "【本次在途汇总】",
        f"采购单：{order_count} 单；在途明细：{len(rows)} 条",
        f"订购 / 已入库 / 在途：{stat['ordered_qty']} / {stat['received_qty']} / {stat['pending_qty']}",
        f"🔴 紧急 {stat['red']} 条　🟡 关注 {stat['yellow']} 条",
        f"手动关闭/恢复预警：{manage_url}",
        "",
        "【在途明细表】",
        "等级｜采购单｜供应商｜商品/SKU｜交期｜剩余天数｜在途数量｜在途占比",
        "────────────────────────",
    ]
    for r in rows:
        o = r.order
        lines.append(
            f"{'🔴' if r.level == '红' else '🟡'}｜"
            f"{o.order_no}｜{o.supplier}｜{o.item_name or o.sku}｜"
            f"{o.delivery_date}｜{r.effective_days_left} 天｜{o.pending_qty}｜"
            f"{in_transit_percentage(o.pending_qty, o.ordered_qty)}"
        )
    return "\n".join(lines)


def build_report_card(purchaser: str, rows, manage_url: str) -> dict:
    stat = summary(rows)
    order_count = len({r.order.order_no for r in rows})
    table_rows = []
    for row in rows:
        order = row.order
        table_rows.append({
            "level": (
                "🔴 紧急" if row.level == "红"
                else "🟡 关注" if row.level == "黄"
                else "🟢 正常"
            ),
            "order_no": order.order_no,
            "supplier": order.supplier,
            "item": order.item_name or order.sku,
            "delivery_date": str(order.delivery_date),
            "days_left": f"{row.effective_days_left} 天",
            "pending_qty": str(order.pending_qty),
            "in_transit_percentage": in_transit_percentage(
                order.pending_qty, order.ordered_qty
            ),
        })
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {
                "tag": "plain_text",
                "content": f"采购交期预警｜{purchaser}",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**本次在途汇总**\n"
                    f"采购单：**{order_count}** 单　"
                    f"在途明细：**{len(rows)}** 条\n"
                    f"订购 / 已入库 / 在途："
                    f"**{stat['ordered_qty']} / {stat['received_qty']} / {stat['pending_qty']}**\n"
                    f"🔴 0–6天 {stat['red']} 条　🟡 7–10天 {stat['yellow']} 条　"
                    f"🟢 11–15天 {stat['green']} 条\n"
                    "**剩余天数按交期直接计算，不再扣减运输时间**"
                ),
            },
            {"tag": "hr"},
            {
                "tag": "table",
                "page_size": 10,
                "row_height": "low",
                "freeze_first_column": True,
                "columns": [
                    {"name": "level", "display_name": "等级", "data_type": "text", "width": "auto"},
                    {"name": "order_no", "display_name": "采购单", "data_type": "text", "width": "auto"},
                    {"name": "supplier", "display_name": "供应商", "data_type": "text", "width": "auto"},
                    {"name": "item", "display_name": "商品", "data_type": "text", "width": "auto"},
                    {"name": "delivery_date", "display_name": "交期", "data_type": "text", "width": "auto"},
                    {"name": "days_left", "display_name": "剩余天数", "data_type": "text", "width": "auto"},
                    {"name": "pending_qty", "display_name": "在途数量", "data_type": "text", "width": "auto"},
                    {"name": "in_transit_percentage", "display_name": "在途占比", "data_type": "text", "width": "auto"},
                ],
                "rows": table_rows,
            },
            {
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "管理预警"},
                    "type": "primary",
                    "url": manage_url,
                }],
            },
        ],
    }


def build_order_summary_card(purchaser: str, rows, manage_url: str) -> dict:
    grouped = {}
    for row in rows:
        grouped.setdefault(row.order.order_no, []).append(row)
    table_rows = []
    for order_no, order_rows in grouped.items():
        orders = [row.order for row in order_rows]
        most_urgent = min(order_rows, key=lambda row: row.effective_days_left)
        suppliers = sorted({order.supplier for order in orders if order.supplier})
        item_names = sorted({
            order.item_name or order.sku
            for order in orders
            if order.item_name or order.sku
        })
        ordered_qty = sum(order.ordered_qty for order in orders)
        received_qty = sum(order.received_qty for order in orders)
        pending_qty = sum(order.pending_qty for order in orders)
        table_rows.append({
            "level": (
                "🔴 紧急" if most_urgent.level == "红"
                else "🟡 关注" if most_urgent.level == "黄"
                else "🟢 正常"
            ),
            "order_no": order_no,
            "supplier": "、".join(suppliers),
            "item_names": "、".join(item_names),
            "sku_count": str(len({order.sku for order in orders})),
            "delivery_date": str(min(order.delivery_date for order in orders)),
            "days_left": f"{most_urgent.effective_days_left} 天",
            "ordered_qty": str(ordered_qty),
            "received_qty": str(received_qty),
            "pending_qty": str(pending_qty),
            "in_transit_percentage": in_transit_percentage(
                pending_qty, ordered_qty
            ),
        })
    table_rows.sort(key=lambda row: (int(row["days_left"].split()[0]), row["order_no"]))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": f"采购在途汇总｜{purchaser}",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**实时在途数据**\n"
                    f"采购单：**{len(grouped)}** 单　SKU 明细：**{len(rows)}** 条\n"
                    "表格已按采购单聚合，每张采购单仅显示一行。\n"
                    "🔴 0–6天　🟡 7–10天　🟢 11–15天\n"
                    "**剩余天数按交期直接计算，不再扣减运输时间**"
                ),
            },
            {"tag": "hr"},
            {
                "tag": "table",
                "page_size": 10,
                "row_height": "low",
                "freeze_first_column": True,
                "columns": [
                    {"name": "level", "display_name": "等级", "data_type": "text", "width": "auto"},
                    {"name": "order_no", "display_name": "采购单", "data_type": "text", "width": "auto"},
                    {"name": "supplier", "display_name": "供应商", "data_type": "text", "width": "auto"},
                    {"name": "item_names", "display_name": "商品名称", "data_type": "text", "width": "auto"},
                    {"name": "sku_count", "display_name": "SKU数", "data_type": "text", "width": "auto"},
                    {"name": "delivery_date", "display_name": "最早交期", "data_type": "text", "width": "auto"},
                    {"name": "days_left", "display_name": "最短剩余天数", "data_type": "text", "width": "auto"},
                    {"name": "ordered_qty", "display_name": "订购", "data_type": "text", "width": "auto"},
                    {"name": "received_qty", "display_name": "已入库", "data_type": "text", "width": "auto"},
                    {"name": "pending_qty", "display_name": "在途", "data_type": "text", "width": "auto"},
                    {"name": "in_transit_percentage", "display_name": "在途占比", "data_type": "text", "width": "auto"},
                ],
                "rows": table_rows,
            },
            {
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "管理预警"},
                    "type": "primary",
                    "url": manage_url,
                }],
            },
        ],
    }


def schedule_slot(buyer, now: datetime) -> str | None:
    if (now.hour, now.minute) != (
        buyer["schedule_hour"],
        buyer["schedule_minute"],
    ):
        return None
    frequency = buyer["schedule_frequency"]
    if frequency == "weekdays" and now.weekday() >= 5:
        return None
    if frequency == "weekly" and now.weekday() != buyer["schedule_weekday"]:
        return None
    if frequency not in {"daily", "weekdays", "weekly"}:
        return None
    slot = now.strftime("%Y-%m-%dT%H:%M")
    return None if buyer["last_schedule_slot"] == slot else slot


def _active_rows(db, buyer, orders):
    rows = build_alerts(
        orders, date.today(), buyer["purchaser"], settings.travel_buffer_days
    )
    closed = closed_order_numbers(db, buyer["purchaser"])
    return [
        row for row in rows
        if row.order.order_no not in closed and not row.order.is_received
    ]


def _current_in_transit(rows):
    return [
        row
        for row in rows
        if MIN_EFFECTIVE_DAYS <= row.effective_days_left <= MAX_EFFECTIVE_DAYS
    ]


async def _send_rows(buyer, rows) -> int:
    manage_url = f"{settings.app_base_url.rstrip('/')}/subscribe/{buyer['token']}"
    messages = 0
    for start in range(0, len(rows), CARD_TABLE_ROW_LIMIT):
        chunk = rows[start:start + CARD_TABLE_ROW_LIMIT]
        await send_card(
            buyer["feishu_open_id"],
            build_report_card(buyer["purchaser"], chunk, manage_url),
        )
        messages += 1
    return messages


async def _send_order_summaries(buyer, rows) -> int:
    manage_url = f"{settings.app_base_url.rstrip('/')}/subscribe/{buyer['token']}"
    return await _send_order_summaries_to(
        buyer["feishu_open_id"], buyer["purchaser"], rows, manage_url
    )


async def _send_order_summaries_to(
    feishu_open_id: str, purchaser: str, rows, manage_url: str
) -> int:
    order_numbers = list(dict.fromkeys(row.order.order_no for row in rows))
    messages = 0
    for start in range(0, len(order_numbers), CARD_TABLE_ROW_LIMIT):
        selected = set(order_numbers[start:start + CARD_TABLE_ROW_LIMIT])
        chunk = [row for row in rows if row.order.order_no in selected]
        await send_card(
            feishu_open_id,
            build_order_summary_card(purchaser, chunk, manage_url),
        )
        messages += 1
    return messages


async def _run_for_buyers(buyers, orders) -> dict:
    db = connect(settings.database_path)
    result = {"buyers": 0, "messages": 0, "orders": len(orders)}
    try:
        for buyer in buyers:
            result["buyers"] += 1
            due = [
                row
                for row in due_warning(_active_rows(db, buyer, orders), settings.warning_day_values)
                if not was_sent(
                    db, row.order.order_no, buyer["purchaser"], row.effective_days_left
                )
            ]
            if not due:
                continue
            result["messages"] += await _send_rows(buyer, due)
            for row in due:
                mark_sent(
                    db, row.order.order_no, buyer["purchaser"], row.effective_days_left
                )
    finally:
        db.close()
    return result


async def run_check() -> dict:
    db = connect(settings.database_path)
    try:
        buyers = [dict(row) for row in active_buyers(db)]
    finally:
        db.close()
    orders = await get_cached_orders()
    return await _run_for_buyers(buyers, orders)


async def run_scheduled_checks(now: datetime | None = None) -> dict:
    current = now or datetime.now()
    db = connect(settings.database_path)
    try:
        due_buyers = []
        slots = {}
        for row in active_buyers(db):
            buyer = dict(row)
            slot = schedule_slot(buyer, current)
            if slot:
                due_buyers.append(buyer)
                slots[buyer["token"]] = slot
    finally:
        db.close()
    if not due_buyers:
        return {"buyers": 0, "messages": 0, "orders": 0}
    orders = await get_cached_orders()
    personal_buyers = [buyer for buyer in due_buyers if not buyer["is_manager"]]
    manager_buyers = [buyer for buyer in due_buyers if buyer["is_manager"]]
    result = (
        await _run_for_buyers(personal_buyers, orders)
        if personal_buyers
        else {"buyers": 0, "messages": 0, "orders": 0}
    )
    for manager in manager_buyers:
        manager_result = await send_manager_report(
            manager["token"], manager["schedule_purchaser"], orders=orders
        )
        result["buyers"] += manager_result["buyers"]
        result["messages"] += manager_result["messages"]
        result["orders"] += manager_result["orders"]
    db = connect(settings.database_path)
    try:
        for buyer in due_buyers:
            mark_schedule_slot(db, buyer["token"], slots[buyer["token"]])
    finally:
        db.close()
    return result


async def send_manual_report(token: str) -> dict:
    orders = await get_cached_orders()
    db = connect(settings.database_path)
    try:
        row = buyer_by_token(db, token)
        if not row:
            raise KeyError(token)
        buyer = dict(row)
        rows = _current_in_transit(_active_rows(db, buyer, orders))
    finally:
        db.close()
    if not rows:
        await send_message(
            buyer["feishu_open_id"],
            "采购在途数据｜立即获取",
            "当前没有未全部入库的在途采购明细。",
        )
        return {"messages": 1, "rows": 0, "orders": 0}
    messages = await _send_order_summaries(buyer, rows)
    return {
        "messages": messages,
        "rows": len(rows),
        "orders": len({row.order.order_no for row in rows}),
    }


async def send_manager_report(
    token: str, purchaser: str = "*", orders=None
) -> dict:
    if orders is None:
        orders = await get_cached_orders()
    db = connect(settings.database_path)
    try:
        row = buyer_by_token(db, token)
        if not row or not row["is_manager"]:
            raise PermissionError(token)
        manager = dict(row)
    finally:
        db.close()
    available = sorted({order.purchaser for order in orders if order.purchaser})
    if purchaser != "*" and purchaser not in available:
        raise KeyError(purchaser)
    selected = available if purchaser == "*" else [purchaser]
    messages = rows_count = order_count = buyers_count = 0
    manage_url = f"{settings.app_base_url.rstrip('/')}/subscribe/{token}"
    send_jobs = []
    for name in selected:
        rows = _current_in_transit([
            row
            for row in build_alerts(
                orders, date.today(), name, settings.travel_buffer_days
            )
            if not row.order.is_received
        ])
        if not rows:
            continue
        buyers_count += 1
        rows_count += len(rows)
        order_count += len({row.order.order_no for row in rows})
        send_jobs.append(
            _send_order_summaries_to(
                manager["feishu_open_id"], name, rows, manage_url
            )
        )
    if send_jobs:
        messages = sum(await asyncio.gather(*send_jobs))
    if not messages:
        await send_message(
            manager["feishu_open_id"],
            "采购团队在途数据",
            "当前筛选范围内没有未全部入库的在途采购明细。",
        )
        messages = 1
    return {
        "messages": messages,
        "rows": rows_count,
        "orders": order_count,
        "buyers": buyers_count,
    }
