"""SQLite state store: one file, no server, survives a restart.

The default for anyone running a single instance who wants a spend cap to mean
something after `systemctl restart`. sqlite3 is synchronous, so every statement runs
on the loop's executor.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from ..observability.usage import USAGE_FIELDS
from .base import StateStore

SCHEMA = """
CREATE TABLE IF NOT EXISTS affinity (
    session_key TEXT PRIMARY KEY,
    account_id  TEXT NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS affinity_expiry ON affinity (expires_at);

CREATE TABLE IF NOT EXISTS spend_ledger (
    scope  TEXT NOT NULL,
    name   TEXT NOT NULL,
    at     REAL NOT NULL,
    amount REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS spend_lookup ON spend_ledger (scope, name, at);

CREATE TABLE IF NOT EXISTS managed_keys (
    name    TEXT PRIMARY KEY,
    record  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS managed_accounts (
    id      TEXT PRIMARY KEY,
    record  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    name    TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
    at                    REAL NOT NULL,
    account_id            TEXT NOT NULL,
    key_name              TEXT NOT NULL,
    model                 TEXT NOT NULL,
    status                INTEGER NOT NULL,
    input_tokens          INTEGER NOT NULL,
    output_tokens         INTEGER NOT NULL,
    cache_read_tokens     INTEGER NOT NULL,
    cache_creation_tokens INTEGER NOT NULL,
    cost_usd              REAL NOT NULL,
    latency               REAL NOT NULL,
    affinity_broken       INTEGER NOT NULL,
    via                   TEXT NOT NULL,
    error                 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_at ON usage_events (at);
CREATE INDEX IF NOT EXISTS usage_account ON usage_events (account_id, at);

CREATE TABLE IF NOT EXISTS key_requests (
    key_name TEXT NOT NULL,
    at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS key_requests_lookup ON key_requests (key_name, at);
"""

#: Ledger rows older than this are pruned. Comfortably past any sane spend window.
LEDGER_RETENTION_SECONDS = 90 * 24 * 3600


class SqliteStateStore(StateStore):
    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._connection: Optional[sqlite3.Connection] = None

    # ---- plumbing ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._connection is None:
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory and not os.path.isdir(directory):
                os.makedirs(directory, exist_ok=True)
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(SCHEMA)
            self._connection.commit()
        return self._connection

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self._locked(fn, *args))

    def _locked(self, fn: Callable[..., Any], *args: Any) -> Any:
        with self._lock:
            return fn(self._connect(), *args)

    async def startup(self) -> None:
        await self._run(lambda conn: conn.execute("SELECT 1").fetchone())
        await self._run(self._prune)

    async def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    @staticmethod
    def _prune(conn: sqlite3.Connection) -> None:
        now = time.time()
        conn.execute("DELETE FROM affinity WHERE expires_at < ?", (now,))
        conn.execute(
            "DELETE FROM spend_ledger WHERE at < ?", (now - LEDGER_RETENTION_SECONDS,)
        )
        conn.execute("DELETE FROM key_requests WHERE at < ?", (now - 3600.0,))
        conn.execute(
            "DELETE FROM usage_events WHERE at < ?", (now - LEDGER_RETENTION_SECONDS,)
        )
        conn.commit()

    # ---- interface -----------------------------------------------------------

    async def get_affinity(self, session_key: str) -> Optional[str]:
        def query(conn: sqlite3.Connection) -> Optional[str]:
            row = conn.execute(
                "SELECT account_id, expires_at FROM affinity WHERE session_key = ?",
                (session_key,),
            ).fetchone()
            if row is None:
                return None
            if row[1] < time.time():
                conn.execute("DELETE FROM affinity WHERE session_key = ?", (session_key,))
                conn.commit()
                return None
            return row[0]

        return await self._run(query)

    async def set_affinity(self, session_key: str, account_id: str, ttl: float) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO affinity (session_key, account_id, expires_at) "
                "VALUES (?, ?, ?) ON CONFLICT(session_key) DO UPDATE SET "
                "account_id = excluded.account_id, expires_at = excluded.expires_at",
                (session_key, account_id, time.time() + ttl),
            )
            conn.commit()

        await self._run(write)

    async def clear_affinity(self, session_key: str) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM affinity WHERE session_key = ?", (session_key,))
            conn.commit()

        await self._run(write)

    async def clear_affinity_for_account(self, account_id: str) -> int:
        def write(conn: sqlite3.Connection) -> int:
            cursor = conn.execute(
                "DELETE FROM affinity WHERE account_id = ?", (account_id,)
            )
            conn.commit()
            return int(cursor.rowcount or 0)

        return await self._run(write)

    async def record_key_request(self, key_name: str, now: float) -> int:
        def write(conn: sqlite3.Connection) -> int:
            conn.execute(
                "INSERT INTO key_requests (key_name, at) VALUES (?, ?)", (key_name, now)
            )
            conn.execute(
                "DELETE FROM key_requests WHERE key_name = ? AND at < ?",
                (key_name, now - 60.0),
            )
            conn.commit()
            row = conn.execute(
                "SELECT COUNT(*) FROM key_requests WHERE key_name = ? AND at >= ?",
                (key_name, now - 60.0),
            ).fetchone()
            return int(row[0]) if row else 0

        return await self._run(write)

    async def add_spend(self, scope: str, name: str, amount: float) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO spend_ledger (scope, name, at, amount) VALUES (?, ?, ?, ?)",
                (scope, name, time.time(), amount),
            )
            conn.commit()

        await self._run(write)

    async def get_spend(self, scope: str, name: str, window_seconds: float) -> float:
        def query(conn: sqlite3.Connection) -> float:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM spend_ledger "
                "WHERE scope = ? AND name = ? AND at >= ?",
                (scope, name, time.time() - window_seconds),
            ).fetchone()
            return float(row[0]) if row else 0.0

        return await self._run(query)

    async def record_usage(self, sample: Dict[str, Any]) -> None:
        columns = ", ".join(USAGE_FIELDS)
        placeholders = ", ".join("?" for _ in USAGE_FIELDS)
        values = tuple(sample.get(field) for field in USAGE_FIELDS)

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                f"INSERT INTO usage_events ({columns}) VALUES ({placeholders})", values
            )
            conn.commit()

        await self._run(write)

    async def usage_rows(
        self, since: float, until: float, account_id: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        columns = ", ".join(USAGE_FIELDS)

        def query(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
            if account_id is None:
                sql = f"SELECT {columns} FROM usage_events WHERE at >= ? AND at < ?"
                params: tuple = (since, until)
            else:
                sql = (
                    f"SELECT {columns} FROM usage_events "
                    "WHERE account_id = ? AND at >= ? AND at < ?"
                )
                params = (account_id, since, until)
            rows = conn.execute(sql + " ORDER BY at", params).fetchall()
            return [dict(zip(USAGE_FIELDS, row)) for row in rows]

        return await self._run(query)

    async def put_key(self, record: Dict[str, Any]) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO managed_keys (name, record) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET record = excluded.record",
                (str(record["name"]), json.dumps(record)),
            )
            conn.commit()

        await self._run(write)

    async def delete_key(self, name: str) -> bool:
        def write(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute("DELETE FROM managed_keys WHERE name = ?", (name,))
            conn.commit()
            return cursor.rowcount > 0

        return await self._run(write)

    async def list_keys(self) -> List[Dict[str, Any]]:
        def query(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
            rows = conn.execute("SELECT record FROM managed_keys").fetchall()
            return [json.loads(row[0]) for row in rows]

        return await self._run(query)

    async def put_account(self, record: Dict[str, Any]) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO managed_accounts (id, record) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET record = excluded.record",
                (str(record["id"]), json.dumps(record)),
            )
            conn.commit()

        await self._run(write)

    async def delete_account(self, account_id: str) -> bool:
        def write(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute("DELETE FROM managed_accounts WHERE id = ?", (account_id,))
            conn.commit()
            return cursor.rowcount > 0

        return await self._run(write)

    async def list_accounts(self) -> List[Dict[str, Any]]:
        def query(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
            rows = conn.execute("SELECT record FROM managed_accounts ORDER BY id").fetchall()
            return [json.loads(row[0]) for row in rows]

        return await self._run(query)

    async def put_setting(self, name: str, value: Any) -> None:
        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT INTO settings (name, value) VALUES (?, ?) "
                "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
                (str(name), json.dumps(value)),
            )
            conn.commit()

        await self._run(write)

    async def get_settings(self) -> Dict[str, Any]:
        def query(conn: sqlite3.Connection) -> Dict[str, Any]:
            out: Dict[str, Any] = {}
            for name, value in conn.execute("SELECT name, value FROM settings"):
                try:
                    out[name] = json.loads(value)
                except ValueError:
                    continue
            return out

        return await self._run(query)

    async def spend_by_scope(self, scope: str, window_seconds: float) -> Dict[str, float]:
        def query(conn: sqlite3.Connection) -> Dict[str, float]:
            rows = conn.execute(
                "SELECT name, COALESCE(SUM(amount), 0) FROM spend_ledger "
                "WHERE scope = ? AND at >= ? GROUP BY name",
                (scope, time.time() - window_seconds),
            ).fetchall()
            return {row[0]: float(row[1]) for row in rows}

        return await self._run(query)
