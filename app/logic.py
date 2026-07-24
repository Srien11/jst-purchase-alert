from datetime import date
from decimal import Decimal
from .models import AlertRow, PurchaseOrder

MIN_EFFECTIVE_DAYS = 0
MAX_EFFECTIVE_DAYS = 15


def classify(order: PurchaseOrder, today: date, travel_buffer_days: int) -> AlertRow:
    calendar_days = (order.delivery_date - today).days
    effective_days = calendar_days
    if order.is_received:
        level, advice = "绿", "已全部入库，无需跟进"
    elif effective_days <= 6:
        level, advice = "红", "立即联系供应商，确认发货/到货时间并准备异常升级"
    elif effective_days <= 10:
        level, advice = "黄", "今日确认生产与物流节点，锁定预计到仓时间"
    else:
        level, advice = "绿", "保持常规跟踪，临近下一预警节点前复核"
    return AlertRow(order, calendar_days, effective_days, level, advice)


def build_alerts(
    orders: list[PurchaseOrder], today: date, purchaser: str, travel_buffer_days: int
) -> list[AlertRow]:
    rows = [
        classify(o, today, travel_buffer_days)
        for o in orders
        if (
            o.purchaser == purchaser
            and MIN_EFFECTIVE_DAYS
            <= (o.delivery_date - today).days
            <= MAX_EFFECTIVE_DAYS
        )
    ]
    return sorted(
        rows,
        key=lambda r: (
            {"红": 0, "黄": 1, "绿": 2}[r.level],
            r.effective_days_left,
            -float(r.order.pending_qty),
        ),
    )


def summary(rows: list[AlertRow]) -> dict:
    ordered = sum((r.order.ordered_qty for r in rows), Decimal("0"))
    received = sum((r.order.received_qty for r in rows), Decimal("0"))
    return {
        "orders": len(rows),
        "fully_received_orders": sum(r.order.is_received for r in rows),
        "pending_orders": sum(not r.order.is_received for r in rows),
        "ordered_qty": ordered,
        "received_qty": received,
        "pending_qty": ordered - received,
        "red": sum(r.level == "红" for r in rows),
        "yellow": sum(r.level == "黄" for r in rows),
        "green": sum(r.level == "绿" for r in rows),
    }


def due_warning(rows: list[AlertRow], warning_days: tuple[int, ...]) -> list[AlertRow]:
    return [
        r for r in rows
        if not r.order.is_received and r.effective_days_left in warning_days
    ]
