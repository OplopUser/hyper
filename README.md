# Hyperliquid Trading Bot

[![Build & Push ARM64 Image](https://github.com/OplopUser/hyper/actions/workflows/build-and-push.yml/badge.svg)](https://github.com/OplopUser/hyper/actions/workflows/build-and-push.yml)

A Python bot that trades [Hyperliquid](https://hyperliquid.xyz) perpetuals using an EMA crossover with RSI, higher-timeframe trend and volatility filters, ATR-based stops and a small risk-management layer. It ships with a backtester, a Flask monitoring dashboard and an ARM64 Docker image for running on a Raspberry Pi.

> **Experimental software that can trade real funds.** Run it on testnet first. Nothing here is financial advice. See [Risks & limitations](#risks--limitations).

---

## Contents

- [Overview](#overview)
- [Features](#features)
- [Strategy](#strategy)
- [Risk management](#risk-management)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Setup](#setup)
- [Usage](#usage)
- [Docker / Raspberry Pi deployment](#docker--raspberry-pi-deployment)
- [Project structure](#project-structure)
- [Risks & limitations](#risks--limitations)
- [License](#license)

---

## Overview

The bot runs a polling loop (every 60 seconds by default). On each iteration it reads account equity (if that fails it backs off — 5 s, 10 s, 20 s … capped at 5 minutes — and resets after the next success), then for every configured symbol it:

1. fetches the mid-price, adopts any position open on the exchange that it isn't tracking yet (see [Restarts](#restarts-and-open-positions)), and checks the open position against its stop-loss / take-profit,
2. runs pre-trade risk checks,
3. pulls 15m candles plus 4h candles from the Hyperliquid Info API,
4. evaluates the strategy on the last **closed** 15m candle,
5. if a signal passes every filter, sizes the position and sends a market order through the official [`hyperliquid-python-sdk`](https://github.com/hyperliquid-dex/hyperliquid-python-sdk).

All trading goes through an API (agent) wallet that signs on behalf of your main account, so the main account's private key never has to sit on the machine running the bot.

## Features

- **Strategy:** EMA(9/21) crossover, RSI(14) range filter, 4h EMA(21) trend filter, ATR-percentile volatility filter
- **ATR-based exits:** stop-loss at 1.5 × ATR, take-profit at 3.0 × ATR
- **Risk controls:** fixed-fractional position sizing (loss at the stop = 2 % of equity at any leverage), free-margin cap, max concurrent positions, one position per coin, minimum order size, drawdown halt
- **Multi-symbol:** trades every coin listed in `SYMBOLS`
- **Signal de-duplication:** a closed candle can only produce one signal per symbol, so 60-second polling does not re-fire the same crossover
- **Restart-safe:** `SIGINT` / `SIGTERM` finish the current iteration and exit, leaving positions open; the next start re-adopts them (set `CLOSE_ON_SHUTDOWN=true` to flatten on exit instead)
- **Backtester:** replays the same `compute_signal()` and sizing used live on closed candles only, fills at the next candle's open, charges taker fees, and writes `backtest_results.json`
- **Dashboard:** Flask web UI with equity, open positions, fills, PnL stats, backtest results and a live log tail
- **Structured logging:** `[SIGNAL]`, `[ORDER]`, `[POSITION]` and `[RISK]` events to the console and a rotating log file
- **Deployment:** Dockerfile, `docker-compose.yml` (bot and dashboard as separate services) and a GitHub Actions workflow that builds and pushes a `linux/arm64` image to GHCR
- **Historical data module:** an S3 client for [Hydromancer Reservoir](https://hydromancer.xyz) (see [Historical data](#historical-data-hydromancer-reservoir))

## Strategy

Defined in [`bot/strategy.py`](bot/strategy.py). Parameters come from `StrategyConfig` in [`config.py`](config.py).

### Indicators

| Indicator | Default | Env override |
|---|---|---|
| Fast EMA (close) | 9 | `EMA_FAST` |
| Slow EMA (close) | 21 | `EMA_SLOW` |
| RSI | 14 | `RSI_PERIOD` |
| ATR | 14 | `ATR_PERIOD` |
| Primary timeframe | `15m`, 200 candles | `config.py` only |
| Higher timeframe (HTF) | `4h`, 100 candles, EMA(21) | `config.py` only |

At least 50 candles are needed before any signal is produced. The strategy looks at the last closed candle (`iloc[-2]`) and the one before it (`iloc[-3]`) and ignores the candle that is still forming.

### Entry rules

| Signal | Conditions (all must hold) |
|---|---|
| **Long** | Fast EMA crosses **above** slow EMA · 30 < RSI < 70 · 4h trend is not bearish · volatility regime is normal |
| **Short** | Fast EMA crosses **below** slow EMA · 30 < RSI < 70 · 4h trend is not bullish · volatility regime is normal |
| **None** | No crossover, or any filter blocks |

**Filters**

- **RSI range:** the signal is dropped when RSI is at or below 30 or at or above 70 (`rsi_oversold` / `rsi_overbought`).
- **HTF trend** (`htf_enabled = True`): the 4h close compared with the 4h EMA(21) gives `bullish` or `bearish`. Longs are blocked in a bearish trend and shorts in a bullish one. If the trend is `unknown` (not enough 4h data), the filter does not block.
- **Volatility regime** (`vol_filter_enabled = True`): the ATR of the last closed candle is ranked against the last 100 ATR values. Below the 20th percentile is `low` (ranging) and above the 95th is `high` (news/chaos); both block the signal.

### Stop-loss and take-profit

Computed from the signal candle's close and ATR:

```
Long:   stop-loss = close − 1.5 × ATR     take-profit = close + 3.0 × ATR
Short:  stop-loss = close + 1.5 × ATR     take-profit = close − 3.0 × ATR
```

That is a 2 : 1 reward-to-risk ratio measured from the candle close. The order fills near the current mid-price, so the realised ratio differs slightly. Before ordering, the bot checks that the levels are on the correct side of the entry price (`SL < entry < TP` for longs, `TP < entry < SL` for shorts).

### Exits

In live trading a position is closed with a reduce-only market order when, on a loop iteration, the mid-price has reached the stop-loss or take-profit. The bot also stops tracking a position that is no longer open on the exchange (closed manually or liquidated). **Stops are enforced by the bot's polling loop, not by trigger orders resting on the exchange.**

### Restarts and open positions

By default (`CLOSE_ON_SHUTDOWN=false`) stopping or restarting the bot — including `docker compose restart` and Watchtower image updates — leaves open positions on the exchange. On each iteration, for every coin in `SYMBOLS`, the bot compares its tracked position with the exchange:

- a position open on the exchange but not tracked (left by a restart, or opened by hand) is **adopted**: it counts toward `MAX_POSITIONS`, blocks a second entry on that coin, and gets a stop-loss / take-profit rebuilt from the exchange entry price and the ATR of the last closed candle (same 1.5× / 3.0× multipliers). Because the ATR is today's rather than the one at the original entry, the levels can differ from the ones the position was opened with; if price is already past one of them, the position is closed on that iteration;
- if the positions query fails, or the SL/TP can't be computed (no candles), the bot opens nothing on that coin that iteration and tries again on the next.

Positions on coins outside `SYMBOLS` are ignored. While the bot is down, nothing enforces the stops.

## Risk management

Implemented in [`bot/risk.py`](bot/risk.py). Defaults come from `RiskConfig` in [`config.py`](config.py).

| Control | Default | Env override | Behaviour |
|---|---|---|---|
| Risk per trade | 2 % of equity | `MAX_RISK_PER_TRADE` | Drives position size (below) |
| Max concurrent positions | 3 | `MAX_POSITIONS` | New entries blocked at the limit |
| One position per coin | always on | — | No pyramiding or adding to a position |
| Drawdown halt | 10 % from peak | `config.py` only | Blocks new entries once equity falls 10 % below the peak seen since start-up. Open positions keep being managed. The halt lasts until the bot restarts. |
| Leverage | 1× (cross) | `config.py` only | Set explicitly on every symbol at start-up. Changes margin use, not risk (below) |
| Minimum order | $10 notional | `config.py` only | Smaller orders are skipped |

**Position sizing**

```
risk_amount = equity × MAX_RISK_PER_TRADE
size        = risk_amount / |entry − stop_loss|
size        = min(size, free_margin × leverage × 0.95 / entry)   # margin cap, 5 % buffer
size        = floor(size, exchange size decimals)
```

The size is chosen so that a fill at the stop loses `MAX_RISK_PER_TRADE` of equity (2 % by default). **Leverage does not scale it**: the loss at the stop is `size × |entry − stop|`, whatever the leverage. Leverage only sets how much margin the position ties up (`notional / leverage`), so its one effect on sizing is to raise the cap.

`free_margin` is the account's `withdrawable` balance (equity not already used as margin by other positions); if it can't be fetched, the bot skips the entry. With tight stops the cap can bind, and the trade then risks **less** than 2 %. Example: $1,000 equity, ETH at $2,000, stop 0.5 % away ($10). The risk budget is $20, so the uncapped size is 2 ETH ($4,000 notional). At 1× the cap is $950 notional (0.475 ETH, $4.75 at risk); at 5× the cap is $4,750 and the full 2 ETH ($20 at risk) fits. Fees and slippage past the stop come on top.

## Architecture

```mermaid
flowchart LR
    subgraph Bot["main.py loop (every LOOP_INTERVAL s)"]
        M[data/market.py<br/>MarketData] --> S[bot/strategy.py<br/>compute_signal]
        S --> R[bot/risk.py<br/>RiskManager]
        R --> E[bot/exchange.py<br/>ExchangeClient]
    end
    HL[(Hyperliquid API<br/>testnet or mainnet)]
    M -- Info API: mids, candles, meta --> HL
    E -- signed orders via API wallet --> HL
    Bot -- logs/bot.log --> D[dashboard.py<br/>Flask :5050]
    BT[bot/backtest.py] -- reuses --> S
    BT -- backtest_results.json --> D
    D -- Info API: account, fills --> HL
```

| Module | Responsibility |
|---|---|
| `main.py` | Entry point: config validation, leverage setup, main loop, signal handling, shutdown |
| `config.py` | Reads `.env`; holds `StrategyConfig`, `RiskConfig`, network and loop settings |
| `bot/strategy.py` | Indicators (EMA, RSI, ATR), crossover detection, HTF and volatility filters, SL/TP |
| `bot/risk.py` | Position sizing, pre-trade checks, SL/TP validation, drawdown halt, in-memory position records |
| `bot/exchange.py` | Wrapper around the SDK `Exchange`: market/limit orders, cancels, close, leverage, account queries |
| `bot/backtest.py` | Bar-by-bar backtester and CLI |
| `data/market.py` | Hyperliquid Info API: mid-prices, candles, L2 book, metadata, funding |
| `data/reservoir.py` | Hydromancer Reservoir S3 reader (candles, fills, snapshots) |
| `utils/logger.py` | Console and rotating-file logging, structured trade-event helpers |
| `utils/backoff.py` | Capped exponential back-off used by the main loop |
| `dashboard.py` | Flask dashboard and JSON API (`/api/all`, `/api/account`, `/api/fills`, `/api/logs`) |

## Requirements

- Python 3.9 or newer (the Docker image uses Python 3.11)
- A Hyperliquid account, plus an **API wallet** created for it (start on testnet)
- Docker, for container deployment (optional)
- AWS credentials, only if you use `data/reservoir.py` (optional)

Python dependencies are listed in [`requirements.txt`](requirements.txt): `hyperliquid-python-sdk`, `pandas`, `numpy`, `ta`, `flask`, `python-dotenv`, `requests`, `boto3`, `pyarrow`.

## Setup

```bash
git clone https://github.com/OplopUser/hyper.git
cd hyper
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### 1. Create an API wallet (recommended)

In the Hyperliquid app (testnet: <https://app.hyperliquid-testnet.xyz>), generate an **API wallet** (agent) for your account. The bot signs orders with the API wallet's key and acts for your main account (`ACCOUNT_ADDRESS`). An API wallet can trade but cannot withdraw, so it limits the damage if the key on the bot machine leaks. Fund the main account; testnet funds are available from the testnet app.

### 2. Fill in `.env`

| Variable | Required | Default | Description |
|---|---|---|---|
| `PRIVATE_KEY` | yes | — | Private key of the **API wallet** that signs orders |
| `API_WALLET_ADDRESS` | yes | — | Address of that API wallet (checked at start-up) |
| `ACCOUNT_ADDRESS` | yes | — | Main account address that holds the funds; used for equity, position and fill queries (bot and dashboard) |
| `USE_TESTNET` | no | `true` | `true` uses `api.hyperliquid-testnet.xyz`; `false` uses mainnet |
| `SYMBOLS` | no | `ETH,SOL` | Comma-separated coins to trade |
| `SYMBOL` | no | — | Legacy single-symbol setting, read only when `SYMBOLS` is unset |
| `EMA_FAST` / `EMA_SLOW` | no | `9` / `21` | EMA periods |
| `RSI_PERIOD` | no | `14` | RSI period |
| `ATR_PERIOD` | no | `14` | ATR period |
| `MAX_RISK_PER_TRADE` | no | `0.02` | Fraction of equity risked per trade |
| `MAX_POSITIONS` | no | `3` | Max concurrent positions |
| `LOOP_INTERVAL` | no | `60` | Seconds between loop iterations |
| `CLOSE_ON_SHUTDOWN` | no | `false` | `true` closes every tracked position on `SIGINT`/`SIGTERM` (old behaviour). `false` leaves them open for the next start to adopt |
| `DASHBOARD_PORT` | no | `5050` | Dashboard port (`python dashboard.py` only; the compose file maps 5050) |
| `DASHBOARD_HOST` | no | `0.0.0.0` | Dashboard bind address; `127.0.0.1` for local-only |
| `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` | no | — | Used only by `data/reservoir.py`; falls back to the default AWS credential chain |
| `AWS_DEFAULT_REGION` | no | `us-east-1` | Region for the Reservoir S3 client |

All other parameters (RSI bounds, ATR multipliers, timeframes, filter settings, drawdown halt, leverage, minimum order, backtest fee) are edited directly in `config.py`.

`.env` is listed in `.gitignore` and `.dockerignore`. Never commit it.

## Usage

### Live trading

```bash
python main.py
```

It logs the active configuration, sets leverage on each symbol and starts the loop. Stop it with `Ctrl+C` (or `SIGTERM`): the bot finishes the current iteration and exits, **leaving open positions in place** for the next start to adopt. With `CLOSE_ON_SHUTDOWN=true` it closes them first.

```bash
python main.py --backtest   # run the backtester first, then start live trading
```

### Backtesting

```bash
python -m bot.backtest                          # SYMBOLS, last 30 days
python -m bot.backtest ETH SOL --days 14        # specific symbols and window
python -m bot.backtest --taker-fee 0.0003       # your fee tier (default 0.00045)
python -m bot.backtest --equity 5000 --no-save  # starting equity; don't write JSON
USE_TESTNET=false python -m bot.backtest        # mainnet candles (public, no keys)
```

The backtester needs no wallet keys; it only reads the public candle endpoint of the network selected by `USE_TESTNET`. Testnet prices are thin and often unlike mainnet, so use `USE_TESTNET=false` for meaningful numbers. It downloads the last `--days` days of closed 15m and 4h candles plus indicator warm-up, then, bar by bar with $1,000 starting equity (`--equity`):

- **decides from closed candles only.** At bar *i* it calls the live `compute_signal()` with bars up to *i − 1*; the 4h trend filter sees only 4h candles that had closed by bar *i*'s open, plus a forming 4h candle priced at that open (what the live bot sees when it polls),
- **enters at bar *i*'s open**, with the signal's SL/TP and the same `RiskManager` sizing as live trading (so `MAX_RISK_PER_TRADE`, the leverage cap, size decimals and the minimum order all apply),
- exits, like the live bot, only on SL/TP, checked against each bar's high/low including the entry bar (if both are touched in one bar the stop is assumed first; a bar that opens past a level fills at the open), or at the last close. While a position is open no new signal is evaluated for that coin, so an opposite crossover neither closes nor reverses it,
- charges the **taker fee** (`--taker-fee`, default `BACKTEST_TAKER_FEE` = 0.045 %, Hyperliquid's base perps tier) on the notional of every entry and exit; reported PnL is net of fees,
- prints trades, win rate, net PnL, fees, max drawdown (mark-to-market), Sharpe ratio (annualised for the candle interval), average win/loss and profit factor,
- saves everything to `backtest_results.json`, which the dashboard displays.

Hyperliquid's candle endpoint returns at most the 5,000 most recent candles per interval (about 52 days of 15m candles), so a longer `--days` is cut short; the printed period shows what was actually tested.

See [Risks & limitations](#risks--limitations) for what the simulation leaves out.

### Dashboard

```bash
python dashboard.py
```

Serves on port 5050 (`DASHBOARD_PORT`) on all interfaces (`DASHBOARD_HOST`, default `0.0.0.0` for Docker; set `127.0.0.1` to keep it local) and refreshes every 5 seconds. It shows account equity, unrealised PnL, margin used, withdrawable balance, open positions with mark and liquidation prices, the last fills with closed PnL and fees, win-rate stats, a cumulative-PnL sparkline, backtest results and the tail of `logs/bot.log`. A badge shows whether it is pointed at **TESTNET** or **MAINNET**. It is read-only and needs only `ACCOUNT_ADDRESS`, not the private key. Without `ACCOUNT_ADDRESS` it still starts and shows backtest results and logs, with the account panels empty.

### Logs

- Console: INFO and above, coloured when attached to a TTY
- File: `logs/bot.log`, DEBUG and above, rotated at midnight UTC, 7 files kept

```
2026-03-28 12:00:00  INFO      trade  [SIGNAL] symbol=ETH direction=LONG fast_ema=... slow_ema=... rsi=... atr=...
2026-03-28 12:00:01  INFO      trade  [ORDER] action=SUBMIT symbol=ETH side=buy size=... price=MARKET order_id=n/a
```

### Historical data (Hydromancer Reservoir)

[`data/reservoir.py`](data/reservoir.py) reads Parquet files from the requester-pays bucket `s3://hyperliquid-reservoir` (your AWS account pays the S3 request costs):

| Function | S3 layout | Notes |
|---|---|---|
| `get_candles(coin, days, resample_to, fallback_fn)` | `candles/<COIN>/YYYY-MM-DD.parquet` | 1-second OHLCV, optional resampling (e.g. `"15min"`), falls back to `fallback_fn` if S3 is unavailable |
| `get_fills(days, coin)` | `fills/YYYY-MM-DD.parquet` | Trades, liquidations, ADLs |
| `get_snapshots(days)` | `snapshots/YYYY-MM-DD.parquet` | Daily position and balance snapshots |

The live bot and the backtester do **not** call this module at the moment; both use the Hyperliquid candle endpoint. It is there for longer-horizon research and backtests.

## Docker / Raspberry Pi deployment

The [`Dockerfile`](Dockerfile) builds a `python:3.11-slim-bookworm` image that contains the bot and the dashboard (port 5050). By default it runs `python main.py`.

### Pre-built ARM64 image (CI)

[`.github/workflows/build-and-push.yml`](.github/workflows/build-and-push.yml) runs on every push to `main`. It builds a `linux/arm64` image with QEMU/Buildx and pushes it to GitHub Container Registry as `ghcr.io/<owner>/<repo>:latest` and `:<commit-sha>`.

### Run with Docker Compose

[`docker-compose.yml`](docker-compose.yml) starts two containers from the same image, both reading `.env`:

| Service | Command | Notes |
|---|---|---|
| `bot` | `python main.py` | `restart: unless-stopped`, writes `./logs` |
| `dashboard` | `python dashboard.py` | Publishes port `5050`, reads `./logs` |

On the Pi (or any ARM64 host):

```bash
cp .env.example .env    # then fill it in
docker compose pull
docker compose up -d
docker compose logs -f bot
```

The compose file references the maintainer's GHCR image. If you fork the repo, change `image:` to your own `ghcr.io/<you>/hyper:latest`, or build locally:

```bash
docker build -t hyper .
```

and set `image: hyper` for both services. A plain `docker build` targets the host's architecture.

Notes:

- `docker compose stop`, `restart`, or an image update sends `SIGTERM` to the bot. By default open positions stay open and the new container adopts them; set `CLOSE_ON_SHUTDOWN=true` to close them on the way out instead.
- `backtest_results.json` is not mounted into the containers, so the dashboard's backtest panel stays empty under Compose unless you add a volume for it.
- The dashboard has no authentication and listens on all interfaces. Keep port 5050 on a trusted network or behind a reverse proxy with auth.

[`watchdog.sh`](watchdog.sh) is a non-Docker alternative: it starts `main.py` and `dashboard.py` and restarts either one if it dies (checked every 30 seconds). It uses `$PYTHON` if set, otherwise `.venv/` or `venv/` next to the script, otherwise `python3` from `PATH`:

```bash
nohup ./watchdog.sh &                          # auto-detect the interpreter
PYTHON=/path/to/python nohup ./watchdog.sh &   # or pick one
```

## Project structure

```
hyper/
├── main.py                        # Entry point and main loop
├── config.py                      # Configuration (env vars + dataclass defaults)
├── dashboard.py                   # Flask monitoring dashboard
├── bot/
│   ├── strategy.py                # EMA/RSI/ATR signal + HTF and volatility filters
│   ├── risk.py                    # Sizing, drawdown halt, SL/TP checks
│   ├── exchange.py                # Order and account wrapper around the SDK
│   └── backtest.py                # Backtester + CLI
├── data/
│   ├── market.py                  # Hyperliquid Info API (live data)
│   └── reservoir.py               # Hydromancer Reservoir S3 reader
├── utils/
│   ├── logger.py                  # Logging setup and trade-event helpers
│   └── backoff.py                 # Capped exponential back-off for API failures
├── tests/                         # Offline unit tests (python -m unittest)
├── strategy_diagram.html          # Visual explainer of the strategy
├── trade_activation_diagram.html  # Visual flow from loop tick to order
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── watchdog.sh
└── .github/workflows/build-and-push.yml
```

The two HTML diagrams open directly in a browser. They show the default parameters from `config.py`; if you change those, the code is authoritative.

## Risks & limitations

**This bot can lose real money.** It is a personal, experimental project and comes with no warranty. Nothing in this repository is financial advice. Run it on testnet and with amounts you can afford to lose.

Known limitations:

- **No exchange-side stops.** SL/TP are checked against the mid-price once per loop. Between checks, or while the bot is down, positions are unprotected, and gaps can fill well past the stop.
- **State lives in memory.** Peak equity and the drawdown halt reset on restart. Open positions are re-adopted, but their SL/TP are rebuilt from the current ATR, not the original signal's.
- **No exit on opposite signal.** Positions close only via SL/TP (live and in the backtest); an opposite crossover while a position is open is ignored.
- **Backtest is a rough check, not evidence of an edge.** It avoids look-ahead and charges taker fees, but it ignores slippage, funding and liquidity, assumes stops fill exactly at the level (or the bar's open on a gap), counts a bar that touches both SL and TP as a loss, and runs each symbol on its own (no shared equity or `MAX_POSITIONS` limit). Live stops are checked once per loop against the mid-price, so live fills differ. A month of 15m data is a small sample.
- **Polling, not streaming.** The bot reacts on a 60-second cycle, not to tick data.
- **Limited tests.** `tests/` covers the back-off, position adoption, position sizing and the backtester (look-ahead, fees, `--days`) on synthetic data, offline (`python -m unittest discover -s tests -t .`). Exchange calls are not tested against a live API, and CI only builds the Docker image.
- **Testnet ≠ mainnet.** Liquidity, slippage and fills differ.

## License

No license file yet — all rights reserved.
