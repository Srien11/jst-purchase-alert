from datetime import date
from .config import settings
from .feishu import send_message
from .jushuitan import fetch_orders
from .logic import build_alerts, due_warning, summary
from .storage import active_buyers, closed_order_numbers, connect, mark_sent, was_sent


def render_report(purchaser: str, rows, manage_url: str) -> str:
    stat = summary(rows)
    lines = [
        f"采购员：{purchaser}",
        f"采购单：{stat['orders']}；已全部入库：{stat['fully_received_orders']}；未全部入库：{stat['pending_orders']}",
        f"订购/已入库/未入库数量：{stat['ordered_qty']} / {stat['received_qty']} / {stat['pending_qty']}",
        f"🔴 {stat['red']}  🟡 {stat['yellow']}  🟢 {stat['green']}",
        f"手动关闭/恢复预警：{manage_url}",
        "",
        "优先跟进清单：",
    ]
    for r in rows:
        o = r.order
        lines.append(
            f"{'🔴' if r.level == '红' else '🟡' if r.level == '黄' else '🟢'} "
            f"{o.order_no}｜{o.supplier}｜{o.sku} {o.item_name}｜"
            f"交期 {o.delivery_date}｜有效剩余 {r.effective_days_left} 天｜"
            f"未入库 {o.pending_qty}｜{r.advice}"
        )
    return "\n".join(lines)


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
            await send_message(
                buyer["feishu_open_id"],
                f"采购交期预警｜{buyer['purchaser']}",
                render_report(
                    buyer["purchaser"],
                    active_rows,
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
