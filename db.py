"""
db.py — SQLite persistence layer for ProofPay.
Uses stdlib sqlite3 only; no ORM.
"""

import sqlite3
import json
import os
from datetime import datetime, timezone
from typing import Optional

DB_PATH = os.getenv("DB_PATH", "proofpay.db")


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with _get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS milestones (
                id              TEXT PRIMARY KEY,
                title           TEXT NOT NULL,
                amount          REAL NOT NULL,
                currency        TEXT NOT NULL DEFAULT 'USD',
                criteria        TEXT NOT NULL,
                template        TEXT NOT NULL DEFAULT 'freelance',
                status          TEXT NOT NULL DEFAULT 'PENDING_FUNDING',
                paypal_order_id TEXT,
                authorization_id TEXT,
                approve_url     TEXT,
                ai_result       TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );
        """)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    if d.get("ai_result"):
        try:
            d["ai_result"] = json.loads(d["ai_result"])
        except (json.JSONDecodeError, TypeError):
            pass
    return d


def create_milestone(
    id: str,
    title: str,
    amount: float,
    currency: str,
    criteria: str,
    template: str,
    paypal_order_id: str,
    approve_url: str,
) -> dict:
    now = _now()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO milestones
              (id, title, amount, currency, criteria, template, status,
               paypal_order_id, approve_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'PENDING_FUNDING', ?, ?, ?, ?)
            """,
            (id, title, amount, currency, criteria, template, paypal_order_id, approve_url, now, now),
        )
    return get_milestone(id)


def get_milestone(id: str) -> Optional[dict]:
    with _get_conn() as conn:
        row = conn.execute("SELECT * FROM milestones WHERE id = ?", (id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_milestones() -> list[dict]:
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM milestones ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_milestone(id: str, **fields) -> Optional[dict]:
    if not fields:
        return get_milestone(id)
    # Serialize ai_result if it's a dict
    if "ai_result" in fields and isinstance(fields["ai_result"], dict):
        fields["ai_result"] = json.dumps(fields["ai_result"])
    fields["updated_at"] = _now()
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values()) + [id]
    with _get_conn() as conn:
        conn.execute(f"UPDATE milestones SET {set_clause} WHERE id = ?", values)
    return get_milestone(id)
