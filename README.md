# OpenSea $1K → $10K trader signal bot

Read-only Telegram bot that watches the public wallets of OpenSea’s **$1K to $10K challenge** traders. When two or more of those wallets buy the same NFT collection or the same token inside a rolling 24-hour window, it posts a cluster signal to your Telegram channel.

The bot never asks for private keys and never sends transactions.

## What it does

- Polls each tracked wallet every 60 seconds (configurable).
- **NFTs:** OpenSea API v2 account events (`sale`, `transfer`). A buy is counted when the tracked wallet is the buyer on a sale, or the paid recipient of a transfer. Sells, mints, airdrops, and NFT buys under `0.001 ETH` are skipped.
- **Tokens:** Alchemy `alchemy_getAssetTransfers` on Ethereum, Base, Robinhood Chain, Arc, and Ink. A buy is counted when the wallet receives an ERC-20 in the same transaction that it spent ETH, WETH, or a stablecoin (USDC / USDG on Robinhood and Arc).
- Groups buys by `chain + contract`. Fires at 2 traders, upgrades 3 → 🟠 and 4+ → 🔴 by editing the original Telegram message.
- Enriches NFTs with OpenSea collection floor / 24h volume, and tokens with DexScreener price / liquidity / market cap. Tokens under `$50k` liquidity are skipped.
- SQLite cursors (`wallets`, `events`, `signals`) so a restart does not double-count.

## Project layout

```
main.py              # entrypoint, admin commands, --test replay
config.py            # env settings, quote tokens, explorers
db.py                # aiosqlite schema and queries
engine.py            # poll loop, filters, clustering
notifier.py          # Telegram formatting and delivery
http_util.py         # httpx + exponential backoff on 429/5xx
models.py            # BuyEvent / Wallet / SignalSnapshot
sources/opensea.py   # OpenSea events + collection stats
sources/tokens.py    # Alchemy transfers + DexScreener
wallets.json         # the 10 tracked traders
.env.example         # required secrets
```

## 1. Create the Telegram bot

1. Open Telegram and chat with [@BotFather](https://t.me/BotFather).
2. Send `/newbot`, pick a name and username.
3. Copy the bot token (`123456:ABC...`).
4. Optional: `/setprivacy` → Disable, so the bot can read DMs with commands.

## 2. Add the bot to your channel as admin

1. Create a public or private channel.
2. Add the bot as an administrator with **Post messages** and **Edit messages**.
3. Get the channel ID:
   - Forward a channel post to [@userinfobot](https://t.me/userinfobot) or [@getidsbot](https://t.me/getidsbot), **or**
   - Post anything in the channel, then open  
     `https://api.telegram.org/bot<TOKEN>/getUpdates`  
     and read `channel_post.chat.id` (looks like `-100xxxxxxxxxx`).
4. DM the bot from your own account, then call `getUpdates` again to read your numeric user ID for `TELEGRAM_ADMIN_IDS`.

## 3. Get API keys

**OpenSea**

1. Go to [OpenSea API docs](https://docs.opensea.io/reference/list_events_by_account).
2. Create a key (free-tier keys can be minted via `POST https://api.opensea.io/api/v2/auth/keys`).
3. Put it in `OPENSEA_API_KEY`.

**Alchemy**

1. Create an app at [dashboard.alchemy.com](https://dashboard.alchemy.com).
2. In the app **Networks** tab, enable **Ethereum Mainnet**, **Base Mainnet**, **Robinhood Mainnet**, **Arc Mainnet**, and **Ink Mainnet**. One key covers all of them.
3. Copy the API key into `ALCHEMY_API_KEY`.

DexScreener is public and needs no key.

## 4. Configure

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

```
TELEGRAM_BOT_TOKEN=123456:ABC-your-bot-token
TELEGRAM_CHANNEL_ID=-100xxxxxxxxxx
TELEGRAM_ADMIN_IDS=your_numeric_user_id
OPENSEA_API_KEY=os_live_...
ALCHEMY_API_KEY=...
CHAINS=ethereum,base,robinhood,arc,ink
SIGNAL_THRESHOLD=2
SIGNAL_WINDOW_HOURS=24
POLL_INTERVAL_SECONDS=60
```

`wallets.json` already lists 10 publicly associated challenge-trader wallets (label, X handle, address). Challenge-funded wallets can differ from a trader’s long-term profile. Replace any address with the dedicated OpenSea Mobile wallet once you have it. Never put a private key in this file.

## 5. Test without Telegram

```bash
python main.py --test
```

This replays `tests/sample_events.json`, runs the same filters and clustering, and prints the alerts it would send. Expected:

- Bored Ape Yacht Club: 🟡 at 2 traders, then edited to 🟠 and 🔴 as more join.
- DEGEN on Base: 🟡 at 2 traders.
- `SPAM` is dropped (liquidity under $50k).
- A lone Doodles buy does not fire.

## 6. Run

```bash
python main.py
```

Admin-only commands (DM the bot; your user ID must be in `TELEGRAM_ADMIN_IDS`):

| Command | Action |
| --- | --- |
| `/traders` | List tracked wallets |
| `/add <label> <wallet> [@handle]` | Track another wallet |
| `/remove <label>` | Stop tracking |
| `/threshold <n>` | Distinct traders required (min 2) |
| `/window <hours>` | Rolling window |
| `/signals` | Last 10 alerts |
| `/status` | Health, last poll, last error |

## 7. Deploy

### Railway

1. Push this repo to GitHub.
2. New Railway project → Deploy from GitHub.
3. Add every variable from `.env.example` in Railway Variables.
4. Persistent volume (optional) mounted at `/app/data` so SQLite survives deploys.
5. Start command: `python main.py`.

### Small VPS (Docker)

```bash
docker build -t opensea-track .
docker run -d --name opensea-track --restart unless-stopped \
  --env-file .env \
  -v "$(pwd)/data:/app/data" \
  opensea-track
```

Without Docker:

```bash
sudo apt install python3.11-venv
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit
python main.py
```

A systemd unit can wrap `python /opt/opensea_track/main.py` with `Restart=always`.

## Signal format

```
🔴 SIGNAL: 3 traders bought Bored Ape Yacht Club
Chain: ethereum | Type: NFT
Traders:
• @trader1 bought at $14.2k (12m ago)
• @trader2 bought at $13.9k (2h ago)
Floor: 14.200 ETH | 24h Vol: 320.50 ETH
[ OpenSea ] [ Explorer ] [ Chart ]
Not financial advice. DYOR.
```

## Rules

- Read-only. No private keys, no trade execution, no approvals.
- Nothing in this repo is financial advice.
- Rate limits: every outbound HTTP call retries on 429 / 5xx with exponential backoff.
- Cursors in SQLite keep each wallet’s last processed event so a crash does not duplicate alerts.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Bot starts then exits | `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHANNEL_ID` missing |
| NFT polls fail | Check `OPENSEA_API_KEY`; 401 means a bad key |
| No token signals | Enable Ethereum, Base, Robinhood, Arc, and Ink on the Alchemy app |
| Commands ignored | DM the bot; set `TELEGRAM_ADMIN_IDS` to your user ID |
| Channel silent | Bot must be a channel admin with post + edit rights |
