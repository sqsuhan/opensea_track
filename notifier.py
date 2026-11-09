"""Telegram channel delivery and admin DM commands."""

from __future__ import annotations

import logging
from html import escape
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    filters,
)

from config import EXPLORERS, Settings
from models import SignalSnapshot, strength_emoji

log = logging.getLogger(__name__)

AdminFn = Callable[..., Awaitable[str]]


def _ago(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def format_money(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "n/a"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M{suffix}"
    if value >= 1_000:
        return f"${value / 1_000:.1f}k{suffix}"
    if value >= 1:
        return f"${value:.2f}{suffix}"
    return f"${value:.6f}{suffix}".rstrip("0").rstrip(".")


def format_eth(value: float | None, symbol: str = "ETH") -> str:
    if value is None:
        return "n/a"
    if value >= 1:
        return f"{value:.3f} {symbol}"
    return f"{value:.4f} {symbol}"


def render_signal(snapshot: SignalSnapshot, now: int) -> str:
    emoji = snapshot.strength or strength_emoji(snapshot.trader_count)
    title_name = escape(snapshot.name)
    lines = [
        f"{emoji} SIGNAL: {snapshot.trader_count} traders bought {title_name}",
        f"Chain: {escape(snapshot.chain)} | Type: {escape(snapshot.asset_type.upper())}",
        "",
        "Traders:",
    ]
    seen: set[str] = set()
    for event in snapshot.traders:
        key = event.wallet.lower()
        if key in seen:
            continue
        seen.add(key)
        handle = escape(event.twitter or f"@{event.trader_label}")
        price = format_money(event.price_usd) if event.price_usd is not None else "n/a"
        age = _ago(now - event.timestamp)
        lines.append(f"• {handle} bought at {price} ({age})")
    lines.append("")
    if snapshot.asset_type == "nft":
        lines.append(f"Floor: {escape(snapshot.floor_or_price)} | 24h Vol: {escape(snapshot.volume_or_liq)}")
    else:
        lines.append(f"Price: {escape(snapshot.floor_or_price)} | Liq: {escape(snapshot.volume_or_liq)}")
    lines.append("")
    lines.append("<i>Not financial advice. DYOR.</i>")
    return "\n".join(lines)


def signal_keyboard(snapshot: SignalSnapshot) -> InlineKeyboardMarkup | None:
    buttons: list[InlineKeyboardButton] = []
    if snapshot.opensea_url:
        buttons.append(InlineKeyboardButton("OpenSea", url=snapshot.opensea_url))
    if snapshot.explorer_url:
        buttons.append(InlineKeyboardButton("Explorer", url=snapshot.explorer_url))
    if snapshot.chart_url:
        buttons.append(InlineKeyboardButton("Chart", url=snapshot.chart_url))
    if not buttons:
        return None
    return InlineKeyboardMarkup([buttons])


class Notifier:
    def __init__(self, settings: Settings, application: Application | None = None) -> None:
        self.settings = settings
        self.application = application
        self.test_mode = application is None
        self.printed: list[str] = []

    async def send_or_edit(
        self,
        snapshot: SignalSnapshot,
        *,
        now: int,
        existing_msg_id: int | None,
    ) -> int | None:
        text = render_signal(snapshot, now)
        markup = signal_keyboard(snapshot)
        if self.test_mode:
            prefix = "EDIT" if existing_msg_id else "SEND"
            rendered = f"=== {prefix} ===\n{text}"
            print(rendered)
            self.printed.append(rendered)
            return existing_msg_id or 1

        if self.application is None:
            return None
        bot = self.application.bot
        chat_id = self.settings.telegram_channel_id
        try:
            if existing_msg_id:
                await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=existing_msg_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
                return existing_msg_id
            sent = await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                disable_web_page_preview=True,
            )
            return sent.message_id
        except Exception as exc:
            log.exception("Telegram delivery failed: %s", exc)
            if existing_msg_id:
                try:
                    sent = await bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=markup,
                        reply_to_message_id=existing_msg_id,
                        disable_web_page_preview=True,
                    )
                    return sent.message_id
                except Exception:
                    try:
                        sent = await bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode=ParseMode.HTML,
                            reply_markup=markup,
                            disable_web_page_preview=True,
                        )
                        return sent.message_id
                    except Exception:
                        log.exception("Telegram fallback failed")
            return existing_msg_id


def explorer_url(chain: str, tx_hash: str) -> str:
    template = EXPLORERS.get(chain, "https://etherscan.io/tx/{tx}")
    return template.format(tx=tx_hash)


def register_admin_handlers(
    application: Application,
    settings: Settings,
    handlers: dict[str, AdminFn],
) -> None:
    admin_filter = filters.ChatType.PRIVATE
    if settings.telegram_admin_ids:
        admin_filter = filters.ChatType.PRIVATE & filters.User(user_id=list(settings.telegram_admin_ids))

    async def wrap(name: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if update.effective_user is None or update.message is None:
            return
        if settings.telegram_admin_ids and update.effective_user.id not in settings.telegram_admin_ids:
            await update.message.reply_text("Unauthorized.")
            return
        fn = handlers[name]
        try:
            reply = await fn(update, context)
        except Exception as exc:
            log.exception("admin command /%s failed", name)
            reply = f"Error: {exc}"
        await update.message.reply_text(reply)

    for name in handlers:
        async def _handler(update: Update, context: ContextTypes.DEFAULT_TYPE, _name: str = name) -> None:
            await wrap(_name, update, context)

        application.add_handler(CommandHandler(name, _handler, filters=admin_filter))
