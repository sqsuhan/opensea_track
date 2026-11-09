"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    return int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, str(default)).strip()
    return float(raw)


def _path(name: str, default: str) -> Path:
    raw = os.getenv(name, default).strip()
    path = Path(raw)
    if not path.is_absolute():
        path = ROOT / path
    return path


@dataclass
class Settings:
    telegram_bot_token: str
    telegram_channel_id: str
    telegram_admin_ids: set[int]
    opensea_api_key: str
    alchemy_api_key: str
    poll_interval_seconds: int = 60
    signal_threshold: int = 2
    signal_window_hours: int = 24
    chains: list[str] = field(default_factory=lambda: ["ethereum", "base", "robinhood", "arc", "ink"])
    min_nft_price_eth: float = 0.001
    min_token_liquidity_usd: float = 50_000.0
    db_path: Path = field(default_factory=lambda: ROOT / "data" / "signals.db")
    wallets_path: Path = field(default_factory=lambda: ROOT / "wallets.json")
    log_level: str = "INFO"

    @property
    def window_seconds(self) -> int:
        return int(self.signal_window_hours * 3600)


def load_settings() -> Settings:
    admin_raw = os.getenv("TELEGRAM_ADMIN_IDS", "")
    admin_ids: set[int] = set()
    for item in _split_csv(admin_raw):
        admin_ids.add(int(item))

    chains = normalize_chains(
        _split_csv(os.getenv("CHAINS", "ethereum,base,robinhood,arc,ink"))
    )
    if not chains:
        chains = list(DEFAULT_CHAINS)

    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_channel_id=os.getenv("TELEGRAM_CHANNEL_ID", "").strip(),
        telegram_admin_ids=admin_ids,
        opensea_api_key=os.getenv("OPENSEA_API_KEY", "").strip(),
        alchemy_api_key=os.getenv("ALCHEMY_API_KEY", "").strip(),
        poll_interval_seconds=max(15, _int("POLL_INTERVAL_SECONDS", 60)),
        signal_threshold=max(2, _int("SIGNAL_THRESHOLD", 2)),
        signal_window_hours=max(1, _int("SIGNAL_WINDOW_HOURS", 24)),
        chains=chains,
        min_nft_price_eth=_float("MIN_NFT_PRICE_ETH", 0.001),
        min_token_liquidity_usd=_float("MIN_TOKEN_LIQUIDITY_USD", 50_000.0),
        db_path=_path("DB_PATH", "data/signals.db"),
        wallets_path=_path("WALLETS_PATH", "wallets.json"),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )


DEFAULT_CHAINS = ["ethereum", "base", "robinhood", "arc", "ink"]

# User typos / dashboard names → OpenSea + Alchemy slugs.
CHAIN_ALIASES: dict[str, str] = {
    "eth": "ethereum",
    "mainnet": "ethereum",
    "eth-mainnet": "ethereum",
    "base-mainnet": "base",
    "robhinhood": "robinhood",
    "robinhoodchain": "robinhood",
    "robinhood-chain": "robinhood",
    "robinhood_chain": "robinhood",
    "robinhood-mainnet": "robinhood",
    "rh": "robinhood",
    "hood": "robinhood",
    "hoodchain": "robinhood",
    "arc-mainnet": "arc",
    "circle-arc": "arc",
    "ink-mainnet": "ink",
    "inkonchain": "ink",
}


def normalize_chains(raw: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        slug = CHAIN_ALIASES.get(item.lower(), item.lower())
        if slug and slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


QUOTE_TOKENS: dict[str, set[str]] = {
    "ethereum": {
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
        "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
        "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
        "0x4c9edd5852cd905f086c759e8383e09bff1e68b3",  # USDe
    },
    "base": {
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "0x4200000000000000000000000000000000000006",  # WETH
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",  # USDC
        "0x50c5725949a6f0c72e6c4a641f24049a917db0cb",  # DAI
        "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",  # USDT
    },
    "robinhood": {
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # WETH
        "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG
        "0x80e0e24718dbfcad49ecaa6f1e6c89a190586ca8",  # bridged USDC
    },
    "arc": {
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",  # native USDC gas
        "0x3600000000000000000000000000000000000000",  # USDC ERC-20
        "0x128cc466b61f542da60c70e3aa11c10e19b84edb",  # WETH
    },
    "ink": {
        "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "0x4200000000000000000000000000000000000006",  # WETH (OP Stack)
        "0x2d270e6886d130d724215a266106e6832161eaed",  # USDC
    },
}

NATIVE_SENTINEL = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

ALCHEMY_NETWORKS: dict[str, str] = {
    "ethereum": "eth-mainnet",
    "base": "base-mainnet",
    "robinhood": "robinhood-mainnet",
    "arc": "arc-mainnet",
    "ink": "ink-mainnet",
}

# OpenSea /api/v2 chain query slug. Keep independent of Alchemy host names.
OPENSEA_CHAIN: dict[str, str] = {
    "ethereum": "ethereum",
    "base": "base",
    "robinhood": "robinhood",
    "arc": "arc",
    "ink": "ink",
}

EXPLORERS: dict[str, str] = {
    "ethereum": "https://etherscan.io/tx/{tx}",
    "base": "https://basescan.org/tx/{tx}",
    "robinhood": "https://robinhoodchain.blockscout.com/tx/{tx}",
    "arc": "https://explorer.arc.io/tx/{tx}",
    "ink": "https://explorer.inkonchain.com/tx/{tx}",
}

DEXSCREENER_CHAIN: dict[str, str] = {
    "ethereum": "ethereum",
    "base": "base",
    "robinhood": "robinhood",
    "arc": "arc",
    "ink": "ink",
}

STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDE", "USD", "USDG"}
ETH_SYMBOLS = {"ETH", "WETH"}
