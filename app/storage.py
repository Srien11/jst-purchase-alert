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
        CREATE TABLE IF NOT EXISTS oauth_states (
          state TEXT PRIMARY KEY,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS join_sessions (
          token TEXT PRIMARY KEY,
          open_id TEXT NOT NULL,
          feishu_name TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS join_requests (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          open_id TEXT NOT NULL,
          feishu_name TEXT NOT NULL,
          purchaser TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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


def create_join_session(
    db: sqlite3.Connection, open_id: str, feishu_name: str
) -> str:
    token = secrets.token_urlsafe(24)
    db.execute(
        "INSERT INTO join_sessions(token,open_id,feishu_name) VALUES(?,?,?)",
        (token, open_id, feishu_name),
    )
    db.commit()
    return token


def join_session(db: sqlite3.Connection, token: str):
    return db.execute(
        """SELECT * FROM join_sessions
           WHERE token=? AND created_at >= datetime('now','-20 minutes')""",
        (token,),
    ).fetchone()


def create_join_request(
    db: sqlite3.Connection, open_id: str, feishu_name: str, purchaser: str
) -> int:
    cursor = db.execute(
        """INSERT INTO join_requests(open_id,feishu_name,purchaser)
           VALUES(?,?,?)""",
        (open_id, feishu_name, purchaser),
    )
    db.commit()
    return int(cursor.lastrowid)


def pending_join_requests(db: sqlite3.Connection):
    return db.execute(
        "SELECT * FROM join_requests WHERE status='pending' ORDER BY created_at"
    ).fetchall()


def join_request_by_id(db: sqlite3.Connection, request_id: int):
    return db.execute(
        "SELECT * FROM join_requests WHERE id=?", (request_id,)
    ).fetchone()


def reject_join_request(db: sqlite3.Connection, request_id: int) -> bool:
    cursor = db.execute(
        """UPDATE join_requests SET status='rejected'
           WHERE id=? AND status='pending'""",
        (request_id,),
    )
    db.commit()
    return cursor.rowcount == 1


def approve_join_request(db: sqlite3.Connection, request_id: int) -> str | None:
    row = db.execute(
        "SELECT * FROM join_requests WHERE id=? AND status='pending'", (request_id,)
    ).fetchone()
    if not row:
        return None
    token = upsert_buyer(db, row["purchaser"], row["open_id"])
    set_buyer_enabled(db, token, True)
    db.execute("UPDATE join_requests SET status='approved' WHERE id=?", (request_id,))
    db.commit()
    return token
