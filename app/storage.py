import secrets
import sqlite3
from datetime import date
from pathlib import Path


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
        """
    )
    return db


def upsert_buyer(db: sqlite3.Connection, purchaser: str, feishu_open_id: str) -> str:
    row = db.execute("SELECT token FROM buyers WHERE purchaser=?", (purchaser,)).fetchone()
    token = row["token"] if row else secrets.token_urlsafe(24)
    db.execute(
        """INSERT INTO buyers(purchaser, token, feishu_open_id)
           VALUES(?,?,?)
           ON CONFLICT(purchaser) DO UPDATE SET feishu_open_id=excluded.feishu_open_id""",
        (purchaser, token, feishu_open_id),
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


def buyer_by_token(db: sqlite3.Connection, token: str):
    return db.execute("SELECT * FROM buyers WHERE token=?", (token,)).fetchone()


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
        (order_no, purchaser, warning_day, date.today().isoformat()),
    )
    db.commit()
