"""Replay sample events through the engine without network or Telegram."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import ALCHEMY_NETWORKS, EXPLORERS, Settings, normalize_chains  # noqa: E402
from db import Database  # noqa: E402
from engine import Engine  # noqa: E402
from http_util import RateLimitedClient  # noqa: E402
from models import BuyEvent, TokenMetrics, Wallet, strength_emoji  # noqa: E402
from notifier import Notifier, render_signal  # noqa: E402
from sources.opensea import OpenSeaSource  # noqa: E402
from sources.tokens import TokenSource  # noqa: E402


SAMPLE = ROOT / "tests" / "sample_events.json"


def _settings(db_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="",
        telegram_channel_id="",
        telegram_admin_ids=set(),
        opensea_api_key="",
        alchemy_api_key="",
        db_path=db_path,
        wallets_path=ROOT / "wallets.json",
    )


class StrengthTests(unittest.TestCase):
    def test_strength_ladder(self) -> None:
        self.assertEqual(strength_emoji(2), "🟡")
        self.assertEqual(strength_emoji(3), "🟠")
        self.assertEqual(strength_emoji(4), "🔴")
        self.assertEqual(strength_emoji(9), "🔴")


class ChainConfigTests(unittest.TestCase):
    def test_aliases_and_new_chains(self) -> None:
        self.assertEqual(
            normalize_chains(["ethereum", "robhinhood", "arc mainnet".replace(" ", "-"), "ink"]),
            ["ethereum", "robinhood", "arc", "ink"],
        )
        for chain in ("robinhood", "arc", "ink"):
            self.assertIn(chain, ALCHEMY_NETWORKS)
            self.assertIn(chain, EXPLORERS)
            self.assertIn("{tx}", EXPLORERS[chain])


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self.settings = _settings(self.db_path)
        self.db = Database(self.db_path)
        await self.db.connect()
        self.client = RateLimitedClient()
        self.engine = Engine(
            self.settings,
            self.db,
            OpenSeaSource(self.settings, self.client),
            TokenSource(self.settings, self.client),
            Notifier(self.settings, application=None),
        )
        payload = json.loads(SAMPLE.read_text(encoding="utf-8"))
        self.payload = payload
        for item in payload["wallets"]:
            await self.db.add_wallet(
                Wallet(
                    label=item["label"],
                    twitter=item.get("twitter") or "",
                    address=item["wallet"].lower(),
                )
            )
        fake = payload.get("token_metrics") or {}

        async def fake_token_metrics(chain: str, contract: str) -> TokenMetrics | None:
            raw = fake.get(f"{chain.lower()}:{contract.lower()}")
            if not raw:
                return None
            return TokenMetrics(
                symbol=raw.get("symbol") or "?",
                name=raw.get("name") or "?",
                price_usd=raw.get("price_usd"),
                liquidity_usd=raw.get("liquidity_usd"),
                market_cap_usd=raw.get("market_cap_usd"),
                volume_24h_usd=raw.get("volume_24h_usd"),
                chart_url=raw.get("chart_url"),
                verified=bool(raw.get("verified", True)),
            )

        async def none_metrics(*_args, **_kwargs):
            return None

        self.engine.tokens.token_metrics = fake_token_metrics  # type: ignore[method-assign]
        self.engine.opensea.collection_metrics = none_metrics  # type: ignore[method-assign]
        self.engine.opensea.collection_by_contract = none_metrics  # type: ignore[method-assign]

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.db.close()
        self.tmp.cleanup()

    async def test_replay_clusters_and_filters_spam(self) -> None:
        now = int(time.time())
        ingested = 0
        for raw in self.payload["events"]:
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
                timestamp=now - int(raw.get("seconds_ago") or 0),
                collection_slug=raw.get("collection_slug"),
                symbol=raw.get("symbol"),
                name=raw.get("name"),
                twitter=raw.get("twitter"),
            )
            if await self.engine.ingest(event):
                ingested += 1

        self.assertEqual(ingested, 7)  # 9 events, 2 spam filtered
        printed = self.engine.notifier.printed
        self.assertGreaterEqual(len(printed), 3)
        self.assertTrue(any("SEND" in p and "Bored Ape" in p for p in printed))
        self.assertTrue(any("EDIT" in p and "🔴" in p for p in printed))
        self.assertTrue(any("DEGEN" in p or "Degen" in p for p in printed))
        self.assertFalse(any("SPAM" in p for p in printed))
        signals = await self.db.last_signals(10)
        names = {row["name"] for row in signals}
        self.assertIn("Bored Ape Yacht Club", names)
        self.assertNotIn("SpamToken", names)

    async def test_duplicate_tx_is_ignored(self) -> None:
        now = int(time.time())
        event = BuyEvent(
            wallet="0x325f3e52bb8529352091a268f60750ba5a978cc4",
            trader_label="AFinDubai",
            chain="ethereum",
            asset_type="nft",
            contract="0xbc4ca0eda7647a8ab7c2061c2e118a18a936f13d",
            token_id="1",
            amount="1",
            price_usd=100.0,
            tx_hash="0xabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabcabca",
            timestamp=now,
            name="Bored Ape Yacht Club",
            twitter="@AFinDubai",
        )
        self.assertTrue(await self.engine.ingest(event))
        self.assertFalse(await self.engine.ingest(event))


class RenderTests(unittest.TestCase):
    def test_render_contains_footer(self) -> None:
        from models import SignalSnapshot

        snap = SignalSnapshot(
            chain="ethereum",
            contract="0xabc",
            asset_type="nft",
            name="Bored Ape Yacht Club",
            traders=[],
            strength="🟡",
            trader_count=2,
            floor_or_price="14 ETH",
            volume_or_liq="100 ETH",
            opensea_url="https://opensea.io/collection/boredapeyachtclub",
            explorer_url="https://etherscan.io/tx/0x1",
            chart_url=None,
        )
        text = render_signal(snap, int(time.time()))
        self.assertIn("Not financial advice", text)
        self.assertIn("SIGNAL", text)


if __name__ == "__main__":
    unittest.main()
