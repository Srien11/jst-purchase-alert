from datetime import date
from .config import settings
from .feishu import send_card
from .jushuitan import fetch_orders
from .logic import build_alerts, due_warning, summary
from .storage import active_buyers, closed_order_numbers, connect, mark_sent, was_sent


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
        "等级｜采购单｜供应商｜商品/SKU｜交期｜有效剩余｜在途数量",
        "────────────────────────",
    ]
    for r in rows:
        o = r.order
        lines.append(
            f"{'🔴' if r.level == '红' else '🟡'}｜"
            f"{o.order_no}｜{o.supplier}｜{o.item_name or o.sku}｜"
            f"{o.delivery_date}｜{r.effective_days_left} 天｜{o.pending_qty}"
        )
    return "\n".join(lines)


def build_report_card(purchaser: str, rows, manage_url: str) -> dict:
    stat = summary(rows)
    order_count = len({r.order.order_no for r in rows})
    table_rows = []
    for row in rows:
        order = row.order
        table_rows.append({
            "level": "🔴 紧急" if row.level == "红" else "🟡 关注",
            "order_no": order.order_no,
            "supplier": order.supplier,
            "item": order.item_name or order.sku,
            "delivery_date": str(order.delivery_date),
            "days_left": f"{row.effective_days_left} 天",
            "pending_qty": str(order.pending_qty),
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
                    f"🔴 紧急 {stat['red']} 条　🟡 关注 {stat['yellow']} 条"
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
                    {"name": "days_left", "display_name": "有效剩余", "data_type": "text", "width": "auto"},
                    {"name": "pending_qty", "display_name": "在途数量", "data_type": "text", "width": "auto"},
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


async def run_check() -> dict:
    orders = await fetch_orders()
    db = connect(settings.database_path)
    result = {"buyers": 0, "messages": 0, "orders": len(orders)}
    try:
        for buyer in active_buyers(db):
            result["buyers"] += 1
            rows = build_alerts(
                orders, date.today(), buyer["purchaser"], settings.travel_buffer_days
            )
            closed = closed_order_numbers(db, buyer["purchaser"])
            active_rows = [r for r in rows if r.order.order_no not in closed]
            due = [
                r for r in due_warning(active_rows, settings.warning_day_values)
                if not was_sent(
                    db, r.order.order_no, buyer["purchaser"], r.effective_days_left
                )
            ]
            if not due:
                continue
            await send_card(
                buyer["feishu_open_id"],
                build_report_card(
                    buyer["purchaser"],
                    due,
                    f"{settings.app_base_url.rstrip('/')}/subscribe/{buyer['token']}",
                ),
            )
            for row in due:
                mark_sent(
                    db, row.order.order_no, buyer["purchaser"], row.effective_days_left
                )
            result["messages"] += 1
    finally:
        db.close()
    return result
