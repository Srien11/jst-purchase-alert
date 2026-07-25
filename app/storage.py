import secrets
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from .models import PurchaseOrder
from .timeutils import business_today


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS buyers (
          purchaser TEXT PRIMARY KEY,
          token TEXT UNIQUE NOT NULL,
          feishu_open_id TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS deliveries (
          order_no TEXT NOT NULL,
          purchaser TEXT NOT NULL,
          warning_day INTEGER NOT NULL,
          sent_on TEXT NOT NULL,
          PRIMARY KEY (order_no, purchaser, warning_day)
        );
        CREATE TABLE IF NOT EXISTS closed_alerts (
          order_no TEXT NOT NULL,
          purchaser TEXT NOT NULL,
          closed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (order_no, purchaser)
        );
        CREATE TABLE IF NOT EXISTS oauth_states (
          state TEXT PRIMARY KEY,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS system_events (
          event_key TEXT PRIMARY KEY,
          status TEXT NOT NULL,
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS order_cache (
          order_no TEXT NOT NULL,
          purchaser TEXT NOT NULL,
          supplier TEXT NOT NULL,
          delivery_date TEXT NOT NULL,
          ordered_qty TEXT NOT NULL,
          received_qty TEXT NOT NULL,
          sku TEXT NOT NULL,
          item_name TEXT NOT NULL,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
          PRIMARY KEY (order_no, sku, item_name, delivery_date)
        );
        CREATE TABLE IF NOT EXISTS cache_state (
          cache_key TEXT PRIMARY KEY,
          updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    columns = {row["name"] for row in db.execute("PRAGMA table_info(buyers)")}
    migrations = {
        "schedule_frequency": "TEXT NOT NULL DEFAULT 'daily'",
        "schedule_hour": "INTEGER NOT NULL DEFAULT 9",
        "schedule_minute": "INTEGER NOT NULL DEFAULT 0",
        "schedule_weekday": "INTEGER NOT NULL DEFAULT 0",
        "last_schedule_slot": "TEXT NOT NULL DEFAULT ''",
        "is_manager": "INTEGER NOT NULL DEFAULT 0",
        "schedule_purchaser": "TEXT NOT NULL DEFAULT '*'",
        "overdue_days": "INTEGER NOT NULL DEFAULT 0",
        "manual_overdue_days": "INTEGER NOT NULL DEFAULT 0",
        "schedule_overdue_days": "INTEGER NOT NULL DEFAULT 0",
    }
    added_overdue_columns = []
    for name, definition in migrations.items():
        if name not in columns:
            db.execute(f"ALTER TABLE buyers ADD COLUMN {name} {definition}")
            if name in {"manual_overdue_days", "schedule_overdue_days"}:
                added_overdue_columns.append(name)
    for name in added_overdue_columns:
        db.execute(f"UPDATE buyers SET {name}=overdue_days")
    db.commit()
    return db


def _insert_cached_orders(db: sqlite3.Connection, orders: list[PurchaseOrder]):
    db.executemany(
        """INSERT OR REPLACE INTO order_cache(
           order_no,purchaser,supplier,delivery_date,ordered_qty,
           received_qty,sku,item_name,updated_at)
           VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)""",
        [
            (
                order.order_no,
                order.purchaser,
                order.supplier,
                order.delivery_date.isoformat(),
                str(order.ordered_qty),
                str(order.received_qty),
                order.sku,
                order.item_name,
            )
            for order in orders
        ],
    )


def replace_order_cache(db: sqlite3.Connection, orders: list[PurchaseOrder]):
    with db:
        db.execute("DELETE FROM order_cache")
        _insert_cached_orders(db, orders)
        db.execute(
            """INSERT OR REPLACE INTO cache_state(cache_key,updated_at)
               VALUES('full',CURRENT_TIMESTAMP)"""
        )


def merge_order_cache(db: sqlite3.Connection, orders: list[PurchaseOrder]):
    order_numbers = sorted({order.order_no for order in orders})
    with db:
        if order_numbers:
            placeholders = ",".join("?" for _ in order_numbers)
            db.execute(
                f"DELETE FROM order_cache WHERE order_no IN ({placeholders})",
                order_numbers,
            )
        _insert_cached_orders(db, orders)
        db.execute(
            """INSERT OR REPLACE INTO cache_state(cache_key,updated_at)
               VALUES('incremental',CURRENT_TIMESTAMP)"""
        )


def cached_orders(db: sqlite3.Connection) -> list[PurchaseOrder]:
    return [
        PurchaseOrder(
            order_no=row["order_no"],
            purchaser=row["purchaser"],
            supplier=row["supplier"],
            delivery_date=date.fromisoformat(row["delivery_date"]),
            ordered_qty=Decimal(row["ordered_qty"]),
            received_qty=Decimal(row["received_qty"]),
            sku=row["sku"],
            item_name=row["item_name"],
        )
        for row in db.execute(
            "SELECT * FROM order_cache ORDER BY order_no,sku,item_name"
        )
    ]


def cached_purchasers(db: sqlite3.Connection) -> list[str]:
    return [
        row["purchaser"]
        for row in db.execute(
            """SELECT DISTINCT purchaser FROM order_cache
               WHERE purchaser<>'' ORDER BY purchaser"""
        )
    ]


def upsert_buyer(
    db: sqlite3.Connection,
    purchaser: str,
    feishu_open_id: str,
    is_manager: bool = False,
) -> str:
    row = db.execute("SELECT token FROM buyers WHERE purchaser=?", (purchaser,)).fetchone()
    token = row["token"] if row else secrets.token_urlsafe(24)
    db.execute(
        """INSERT INTO buyers(purchaser, token, feishu_open_id, is_manager)
           VALUES(?,?,?,?)
           ON CONFLICT(purchaser) DO UPDATE SET
             feishu_open_id=excluded.feishu_open_id,
             is_manager=MAX(buyers.is_manager, excluded.is_manager)""",
        (purchaser, token, feishu_open_id, int(is_manager)),
    )
    db.commit()
    return token


def enable_by_token(db: sqlite3.Connection, token: str):
    row = db.execute("SELECT * FROM buyers WHERE token=?", (token,)).fetchone()
    if row:
        db.execute("UPDATE buyers SET enabled=1 WHERE token=?", (token,))
        db.commit()
    return row


def set_buyer_enabled(db: sqlite3.Connection, token: str, enabled: bool):
    db.execute("UPDATE buyers SET enabled=? WHERE token=?", (int(enabled), token))
    db.commit()


def active_buyers(db: sqlite3.Connection):
    return db.execute("SELECT * FROM buyers WHERE enabled=1 ORDER BY purchaser").fetchall()


def all_buyers(db: sqlite3.Connection):
    return db.execute("SELECT * FROM buyers ORDER BY purchaser").fetchall()


def update_buyer_schedule(
    db: sqlite3.Connection,
    token: str,
    frequency: str,
    hour: int,
    minute: int,
    weekday: int,
    purchaser: str = "*",
    schedule_overdue_days: int = 0,
):
    db.execute(
        """UPDATE buyers SET schedule_frequency=?, schedule_hour=?,
           schedule_minute=?, schedule_weekday=?, schedule_purchaser=?,
           schedule_overdue_days=?,
           last_schedule_slot=''
           WHERE token=?""",
        (
            frequency,
            hour,
            minute,
            weekday,
            purchaser,
            schedule_overdue_days,
            token,
        ),
    )
    db.commit()


def update_buyer_manual_overdue_days(
    db: sqlite3.Connection, token: str, overdue_days: int
):
    db.execute(
        "UPDATE buyers SET manual_overdue_days=? WHERE token=?",
        (overdue_days, token),
    )
    db.commit()


def mark_schedule_slot(db: sqlite3.Connection, token: str, slot: str):
    db.execute(
        "UPDATE buyers SET last_schedule_slot=? WHERE token=?", (slot, token)
    )
    db.commit()


def system_event(db: sqlite3.Connection, event_key: str):
    return db.execute(
        "SELECT * FROM system_events WHERE event_key=?", (event_key,)
    ).fetchone()


def claim_system_event(db: sqlite3.Connection, event_key: str) -> bool:
    cursor = db.execute(
        """INSERT OR IGNORE INTO system_events(event_key,status)
           VALUES(?,'running')""",
        (event_key,),
    )
    db.commit()
    return cursor.rowcount == 1


def finish_system_event(db: sqlite3.Connection, event_key: str, detail: str):
    db.execute(
        """UPDATE system_events SET status='completed', detail=?,
           updated_at=CURRENT_TIMESTAMP WHERE event_key=?""",
        (detail, event_key),
    )
    db.commit()


def buyer_by_token(db: sqlite3.Connection, token: str):
    return db.execute("SELECT * FROM buyers WHERE token=?", (token,)).fetchone()


def buyer_by_open_id(db: sqlite3.Connection, open_id: str):
    return db.execute(
        "SELECT * FROM buyers WHERE feishu_open_id=?", (open_id,)
    ).fetchone()


def close_alert(db: sqlite3.Connection, order_no: str, purchaser: str):
    db.execute(
        "INSERT OR IGNORE INTO closed_alerts(order_no, purchaser) VALUES(?,?)",
        (order_no, purchaser),
    )
    db.commit()


def reopen_alert(db: sqlite3.Connection, order_no: str, purchaser: str):
    db.execute(
        "DELETE FROM closed_alerts WHERE order_no=? AND purchaser=?",
        (order_no, purchaser),
    )
    db.commit()


def closed_order_numbers(db: sqlite3.Connection, purchaser: str) -> set[str]:
    return {
        row["order_no"]
        for row in db.execute(
            "SELECT order_no FROM closed_alerts WHERE purchaser=?", (purchaser,)
        )
    }


def was_sent(db, order_no: str, purchaser: str, warning_day: int) -> bool:
    return db.execute(
        "SELECT 1 FROM deliveries WHERE order_no=? AND purchaser=? AND warning_day=?",
        (order_no, purchaser, warning_day),
    ).fetchone() is not None


def mark_sent(db, order_no: str, purchaser: str, warning_day: int):
    db.execute(
        "INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?)",
        (order_no, purchaser, warning_day, business_today().isoformat()),
    )
    db.commit()


def create_oauth_state(db: sqlite3.Connection) -> str:
    state = secrets.token_urlsafe(24)
    db.execute("INSERT INTO oauth_states(state) VALUES(?)", (state,))
    db.commit()
    return state


def consume_oauth_state(db: sqlite3.Connection, state: str) -> bool:
    row = db.execute(
        """SELECT 1 FROM oauth_states
           WHERE state=? AND created_at >= datetime('now','-10 minutes')""",
        (state,),
    ).fetchone()
    db.execute("DELETE FROM oauth_states WHERE state=?", (state,))
    db.commit()
    return row is not None
