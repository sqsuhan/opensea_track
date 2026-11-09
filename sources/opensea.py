"""OpenSea API v2: account events and collection enrichment."""

from __future__ import annotations

import logging
from typing import Any

from config import OPENSEA_CHAIN, Settings
from http_util import RateLimitedClient
from models import BuyEvent, NftMetrics, Wallet

log = logging.getLogger(__name__)

OPENSEA_BASE = "https://api.opensea.io/api/v2"
ZERO = "0x0000000000000000000000000000000000000000"


def _addr(value: str | None) -> str:
    return (value or "").lower()


def _payment_eth(payment: dict[str, Any] | None) -> float | None:
    if not payment:
        return None
    try:
        quantity = int(str(payment.get("quantity") or "0"))
        decimals = int(payment.get("decimals") or 18)
        return quantity / (10**decimals)
    except (TypeError, ValueError):
        return None


def _parse_timestamp(raw: Any) -> int:
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        value = int(raw)
        return value // 1000 if value > 10_000_000_000 else value
    text = str(raw)
    if text.isdigit():
        value = int(text)
        return value // 1000 if value > 10_000_000_000 else value
    from datetime import datetime, timezone

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return int(dt.timestamp())
    except ValueError:
        return 0


class OpenSeaSource:
    def __init__(self, settings: Settings, client: RateLimitedClient) -> None:
        self.settings = settings
        self.client = client

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.settings.opensea_api_key:
            headers["x-api-key"] = self.settings.opensea_api_key
        return headers

    async def fetch_buys(self, wallet: Wallet, after: int) -> list[BuyEvent]:
        events: list[BuyEvent] = []
        for chain in self.settings.chains:
            os_chain = OPENSEA_CHAIN.get(chain, chain)
            events.extend(await self._fetch_chain(wallet, chain, after, os_chain=os_chain))
        events.sort(key=lambda e: e.timestamp)
        return events

    async def _fetch_chain(
        self, wallet: Wallet, chain: str, after: int, *, os_chain: str | None = None
    ) -> list[BuyEvent]:
        out: list[BuyEvent] = []
        next_cursor: str | None = None
        pages = 0
        os_chain = os_chain or OPENSEA_CHAIN.get(chain, chain)
        while pages < 8:
            params: list[tuple[str, Any]] = [
                ("event_type", "sale"),
                ("event_type", "transfer"),
                ("chain", os_chain),
                ("limit", 50),
            ]
            if after:
                params.append(("after", after))
            if next_cursor:
                params.append(("next", next_cursor))
            url = f"{OPENSEA_BASE}/events/accounts/{wallet.address}"
            try:
                data = await self.client.get_json(url, params=params, headers=self._headers())
            except Exception as exc:
                log.warning("OpenSea events failed for %s on %s: %s", wallet.label, chain, exc)
                break

            raw_events = data.get("asset_events") or []
            for raw in raw_events:
                parsed = self._parse_event(wallet, chain, raw)
                if parsed is not None:
                    out.append(parsed)

            next_cursor = data.get("next") or None
            pages += 1
            if not next_cursor or not raw_events:
                break
        return out

    def _parse_event(self, wallet: Wallet, chain: str, raw: dict[str, Any]) -> BuyEvent | None:
        event_type = (raw.get("event_type") or "").lower()
        ts = _parse_timestamp(raw.get("event_timestamp") or raw.get("closing_date"))
        nft = raw.get("nft") or raw.get("asset") or {}
        contract = _addr(nft.get("contract"))
        if not contract:
            return None
        token_id = str(nft.get("identifier") or nft.get("token_id") or "") or None
        tx_hash = (raw.get("transaction") or "").lower()
        if not tx_hash:
            return None
        slug = nft.get("collection")
        name = nft.get("name") or slug
        quantity = str(raw.get("quantity") or "1")

        if event_type == "sale":
            buyer = _addr(raw.get("buyer"))
            seller = _addr(raw.get("seller"))
            if buyer != wallet.address.lower():
                return None
            if seller == wallet.address.lower():
                return None
            eth = _payment_eth(raw.get("payment"))
            if eth is not None and eth < self.settings.min_nft_price_eth:
                log.debug("skip cheap NFT sale %.6f ETH %s", eth, tx_hash)
                return None
            price_usd = None
            if eth is not None:
                estimated = nft.get("estimated_value_usd")
                price_usd = float(estimated) if estimated is not None else None
            return BuyEvent(
                wallet=wallet.address.lower(),
                trader_label=wallet.label,
                chain=chain,
                asset_type="nft",
                contract=contract,
                token_id=token_id,
                amount=quantity,
                price_usd=price_usd,
                tx_hash=tx_hash,
                timestamp=ts,
                collection_slug=slug,
                symbol=None,
                name=name,
                twitter=wallet.handle,
            )

        if event_type == "transfer":
            to_addr = _addr(raw.get("to_address"))
            from_addr = _addr(raw.get("from_address"))
            if to_addr != wallet.address.lower():
                return None
            if from_addr in {ZERO, wallet.address.lower()}:
                return None
            transfer_type = (raw.get("transfer_type") or "").lower()
            if transfer_type in {"airdrop", "mint", "burn"}:
                return None
            payment = raw.get("payment")
            eth = _payment_eth(payment)
            if payment is None or eth is None or eth <= 0:
                return None
            if eth < self.settings.min_nft_price_eth:
                return None
            return BuyEvent(
                wallet=wallet.address.lower(),
                trader_label=wallet.label,
                chain=chain,
                asset_type="nft",
                contract=contract,
                token_id=token_id,
                amount=quantity,
                price_usd=None,
                tx_hash=tx_hash,
                timestamp=ts,
                collection_slug=slug,
                name=name,
                twitter=wallet.handle,
            )
        return None

    async def collection_metrics(self, slug: str) -> NftMetrics | None:
        if not slug:
            return None
        try:
            stats = await self.client.get_json(
                f"{OPENSEA_BASE}/collections/{slug}/stats",
                headers=self._headers(),
            )
            meta = await self.client.get_json(
                f"{OPENSEA_BASE}/collections/{slug}",
                headers=self._headers(),
            )
        except Exception as exc:
            log.warning("OpenSea collection lookup failed for %s: %s", slug, exc)
            return None

        total = stats.get("total") or {}
        volume_24h = None
        volume_symbol = total.get("volume_symbol") or "ETH"
        for interval in stats.get("intervals") or []:
            if interval.get("interval") in {"one_day", "1d", "24h"}:
                volume_24h = interval.get("volume")
                volume_symbol = interval.get("volume_symbol") or volume_symbol
                break
        name = (meta.get("name") or slug)
        return NftMetrics(
            name=name,
            slug=slug,
            floor_price=_to_float(total.get("floor_price")),
            floor_symbol=total.get("floor_price_symbol") or "ETH",
            volume_24h=_to_float(volume_24h),
            volume_symbol=volume_symbol,
            opensea_url=f"https://opensea.io/collection/{slug}",
        )

    async def collection_by_contract(self, chain: str, contract: str) -> NftMetrics | None:
        try:
            data = await self.client.get_json(
                f"{OPENSEA_BASE}/chain/{chain}/contract/{contract}",
                headers=self._headers(),
            )
        except Exception as exc:
            log.debug("contract lookup failed %s %s: %s", chain, contract, exc)
            return None
        collection = data.get("collection") or ""
        if isinstance(collection, dict):
            collection = collection.get("slug") or collection.get("collection") or ""
        if not collection:
            return None
        return await self.collection_metrics(collection)


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
