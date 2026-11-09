"""Shared dataclasses for buy events, wallets, and signals."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class Wallet:
    label: str
    twitter: str
    address: str

    @property
    def handle(self) -> str:
        handle = (self.twitter or self.label).strip()
        if handle and not handle.startswith("@"):
            return f"@{handle}"
        return handle or self.label


@dataclass(slots=True)
class BuyEvent:
    wallet: str
    trader_label: str
    chain: str
    asset_type: str  # nft | token
    contract: str
    token_id: str | None
    amount: str
    price_usd: float | None
    tx_hash: str
    timestamp: int
    collection_slug: str | None = None
    symbol: str | None = None
    name: str | None = None
    twitter: str | None = None

    def key(self) -> tuple[str, str, str, str]:
        return (self.wallet.lower(), self.tx_hash.lower(), self.contract.lower(), self.token_id or "")

    def group_key(self) -> tuple[str, str]:
        return (self.chain.lower(), self.contract.lower())

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class TokenMetrics:
    symbol: str
    name: str
    price_usd: float | None
    liquidity_usd: float | None
    market_cap_usd: float | None
    volume_24h_usd: float | None
    chart_url: str | None
    pair_address: str | None = None
    verified: bool = False


@dataclass(slots=True)
class NftMetrics:
    name: str
    slug: str
    floor_price: float | None
    floor_symbol: str
    volume_24h: float | None
    volume_symbol: str
    opensea_url: str


@dataclass(slots=True)
class SignalSnapshot:
    chain: str
    contract: str
    asset_type: str
    name: str
    traders: list[BuyEvent]
    strength: str
    trader_count: int
    floor_or_price: str
    volume_or_liq: str
    opensea_url: str | None
    explorer_url: str | None
    chart_url: str | None
    extra: dict[str, Any] = field(default_factory=dict)


def strength_emoji(trader_count: int) -> str:
    if trader_count >= 4:
        return "🔴"
    if trader_count == 3:
        return "🟠"
    return "🟡"
