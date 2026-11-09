"""SQLite persistence for wallets, buy events, signals, and poll cursors."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from models import BuyEvent, Wallet

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS wallets (
    address     TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    twitter     TEXT NOT NULL DEFAULT '',
    created_at  INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet          TEXT NOT NULL,
    trader_label    TEXT NOT NULL,
    chain           TEXT NOT NULL,
    asset_type      TEXT NOT NULL,
    contract        TEXT NOT NULL,
    token_id        TEXT,
    amount          TEXT NOT NULL DEFAULT '1',
    price_usd       REAL,
    tx_hash         TEXT NOT NULL,
    timestamp       INTEGER NOT NULL,
    collection_slug TEXT,
    symbol          TEXT,
    name            TEXT,
    twitter         TEXT,
    UNIQUE(wallet, tx_hash, contract, token_id)
);

CREATE INDEX IF NOT EXISTS idx_events_group_time
    ON events(chain, contract, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_wallet_time
    ON events(wallet, timestamp);

CREATE TABLE IF NOT EXISTS signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    chain           TEXT NOT NULL,
    contract        TEXT NOT NULL,
    asset_type      TEXT NOT NULL,
    name            TEXT NOT NULL,
    trader_count    INTEGER NOT NULL,
    strength        TEXT NOT NULL,
    window_start    INTEGER NOT NULL,
    window_end      INTEGER NOT NULL,
    telegram_msg_id INTEGER,
    payload_json    TEXT NOT NULL DEFAULT '{}',
    created_at      INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    updated_at      INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_signals_group
    ON signals(chain, contract, window_end);

CREATE TABLE IF NOT EXISTS cursors (
    wallet          TEXT NOT NULL,
    source          TEXT NOT NULL,
    last_timestamp  INTEGER NOT NULL DEFAULT 0,
    last_id         TEXT NOT NULL DEFAULT '',
    updated_at      INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (wallet, source)
);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_state (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def seed_wallets(self, wallets: Iterable[Wallet]) -> None:
        await self.conn.executemany(
            """
            INSERT INTO wallets(address, label, twitter)
            VALUES (?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                label=excluded.label,
                twitter=excluded.twitter
            """,
            [(w.address.lower(), w.label, w.twitter) for w in wallets],
        )
        await self.conn.commit()

    async def wallet_count(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS n FROM wallets")
        row = await cur.fetchone()
        return int(row["n"] if row else 0)

    async def list_wallets(self) -> list[Wallet]:
        cur = await self.conn.execute(
            "SELECT address, label, twitter FROM wallets ORDER BY label COLLATE NOCASE"
        )
        rows = await cur.fetchall()
        return [
            Wallet(label=r["label"], twitter=r["twitter"], address=r["address"])
            for r in rows
        ]

    async def add_wallet(self, wallet: Wallet) -> None:
        await self.conn.execute(
            """
            INSERT INTO wallets(address, label, twitter)
            VALUES (?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                label=excluded.label,
                twitter=excluded.twitter
            """,
            (wallet.address.lower(), wallet.label, wallet.twitter),
        )
        await self.conn.commit()

    async def remove_wallet(self, label: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM wallets WHERE lower(label)=lower(?)",
            (label,),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def get_cursor(self, wallet: str, source: str) -> tuple[int, str]:
        cur = await self.conn.execute(
            "SELECT last_timestamp, last_id FROM cursors WHERE wallet=? AND source=?",
            (wallet.lower(), source),
        )
        row = await cur.fetchone()
        if row is None:
            return 0, ""
        return int(row["last_timestamp"]), str(row["last_id"] or "")

    async def set_cursor(self, wallet: str, source: str, timestamp: int, last_id: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO cursors(wallet, source, last_timestamp, last_id, updated_at)
            VALUES (?, ?, ?, ?, strftime('%s','now'))
            ON CONFLICT(wallet, source) DO UPDATE SET
                last_timestamp=excluded.last_timestamp,
                last_id=excluded.last_id,
                updated_at=excluded.updated_at
            """,
            (wallet.lower(), source, int(timestamp), last_id),
        )
        await self.conn.commit()

    async def insert_event(self, event: BuyEvent) -> bool:
        try:
            await self.conn.execute(
                """
                INSERT INTO events(
                    wallet, trader_label, chain, asset_type, contract, token_id,
                    amount, price_usd, tx_hash, timestamp, collection_slug,
                    symbol, name, twitter
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.wallet.lower(),
                    event.trader_label,
                    event.chain.lower(),
                    event.asset_type,
                    event.contract.lower(),
                    event.token_id or "",
                    event.amount,
                    event.price_usd,
                    event.tx_hash.lower(),
                    int(event.timestamp),
                    event.collection_slug,
                    event.symbol,
                    event.name,
                    event.twitter,
                ),
            )
            await self.conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def recent_buys(
        self, chain: str, contract: str, since: int
    ) -> list[BuyEvent]:
        cur = await self.conn.execute(
            """
            SELECT wallet, trader_label, chain, asset_type, contract, token_id,
                   amount, price_usd, tx_hash, timestamp, collection_slug,
                   symbol, name, twitter
            FROM events
            WHERE chain=? AND contract=? AND timestamp >= ?
            ORDER BY timestamp ASC
            """,
            (chain.lower(), contract.lower(), int(since)),
        )
        rows = await cur.fetchall()
        return [self._event_from_row(r) for r in rows]

    async def last_signals(self, limit: int = 10) -> list[dict[str, Any]]:
        cur = await self.conn.execute(
            """
            SELECT id, chain, contract, asset_type, name, trader_count, strength,
                   window_start, window_end, telegram_msg_id, created_at, updated_at
            FROM signals
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def find_open_signal(
        self, chain: str, contract: str, now: int, window_seconds: int
    ) -> dict[str, Any] | None:
        cur = await self.conn.execute(
            """
            SELECT id, chain, contract, asset_type, name, trader_count, strength,
                   window_start, window_end, telegram_msg_id, payload_json
            FROM signals
            WHERE chain=? AND contract=? AND window_end >= ?
            ORDER BY window_end DESC
            LIMIT 1
            """,
            (chain.lower(), contract.lower(), int(now) - window_seconds),
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def upsert_signal(
        self,
        *,
        chain: str,
        contract: str,
        asset_type: str,
        name: str,
        trader_count: int,
        strength: str,
        window_start: int,
        window_end: int,
        telegram_msg_id: int | None,
        payload: dict[str, Any],
        existing_id: int | None = None,
    ) -> int:
        payload_json = json.dumps(payload)
        if existing_id is not None:
            await self.conn.execute(
                """
                UPDATE signals SET
                    asset_type=?, name=?, trader_count=?, strength=?,
                    window_start=?, window_end=?, telegram_msg_id=?,
                    payload_json=?, updated_at=strftime('%s','now')
                WHERE id=?
                """,
                (
                    asset_type,
                    name,
                    trader_count,
                    strength,
                    window_start,
                    window_end,
                    telegram_msg_id,
                    payload_json,
                    existing_id,
                ),
            )
            await self.conn.commit()
            return existing_id

        cur = await self.conn.execute(
            """
            INSERT INTO signals(
                chain, contract, asset_type, name, trader_count, strength,
                window_start, window_end, telegram_msg_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                chain.lower(),
                contract.lower(),
                asset_type,
                name,
                trader_count,
                strength,
                window_start,
                window_end,
                telegram_msg_id,
                payload_json,
            ),
        )
        await self.conn.commit()
        return int(cur.lastrowid)

    async def get_setting(self, key: str, default: str) -> str:
        cur = await self.conn.execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        )
        row = await cur.fetchone()
        return str(row["value"]) if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, str(value)),
        )
        await self.conn.commit()

    async def set_poll_state(self, key: str, value: str) -> None:
        await self.conn.execute(
            """
            INSERT INTO poll_state(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        await self.conn.commit()

    async def get_poll_state(self, key: str) -> str | None:
        cur = await self.conn.execute(
            "SELECT value FROM poll_state WHERE key=?", (key,)
        )
        row = await cur.fetchone()
        return str(row["value"]) if row else None

    @staticmethod
    def _event_from_row(row: aiosqlite.Row) -> BuyEvent:
        return BuyEvent(
            wallet=row["wallet"],
            trader_label=row["trader_label"],
            chain=row["chain"],
            asset_type=row["asset_type"],
            contract=row["contract"],
            token_id=row["token_id"],
            amount=row["amount"],
            price_usd=row["price_usd"],
            tx_hash=row["tx_hash"],
            timestamp=int(row["timestamp"]),
            collection_slug=row["collection_slug"],
            symbol=row["symbol"],
            name=row["name"],
            twitter=row["twitter"],
        )
