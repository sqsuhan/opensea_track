"""Telegram signal bot for the OpenSea $1K to $10K challenge traders."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from config import ROOT, Settings, load_settings
from db import Database
from engine import Engine
from http_util import RateLimitedClient
from models import BuyEvent, TokenMetrics, Wallet
from notifier import Notifier, register_admin_handlers
from sources.opensea import OpenSeaSource
from sources.tokens import TokenSource

log = logging.getLogger(__name__)
WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)


def persist_wallets_file(path: Path, wallets: list[Wallet]) -> None:
    payload = [
        {"label": w.label, "twitter": w.twitter, "wallet": w.address}
        for w in wallets
    ]
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_wallets_file(path: Path) -> list[Wallet]:
    if not path.exists():
        raise FileNotFoundError(f"wallets file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    wallets: list[Wallet] = []
    for item in raw:
        address = str(item.get("wallet") or item.get("address") or "").strip()
        label = str(item.get("label") or "").strip()
        twitter = str(item.get("twitter") or item.get("x") or "").strip()
        if not WALLET_RE.match(address):
            log.warning("skipping invalid wallet for %s: %s", label, address)
            continue
        if not label:
            label = address[:10]
        wallets.append(Wallet(label=label, twitter=twitter, address=address.lower()))
    return wallets


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenSea trader cluster signal bot")
    parser.add_argument(
        "--test",
        action="store_true",
        help="Replay tests/sample_events.json and print alerts without Telegram",
    )
    parser.add_argument(
        "--sample",
        type=Path,
        default=ROOT / "tests" / "sample_events.json",
        help="Sample events file for --test",
    )
    return parser.parse_args()


async def build_runtime(
    settings: Settings, *, test_mode: bool, db_path: Path | None = None
) -> tuple[Database, Engine, RateLimitedClient]:
    db = Database(db_path or settings.db_path)
    await db.connect()
    if not test_mode and await db.wallet_count() == 0:
        wallets = load_wallets_file(settings.wallets_path)
        await db.seed_wallets(wallets)
        log.info("seeded %s wallets from %s", len(wallets), settings.wallets_path)

    client = RateLimitedClient(timeout=25.0, headers={"User-Agent": "opensea-track/1.0"})
    opensea = OpenSeaSource(settings, client)
    tokens = TokenSource(settings, client)
    notifier = Notifier(settings, application=None)
    engine = Engine(settings, db, opensea, tokens, notifier)
    return db, engine, client


async def run_test(settings: Settings, sample_path: Path) -> int:
    settings.db_path = ROOT / "data" / "test_signals.db"
    if settings.db_path.exists():
        settings.db_path.unlink()
    db, engine, client = await build_runtime(settings, test_mode=True)
    try:
        payload = json.loads(sample_path.read_text(encoding="utf-8"))
        for item in payload.get("wallets") or []:
            await db.add_wallet(
                Wallet(
                    label=item["label"],
                    twitter=item.get("twitter") or "",
                    address=item["wallet"].lower(),
                )
            )
        fake_metrics = payload.get("token_metrics") or {}

        async def fake_token_metrics(chain: str, contract: str) -> TokenMetrics | None:
            key = f"{chain.lower()}:{contract.lower()}"
            raw = fake_metrics.get(key)
            if not raw:
                return None
            return TokenMetrics(
                symbol=raw.get("symbol") or "?",
                name=raw.get("name") or raw.get("symbol") or "?",
                price_usd=raw.get("price_usd"),
                liquidity_usd=raw.get("liquidity_usd"),
                market_cap_usd=raw.get("market_cap_usd"),
                volume_24h_usd=raw.get("volume_24h_usd"),
                chart_url=raw.get("chart_url") or f"https://dexscreener.com/{chain}/{contract}",
                verified=bool(raw.get("verified", True)),
            )

        async def fake_collection_metrics(slug: str):
            return None

        async def fake_collection_by_contract(chain: str, contract: str):
            return None

        engine.tokens.token_metrics = fake_token_metrics  # type: ignore[method-assign]
        engine.opensea.collection_metrics = fake_collection_metrics  # type: ignore[method-assign]
        engine.opensea.collection_by_contract = fake_collection_by_contract  # type: ignore[method-assign]

        now = int(time.time())
        fired = 0
        for raw in payload.get("events") or []:
            ts = raw.get("timestamp")
            if ts is None:
                ts = now - int(raw.get("seconds_ago") or 0)
            event = BuyEvent(
                wallet=raw["wallet"].lower(),
                trader_label=raw["trader_label"],
                chain=raw.get("chain") or "ethereum",
                asset_type=raw["asset_type"],
                contract=raw["contract"].lower(),
                token_id=raw.get("token_id"),
                amount=str(raw.get("amount") or "1"),
                price_usd=raw.get("price_usd"),
                tx_hash=raw["tx_hash"].lower(),
                timestamp=int(ts),
                collection_slug=raw.get("collection_slug"),
                symbol=raw.get("symbol"),
                name=raw.get("name"),
                twitter=raw.get("twitter"),
            )
            if await engine.ingest(event):
                fired += 1
        print(f"\nReplayed {len(payload.get('events') or [])} events, ingested {fired}.")
        print(f"Alerts printed: {len(engine.notifier.printed)}")
        return 0
    finally:
        await client.close()
        await db.close()


def _ensure_event_loop() -> None:
    """Python 3.14 no longer creates a default loop for get_event_loop().

    python-telegram-bot <22.4 still calls asyncio.get_event_loop() inside
    Application.run_polling. Create and set a loop when none is current.
    """
    try:
        asyncio.get_running_loop()
        return
    except RuntimeError:
        pass
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)


def _ago_status(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = int(time.time() - ts)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h ago"


def run_bot(settings: Settings) -> None:
    from telegram.ext import Application

    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env.")
    if not settings.telegram_channel_id:
        raise SystemExit("TELEGRAM_CHANNEL_ID is missing.")
    if not settings.opensea_api_key:
        log.warning("OPENSEA_API_KEY is empty; NFT polling will fail until it is set.")
    if not settings.alchemy_api_key:
        log.warning("ALCHEMY_API_KEY is empty; token polling is disabled.")
    if not settings.telegram_admin_ids:
        log.warning("TELEGRAM_ADMIN_IDS is empty; any user who DMs the bot can run admin commands.")

    runtime: dict[str, Any] = {}

    async def db() -> Database:
        return runtime["db"]

    async def engine() -> Engine:
        return runtime["engine"]

    async def cmd_traders(update, context) -> str:
        wallets = await (await db()).list_wallets()
        if not wallets:
            return "No wallets tracked. Use /add <label> <wallet>."
        lines = [f"{i}. {w.label} {w.handle} `{w.address}`" for i, w in enumerate(wallets, 1)]
        return "Tracked traders:\n" + "\n".join(lines)

    async def cmd_add(update, context) -> str:
        args = context.args or []
        if len(args) < 2:
            return "Usage: /add <label> <wallet> [@twitter]"
        label, address = args[0], args[1]
        twitter = args[2] if len(args) > 2 else ""
        if not WALLET_RE.match(address):
            return "Wallet must be a 0x-prefixed 40-hex address."
        await (await db()).add_wallet(
            Wallet(label=label, twitter=twitter, address=address.lower())
        )
        persist_wallets_file(settings.wallets_path, await (await db()).list_wallets())
        return f"Added {label} {address.lower()}"

    async def cmd_remove(update, context) -> str:
        args = context.args or []
        if not args:
            return "Usage: /remove <label>"
        ok = await (await db()).remove_wallet(args[0])
        if ok:
            persist_wallets_file(settings.wallets_path, await (await db()).list_wallets())
        return f"Removed {args[0]}" if ok else f"No trader named {args[0]}"

    async def cmd_threshold(update, context) -> str:
        eng = await engine()
        store = await db()
        args = context.args or []
        if not args:
            return f"Current threshold: {await eng.threshold()} traders"
        value = int(args[0])
        if value < 2:
            return "Threshold must be >= 2"
        await store.set_setting("threshold", str(value))
        return f"Threshold set to {value}"

    async def cmd_window(update, context) -> str:
        eng = await engine()
        store = await db()
        args = context.args or []
        if not args:
            return f"Current window: {await eng.window_hours()} hours"
        value = int(args[0])
        if value < 1:
            return "Window must be >= 1 hour"
        await store.set_setting("window_hours", str(value))
        return f"Window set to {value} hours"

    async def cmd_signals(update, context) -> str:
        rows = await (await db()).last_signals(10)
        if not rows:
            return "No signals yet."
        lines = []
        for row in rows:
            lines.append(
                f"{row['strength']} {row['trader_count']}x {row['name']} "
                f"({row['chain']}) {row['contract'][:10]}…"
            )
        return "Last signals:\n" + "\n".join(lines)

    async def cmd_status(update, context) -> str:
        eng = await engine()
        health = eng.health()
        wallets = await (await db()).list_wallets()
        return (
            f"Status: running\n"
            f"Wallets: {len(wallets)}\n"
            f"Chains: {', '.join(settings.chains)}\n"
            f"Threshold: {await eng.threshold()}\n"
            f"Window: {await eng.window_hours()}h\n"
            f"Last poll: {_ago_status(health['last_poll_at'])}\n"
            f"Polls completed: {health['polls_completed']}\n"
            f"Last error: {health['last_error'] or 'none'}"
        )

    async def on_start(app) -> None:
        store, eng, client = await build_runtime(settings, test_mode=False)
        eng.notifier = Notifier(settings, app)
        runtime["db"] = store
        runtime["engine"] = eng
        runtime["client"] = client
        runtime["poll_task"] = asyncio.create_task(eng.run_forever(), name="poller")
        try:
            from telegram import BotCommand

            await app.bot.set_my_commands(
                [
                    BotCommand("traders", "List tracked wallets"),
                    BotCommand("add", "Add a wallet: /add label 0x... @handle"),
                    BotCommand("remove", "Remove a wallet by label"),
                    BotCommand("threshold", "Set or show trader threshold"),
                    BotCommand("window", "Set or show window hours"),
                    BotCommand("signals", "Last 10 signals"),
                    BotCommand("status", "Health and last poll"),
                ]
            )
        except Exception as exc:
            log.warning("could not set bot commands: %s", exc)
        log.info("bot online, posting to %s", settings.telegram_channel_id)

    async def on_stop(app) -> None:
        eng: Engine | None = runtime.get("engine")
        if eng is not None:
            eng.stop()
        poll_task = runtime.get("poll_task")
        if poll_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(poll_task), timeout=8)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                poll_task.cancel()
        client = runtime.get("client")
        if client is not None:
            await client.close()
        store = runtime.get("db")
        if store is not None:
            await store.close()

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .post_init(on_start)
        .post_shutdown(on_stop)
        .build()
    )
    register_admin_handlers(
        application,
        settings,
        {
            "traders": cmd_traders,
            "add": cmd_add,
            "remove": cmd_remove,
            "threshold": cmd_threshold,
            "window": cmd_window,
            "signals": cmd_signals,
            "status": cmd_status,
        },
    )
    _ensure_event_loop()
    application.run_polling(allowed_updates=["message"], drop_pending_updates=True)


def main() -> None:
    args = parse_args()
    settings = load_settings()
    setup_logging(settings.log_level)
    if args.test:
        raise SystemExit(asyncio.run(run_test(settings, args.sample)))
    run_bot(settings)


if __name__ == "__main__":
    main()
