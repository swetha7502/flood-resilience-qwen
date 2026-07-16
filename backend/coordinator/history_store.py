"""
history_store.py — durable audit log for flood risk decisions.

Why this exists separately from the Redis zone history in coordinator.py:
  Redis's "history:{zone}" list is a rolling window of the last 5 decisions,
  used purely to give Qwen recent context in its prompt. It's ephemeral by
  design (ltrim keeps it small so prompts stay small) and disappears if
  Redis restarts. That's the right tradeoff for prompt context, but it means
  there was previously no durable record of what the system actually
  decided over time -- which a real deployment needs for post-incident
  review, liability, and regulatory audit ("why did Zone B get evacuated
  at 3:14am on the 12th?").

  This module is that durable record. It does NOT feed back into Qwen's
  prompt (that would bloat every request with the zone's entire history) --
  it's a write-mostly append log, queried separately via /audit endpoints.

Uses stdlib sqlite3 only (no new dependency), wrapped in asyncio.to_thread
since sqlite3 is a blocking API and this is an asyncio codebase.
"""

import asyncio
import json
import os
import sqlite3
import time

DB_PATH = os.getenv(
    "FLOODGUARD_HISTORY_DB_PATH",
    os.path.join(os.path.dirname(__file__), "..", "flood_history.db"),
)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _init_db_sync():
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zone TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                confidence REAL,
                source TEXT,
                requires_human_approval INTEGER,
                reasoning TEXT,
                timestamp REAL NOT NULL,
                raw_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_decisions_zone_ts ON decisions(zone, timestamp)"
        )
        conn.commit()
    finally:
        conn.close()


async def init_db():
    await asyncio.to_thread(_init_db_sync)


def _record_sync(decision: dict):
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO decisions
                (zone, risk_level, confidence, source, requires_human_approval, reasoning, timestamp, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                decision.get("zone"),
                decision.get("risk_level"),
                decision.get("confidence"),
                decision.get("source"),
                int(bool(decision.get("requires_human_approval"))),
                decision.get("reasoning"),
                decision.get("timestamp", time.time()),
                json.dumps(decision),
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def record_decision(decision: dict):
    """
    Fire-and-forget durable write. Deliberately does not raise into the
    caller's request path -- an audit-log write failing should never be
    the reason a flood risk decision fails to reach the frontend/edge
    agent. Logged, not raised.
    """
    try:
        await asyncio.to_thread(_record_sync, decision)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Audit log write failed: %s", e)


def _query_sync(zone: str | None, limit: int) -> list:
    conn = _connect()
    try:
        if zone:
            rows = conn.execute(
                "SELECT raw_json FROM decisions WHERE zone = ? ORDER BY timestamp DESC LIMIT ?",
                (zone, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT raw_json FROM decisions ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(r["raw_json"]) for r in rows]
    finally:
        conn.close()


async def query_decisions(zone: str | None = None, limit: int = 200) -> list:
    return await asyncio.to_thread(_query_sync, zone, limit)
