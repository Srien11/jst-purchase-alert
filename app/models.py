from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class PurchaseOrder:
    order_no: str
    purchaser: str
    supplier: str
    delivery_date: date
    ordered_qty: Decimal
    received_qty: Decimal
    sku: str = ""
    item_name: str = ""

    @property
    def pending_qty(self) -> Decimal:
        return max(Decimal("0"), self.ordered_qty - self.received_qty)

    @property
    def is_received(self) -> bool:
        return self.pending_qty == 0


@dataclass(frozen=True)
class AlertRow:
    order: PurchaseOrder
    calendar_days_left: int
    effective_days_left: int
    level: str
    advice: str

