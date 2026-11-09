"""Alchemy token transfers + DexScreener enrichment."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Any

import httpx

from config import (
    ALCHEMY_NETWORKS,
    DEXSCREENER_CHAIN,
    ETH_SYMBOLS,
    NATIVE_SENTINEL,
    QUOTE_TOKENS,
    STABLE_SYMBOLS,
    Settings,
)
from http_util import RateLimitedClient
from models import BuyEvent, TokenMetrics, Wallet

log = logging.getLogger(__name__)

DEXSCREENER = "https://api.dexscreener.com"


def _addr(value: str | None) -> str:
    return (value or "").lower()


def _parse_ts(raw: Any) -> int:
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        value = int(raw)
        return value // 1000 if value > 10_000_000_000 else value
    text = str(raw)
    if text.endswith("Z"):
        from datetime import datetime, timezone

        try:
            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return 0
    if text.isdigit():
        value = int(text)
        return value // 1000 if value > 10_000_000_000 else value
    return 0


class TokenSource:
    def __init__(self, settings: Settings, client: RateLimitedClient) -> None:
        self.settings = settings
        self.client = client
        self._metrics_cache: dict[tuple[str, str], TokenMetrics | None] = {}
        self._disabled_chains: dict[str, float] = {}

    async def fetch_buys(self, wallet: Wallet, after: int) -> list[BuyEvent]:
        if not self.settings.alchemy_api_key:
            log.debug("Alchemy key missing; skipping token polling")
            return []
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        now_ts = time.time()
        for chain in self.settings.chains:
            if chain in self._disabled_chains:
                if now_ts < self._disabled_chains[chain]:
                    continue
                del self._disabled_chains[chain]
            if chain not in ALCHEMY_NETWORKS:
                log.debug("no Alchemy network mapping for %s; skipping token poll", chain)
                continue
            transfers = await self._transfers(wallet.address, chain, after)
            for item in transfers:
                tx = _addr(item.get("hash") or item.get("uniqueId"))
                if tx:
                    grouped[tx].append(item)

        buys: list[BuyEvent] = []
        quotes = set()
        for chain in self.settings.chains:
            quotes |= QUOTE_TOKENS.get(chain, set())

        for tx_hash, legs in grouped.items():
            buy = self._classify_swap(wallet, tx_hash, legs, quotes)
            if buy is not None:
                buys.append(buy)
        buys.sort(key=lambda e: e.timestamp)
        return buys

    async def _transfers(self, address: str, chain: str, after: int) -> list[dict[str, Any]]:
        network = ALCHEMY_NETWORKS.get(chain)
        if not network:
            return []
        url = f"https://{network}.g.alchemy.com/v2/{self.settings.alchemy_api_key}"
        params: dict[str, Any] = {
            "fromBlock": "0x0",
            "toBlock": "latest",
            "toAddress": address,
            "category": ["external", "erc20"],
            "withMetadata": True,
            "excludeZeroValue": True,
            "maxCount": "0x64",
            "order": "desc",
        }
        out: list[dict[str, Any]] = []
        page_key = None
        pages = 0
        while pages < 6:
            body_params = dict(params)
            if page_key:
                body_params["pageKey"] = page_key
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "alchemy_getAssetTransfers",
                "params": [body_params],
            }
            try:
                data = await self.client.post_json(url, json=payload)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 403:
                    if chain not in self._disabled_chains:
                        self._disabled_chains[chain] = time.time() + 600
                        log.warning(
                            "Alchemy network '%s' returned 403 Forbidden (not enabled for app). Disabling for 10m. Visit https://dashboard.alchemy.com to enable.",
                            chain,
                        )
                else:
                    log.warning("Alchemy transfers failed %s %s: %s", address, chain, exc)
                break
            except Exception as exc:
                log.warning("Alchemy transfers failed %s %s: %s", address, chain, exc)
                break
            result = data.get("result") or {}
            transfers = result.get("transfers") or []
            stop = False
            for item in transfers:
                item["_chain"] = chain
                ts = _parse_ts((item.get("metadata") or {}).get("blockTimestamp"))
                item["_ts"] = ts
                if after and ts and ts <= after:
                    stop = True
                    continue
                out.append(item)
            page_key = result.get("pageKey")
            pages += 1
            if stop or not page_key or not transfers:
                break

        # Incoming-only misses the spent quote token. Fetch outgoing quote legs
        # for the same recent window so swap classification works.
        outgoing = await self._outgoing(address, chain, after)
        out.extend(outgoing)
        return out

    async def _outgoing(self, address: str, chain: str, after: int) -> list[dict[str, Any]]:
        if chain in self._disabled_chains:
            return []
        network = ALCHEMY_NETWORKS.get(chain)
        if not network:
            return []
        url = f"https://{network}.g.alchemy.com/v2/{self.settings.alchemy_api_key}"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "alchemy_getAssetTransfers",
            "params": [
                {
                    "fromBlock": "0x0",
                    "toBlock": "latest",
                    "fromAddress": address,
                    "category": ["external", "erc20"],
                    "withMetadata": True,
                    "excludeZeroValue": True,
                    "maxCount": "0x64",
                    "order": "desc",
                }
            ],
        }
        try:
            data = await self.client.post_json(url, json=payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 403:
                if chain not in self._disabled_chains:
                    self._disabled_chains[chain] = time.time() + 600
                    log.warning(
                        "Alchemy network '%s' returned 403 Forbidden (not enabled for app). Disabling for 10m. Visit https://dashboard.alchemy.com to enable.",
                        chain,
                    )
            else:
                log.warning("Alchemy outgoing failed %s %s: %s", address, chain, exc)
            return []
        except Exception as exc:
            log.warning("Alchemy outgoing failed %s %s: %s", address, chain, exc)
            return []
        result = data.get("result") or {}
        out = []
        for item in result.get("transfers") or []:
            ts = _parse_ts((item.get("metadata") or {}).get("blockTimestamp"))
            if after and ts and ts <= after:
                continue
            item["_chain"] = chain
            item["_ts"] = ts
            out.append(item)
        return out

    def _classify_swap(
        self,
        wallet: Wallet,
        tx_hash: str,
        legs: list[dict[str, Any]],
        quotes: set[str],
    ) -> BuyEvent | None:
        wallet_addr = wallet.address.lower()
        received: list[dict[str, Any]] = []
        spent_quote = False
        chain = legs[0].get("_chain") if legs else "ethereum"
        ts = 0
        for leg in legs:
            ts = max(ts, int(leg.get("_ts") or 0))
            chain = leg.get("_chain") or chain
            raw_contract = leg.get("rawContract") or {}
            contract = _addr(raw_contract.get("address")) or NATIVE_SENTINEL
            category = (leg.get("category") or "").lower()
            if category == "external" and not raw_contract.get("address"):
                contract = NATIVE_SENTINEL
            to_addr = _addr(leg.get("to"))
            from_addr = _addr(leg.get("from"))
            asset = (leg.get("asset") or "").upper()
            if to_addr == wallet_addr:
                received.append(
                    {
                        "contract": contract,
                        "asset": asset,
                        "value": str(leg.get("value") or "0"),
                        "category": category,
                    }
                )
            if from_addr == wallet_addr and (
                contract in quotes or asset in ETH_SYMBOLS | STABLE_SYMBOLS
            ):
                spent_quote = True

        if not spent_quote:
            return None

        candidates = [
            r
            for r in received
            if r["contract"] not in quotes
            and r["asset"] not in ETH_SYMBOLS | STABLE_SYMBOLS
            and r["category"] in {"erc20", ""}
        ]
        if not candidates:
            return None
        chosen = candidates[0]
        if chosen["contract"] in {NATIVE_SENTINEL, ""}:
            return None
        return BuyEvent(
            wallet=wallet_addr,
            trader_label=wallet.label,
            chain=chain,
            asset_type="token",
            contract=chosen["contract"],
            token_id=None,
            amount=chosen["value"],
            price_usd=None,
            tx_hash=tx_hash,
            timestamp=ts,
            symbol=chosen["asset"] or None,
            name=chosen["asset"] or None,
            twitter=wallet.handle,
        )

    async def token_metrics(self, chain: str, contract: str) -> TokenMetrics | None:
        key = (chain.lower(), contract.lower())
        if key in self._metrics_cache:
            return self._metrics_cache[key]
        ds_chain = DEXSCREENER_CHAIN.get(chain, chain)
        url = f"{DEXSCREENER}/tokens/v1/{ds_chain}/{contract}"
        try:
            data = await self.client.get_json(url)
        except Exception as exc:
            log.warning("DexScreener lookup failed %s %s: %s", chain, contract, exc)
            self._metrics_cache[key] = None
            return None

        pairs = data if isinstance(data, list) else (data.get("pairs") or [])
        if not pairs:
            self._metrics_cache[key] = None
            return None

        def liq(pair: dict[str, Any]) -> float:
            return float(((pair.get("liquidity") or {}).get("usd")) or 0)

        best = max(pairs, key=liq)
        base = best.get("baseToken") or {}
        quote = best.get("quoteToken") or {}
        # Prefer the pair where the queried contract is the base token.
        for pair in pairs:
            b = pair.get("baseToken") or {}
            if _addr(b.get("address")) == contract.lower():
                if liq(pair) >= liq(best) * 0.5:
                    best = pair
                    base = b
                    quote = pair.get("quoteToken") or {}
                    break

        info = best.get("info") or {}
        verified = bool(info.get("imageUrl") or (info.get("websites") or info.get("socials")))
        metrics = TokenMetrics(
            symbol=base.get("symbol") or quote.get("symbol") or "?",
            name=base.get("name") or base.get("symbol") or contract,
            price_usd=_to_float(best.get("priceUsd")),
            liquidity_usd=_to_float((best.get("liquidity") or {}).get("usd")),
            market_cap_usd=_to_float(best.get("marketCap") or best.get("fdv")),
            volume_24h_usd=_to_float((best.get("volume") or {}).get("h24")),
            chart_url=best.get("url")
            or f"https://dexscreener.com/{ds_chain}/{contract}",
            pair_address=best.get("pairAddress"),
            verified=verified or liq(best) >= self.settings.min_token_liquidity_usd,
        )
        self._metrics_cache[key] = metrics
        return metrics


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
