# Hyperliquid Trading Bot

An algorithmic trading bot for [Hyperliquid](https://hyperliquid.xyz) perpetuals, using:

- **[hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)** for all trading operations
- **[Hydromancer Reservoir](https://hydromancer.xyz)** (S3) for historical candle / fill / snapshot data
- **EMA crossover + RSI filter** strategy with ATR-based stop-loss / take-profit
- **Structured logging**, drawdown halting, and position-size risk management

---

## Project Structure

```
hyper/
├── main.py              # Bot entry point & 60s loop
├── config.py            # All configuration (reads from .env)
├── requirements.txt     # Python dependencies
├── .env.example         # Template for secrets — copy to .env
├── data/
│   ├── reservoir.py     # Hydromancer Reservoir S3 historical data
│   └── market.py        # Live market data via Hyperliquid Info API
├── bot/
│   ├── exchange.py      # Order placement & position management (SDK)
│   ├── strategy.py      # EMA crossover + RSI strategy
│   └── risk.py          # Position sizing, drawdown, SL/TP logic
└── utils/
    └── logger.py        # Structured logging (console + rotating file)
```

---

## Prerequisites

- Python 3.10+
- A Hyperliquid account (testnet recommended for initial runs)
- AWS credentials with S3 access (for Reservoir historical data)

---

## Setup

### 1. Clone / enter the project directory

```bash
cd hyper
```

### 2. Create a virtual environment

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure environment variables

```bash
cp .env.example .env
```

Then edit `.env` with your values:

| Variable | Description |
|---|---|
| `PRIVATE_KEY` | Your wallet private key (hex). Never share this. |
| `API_WALLET_ADDRESS` | Your Hyperliquid wallet address (`0x...`). |
| `USE_TESTNET` | `true` for testnet (safe), `false` for mainnet. |
| `AWS_ACCESS_KEY_ID` | AWS key for S3 requester-pays access. |
| `AWS_SECRET_ACCESS_KEY` | AWS secret for S3 access. |
| `AWS_DEFAULT_REGION` | AWS region (default: `us-east-1`). |
| `SYMBOL` | Coin to trade (default: `BTC`). |

> **Security**: `.env` is git-ignored by default. Never commit it.

---

## Testnet Quickstart

1. Get testnet funds at [app.hyperliquid-testnet.xyz](https://app.hyperliquid-testnet.xyz)
2. Ensure `USE_TESTNET=true` in your `.env`
3. Run the bot:

```bash
python main.py
```

The bot will:
- Connect to the testnet API
- Set leverage for your symbol
- Begin polling every 60 seconds for EMA crossover signals
- Log all actions to console and `logs/bot.log`

---

## Strategy: EMA Crossover + RSI Filter

### Entry signals

| Signal | Condition |
|---|---|
| **LONG** | Fast EMA (9) crosses **above** Slow EMA (21) AND RSI is between 30–70 |
| **SHORT** | Fast EMA (9) crosses **below** Slow EMA (21) AND RSI is between 30–70 |
| **No trade** | No crossover, OR RSI ≤ 30 or ≥ 70 |

RSI boundaries prevent chasing oversold/overbought conditions.

### Stop-loss & Take-profit (ATR-based)

```
Stop-loss   = entry ± 1.5 × ATR(14)
Take-profit = entry ± 3.0 × ATR(14)
```

Risk/reward ratio: **1:2** minimum.

---

## Risk Management

| Parameter | Default | Description |
|---|---|---|
| `MAX_RISK_PER_TRADE` | 2% | Max equity loss if SL is hit |
| `MAX_POSITIONS` | 3 | Max concurrent open positions |
| `max_drawdown_halt` | 10% | Halt all new trades if equity drops 10% from peak |
| `min_order_usd` | $10 | Minimum order notional value |
| `leverage` | 1× | Position leverage (change carefully) |

Position size formula:
```
size = (equity × risk_%) / |entry_price - stop_loss_price|
```

---

## Historical Data — Hydromancer Reservoir

The bot uses [Hydromancer Reservoir](https://hydromancer.xyz), an S3-based archive of Hyperliquid data:

| Dataset | S3 prefix | Content |
|---|---|---|
| Candles | `candles/<COIN>/YYYY-MM-DD.parquet` | 1-second OHLCV |
| Fills | `fills/YYYY-MM-DD.parquet` | Trades, liquidations, ADLs |
| Snapshots | `snapshots/YYYY-MM-DD.parquet` | Daily positions & balances |

**Bucket**: `s3://hyperliquid-reservoir` (requester-pays — your AWS account is billed for S3 request costs).

If S3 is unavailable, the bot automatically falls back to Hyperliquid's native candle REST endpoint.

---

## Logging

- **Console**: INFO level, colour-coded by severity
- **File**: `logs/bot.log`, rotates daily, keeps 7 days
- **Trade events**: tagged `[SIGNAL]`, `[ORDER]`, `[POSITION]`, `[RISK]`

```
2026-03-28 12:00:00  INFO      trade  [SIGNAL] symbol=BTC direction=LONG fast_ema=84123.4 slow_ema=83987.1 rsi=52.3 atr=412.7
2026-03-28 12:00:01  INFO      trade  [ORDER] action=SUBMIT symbol=BTC side=buy size=0.001000 price=MARKET
2026-03-28 12:00:01  INFO      trade  [ORDER] action=CONFIRMED symbol=BTC side=buy size=0.001000 price=MARKET order_id=12345
```

---

## Configuration Reference

All strategy and risk parameters can be overridden in `.env`:

```bash
EMA_FAST=9
EMA_SLOW=21
RSI_PERIOD=14
ATR_PERIOD=14
MAX_RISK_PER_TRADE=0.02
MAX_POSITIONS=3
LOOP_INTERVAL=60
```

Or by editing `config.py` directly for non-secret values.

---

## Safety Checklist

Before going live on mainnet:

- [ ] Test end-to-end on testnet with small amounts
- [ ] Confirm stop-loss orders are being triggered correctly
- [ ] Review drawdown halt behaviour
- [ ] Ensure `.env` is in `.gitignore`
- [ ] Set `USE_TESTNET=false` only when ready
- [ ] Start with `leverage=1` (no leverage)
- [ ] Monitor `logs/bot.log` actively for the first few hours

---

## Disclaimer

This software is provided for educational purposes. Algorithmic trading involves significant financial risk. Always test on testnet first. The authors are not responsible for any financial losses.
