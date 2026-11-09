"""Poll wallets, normalize buys, and fire clustered Telegram signals."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from typing import Any

from config import Settings
from db import Database
from models import BuyEvent, SignalSnapshot, Wallet, strength_emoji
from notifier import Notifier, explorer_url, format_eth, format_money
from sources.opensea import OpenSeaSource
from sources.tokens import TokenSource

log = logging.getLogger(__name__)


class Engine:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        opensea: OpenSeaSource,
        tokens: TokenSource,
        notifier: Notifier,
    ) -> None:
        self.settings = settings
        self.db = db
        self.opensea = opensea
        self.tokens = tokens
        self.notifier = notifier
        self.last_poll_at: float | None = None
        self.last_error: str | None = None
        self.polls_completed = 0
        self._stop = asyncio.Event()
        self._eth_price: float | None = None
        self._lock = asyncio.Lock()

    async def threshold(self) -> int:
        raw = await self.db.get_setting("threshold", str(self.settings.signal_threshold))
        return max(2, int(raw))

    async def window_hours(self) -> int:
        raw = await self.db.get_setting("window_hours", str(self.settings.signal_window_hours))
        return max(1, int(raw))

    async def window_seconds(self) -> int:
        return int((await self.window_hours()) * 3600)

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        log.info(
            "poller started interval=%ss chains=%s",
            self.settings.poll_interval_seconds,
            ",".join(self.settings.chains),
        )
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.poll_once()
            except Exception as exc:
                self.last_error = str(exc)
                log.exception("poll cycle failed: %s", exc)
            delay = self.settings.poll_interval_seconds - (time.monotonic() - started)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(1.0, delay))
            except asyncio.TimeoutError:
                continue

    async def poll_once(self) -> int:
        wallets = await self.db.list_wallets()
        ingested = 0
        await self._refresh_eth_price()
        window = await self.window_seconds()
        now = int(time.time())
        for wallet in wallets:
            ingested += await self.poll_wallet(wallet, now=now, window=window)
            await asyncio.sleep(0.2)
        self.last_poll_at = time.time()
        self.polls_completed += 1
        await self.db.set_poll_state("last_poll", str(int(self.last_poll_at)))
        await self.db.set_poll_state("last_error", self.last_error or "")
        log.info("poll cycle done wallets=%s new_events=%s", len(wallets), ingested)
        return ingested

    async def poll_wallet(self, wallet: Wallet, *, now: int, window: int) -> int:
        ingested = 0
        floor = now - window
        for source_name, fetcher in (
            ("opensea", self.opensea.fetch_buys),
            ("tokens", self.tokens.fetch_buys),
        ):
            cursor_ts, _ = await self.db.get_cursor(wallet.address, source_name)
            after = cursor_ts if cursor_ts else floor
            try:
                events = await fetcher(wallet, after)
            except Exception as exc:
                self.last_error = f"{source_name}:{wallet.label}:{exc}"
                log.warning("fetch %s %s failed: %s", source_name, wallet.label, exc)
                continue
            max_ts = after
            last_id = ""
            for event in events:
                max_ts = max(max_ts, event.timestamp)
                last_id = event.tx_hash
                if event.timestamp < after:
                    continue
                if await self.ingest(event):
                    ingested += 1
            # Successful fetch: park the cursor at the newest event, or slide
            # forward with a 2-minute overlap when the wallet was quiet.
            cursor_ts = max_ts if events else max(after, now - 120)
            await self.db.set_cursor(wallet.address, source_name, cursor_ts, last_id)
        return ingested

    async def ingest(self, event: BuyEvent) -> bool:
        async with self._lock:
            if not await self._passes_filters(event):
                return False
            inserted = await self.db.insert_event(event)
            if not inserted:
                return False
            log.info(
                "buy %s %s %s %s tx=%s",
                event.trader_label,
                event.asset_type,
                event.chain,
                event.contract[:10],
                event.tx_hash[:12],
            )
            await self._evaluate(event)
            return True

    async def _passes_filters(self, event: BuyEvent) -> bool:
        if event.asset_type == "nft":
            return True
        metrics = await self.tokens.token_metrics(event.chain, event.contract)
        if metrics is None:
            log.debug("skip token with no DexScreener pair %s", event.contract)
            return False
        liquidity = metrics.liquidity_usd or 0.0
        if liquidity < self.settings.min_token_liquidity_usd:
            log.debug("skip low-liq token %s liq=%.0f", metrics.symbol, liquidity)
            return False
        if not metrics.verified and liquidity < self.settings.min_token_liquidity_usd:
            return False
        event.symbol = event.symbol or metrics.symbol
        event.name = event.name or metrics.name or metrics.symbol
        if event.price_usd is None and metrics.price_usd is not None:
            try:
                amount = float(event.amount)
                event.price_usd = amount * metrics.price_usd if amount > 0 else metrics.price_usd
            except (TypeError, ValueError):
                event.price_usd = metrics.price_usd
        return True

    async def _evaluate(self, event: BuyEvent) -> None:
        now = int(time.time())
        window = await self.window_seconds()
        threshold = await self.threshold()
        since = now - window
        buys = await self.db.recent_buys(event.chain, event.contract, since)
        latest_by_wallet: dict[str, BuyEvent] = {}
        for buy in buys:
            latest_by_wallet[buy.wallet.lower()] = buy
        traders = list(latest_by_wallet.values())
        traders.sort(key=lambda b: b.timestamp)
        if len(traders) < threshold:
            return

        existing = await self.db.find_open_signal(
            event.chain, event.contract, now, window
        )
        if existing and int(existing["trader_count"]) >= len(traders):
            return

        snapshot = await self._build_snapshot(event, traders, now)
        msg_id = existing["telegram_msg_id"] if existing else None
        new_msg_id = await self.notifier.send_or_edit(
            snapshot, now=now, existing_msg_id=msg_id
        )
        window_start = traders[0].timestamp
        await self.db.upsert_signal(
            chain=event.chain,
            contract=event.contract,
            asset_type=event.asset_type,
            name=snapshot.name,
            trader_count=snapshot.trader_count,
            strength=snapshot.strength,
            window_start=window_start,
            window_end=now,
            telegram_msg_id=new_msg_id,
            payload={"contract": event.contract, "chain": event.chain},
            existing_id=existing["id"] if existing else None,
        )

    async def _build_snapshot(
        self, event: BuyEvent, traders: list[BuyEvent], now: int
    ) -> SignalSnapshot:
        name = event.name or event.symbol or event.contract
        floor_or_price = "n/a"
        volume_or_liq = "n/a"
        opensea_url = None
        chart_url = None
        explorer = explorer_url(event.chain, traders[-1].tx_hash)

        if event.asset_type == "nft":
            slug = event.collection_slug or next(
                (t.collection_slug for t in traders if t.collection_slug), None
            )
            metrics = None
            if slug:
                metrics = await self.opensea.collection_metrics(slug)
            if metrics is None:
                metrics = await self.opensea.collection_by_contract(event.chain, event.contract)
            if metrics:
                name = metrics.name or name
                floor_or_price = format_eth(metrics.floor_price, metrics.floor_symbol)
                vol = metrics.volume_24h
                volume_or_liq = (
                    f"{vol:,.2f} {metrics.volume_symbol}" if vol is not None else "n/a"
                )
                opensea_url = metrics.opensea_url
            else:
                opensea_url = (
                    f"https://opensea.io/assets/{event.chain}/{event.contract}"
                )
        else:
            metrics = await self.tokens.token_metrics(event.chain, event.contract)
            if metrics:
                name = metrics.symbol or metrics.name or name
                floor_or_price = format_money(metrics.price_usd)
                volume_or_liq = format_money(metrics.liquidity_usd)
                chart_url = metrics.chart_url
                opensea_url = (
                    f"https://opensea.io/token/{event.chain}/{event.contract}"
                )

        return SignalSnapshot(
            chain=event.chain,
            contract=event.contract,
            asset_type=event.asset_type,
            name=name,
            traders=traders,
            strength=strength_emoji(len(traders)),
            trader_count=len(traders),
            floor_or_price=floor_or_price,
            volume_or_liq=volume_or_liq,
            opensea_url=opensea_url,
            explorer_url=explorer,
            chart_url=chart_url,
        )

    async def _refresh_eth_price(self) -> None:
        weth = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
        try:
            metrics = await self.tokens.token_metrics("ethereum", weth)
            if metrics and metrics.price_usd:
                self._eth_price = metrics.price_usd
        except Exception as exc:
            log.debug("eth price refresh failed: %s", exc)

    def health(self) -> dict[str, Any]:
        return {
            "last_poll_at": self.last_poll_at,
            "polls_completed": self.polls_completed,
            "last_error": self.last_error,
            "test_mode": self.notifier.test_mode,
        }


def group_events(events: list[BuyEvent]) -> dict[tuple[str, str], list[BuyEvent]]:
    grouped: dict[tuple[str, str], list[BuyEvent]] = defaultdict(list)
    for event in events:
        grouped[event.group_key()].append(event)
    return grouped
