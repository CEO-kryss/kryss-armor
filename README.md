# kryss-armor
[README.md](https://github.com/user-attachments/files/26688486/README.md)
# 🛡️ KRYSS-ARMOR V5.1 — HFT Paper Trading Bot

> Algorithmic trading bot using real-time WebSocket price feeds with a trailing HFT strategy.
> Built for Hyperliquid (simulated fees) — Paper trading mode.

---

## 📌 Overview

KRYSS-ARMOR is a High-Frequency Trading bot that monitors real-time BNB/USDT prices
via Binance WebSocket and executes a trailing buy/sell strategy based on local price
extremes (p_creux / p_sommet).

All trades are simulated (paper trading) with Hyperliquid fee structure applied.

---

## 📊 Live Results (Day 1 — April 13, 2026)

| Metric | Value |
|---|---|
| Session duration | ~5 hours |
| Total trades | 20 |
| Wins | 18 |
| Losses | 2 |
| Win Rate | **90%** |
| Starting capital | 6.50 USDT |
| Final balance | 6.669 USDT |
| Net profit | **+0.073 USDT (+1.13%)** |

---

## ⚙️ Strategy — Trailing p_creux / p_sommet

```
BUY PHASE (Trailing Buy)
─────────────────────────────────────────
Every price tick received :
  → if price < p_creux   : update p_creux (new low)
  → buy_threshold = p_creux × (1 + 0.003%)
  → if price >= threshold : BUY MAKER (bounce confirmed)

SELL PHASE (Trailing Sell)
─────────────────────────────────────────
Every price tick received :
  → if price > p_sommet  : update p_sommet (new high)
  → sell_threshold = p_sommet × (1 - 0.003%)

PROFIT EXIT (TAKER) if :
  ✅ price <= sell_threshold  (reversal from peak confirmed)
  ✅ net_profit > total_fees
  ✅ net_profit >= 0.010 USDT minimum

STOP-LOSS EXIT (TAKER) if :
  ✅ price <= entry_price × (1 - 0.6%)
  → Immediate market order, guarantees exit
```

---

## 💰 Fee Structure (Hyperliquid)

| Order type | Fee | Usage |
|---|---|---|
| Maker | 0.015% | Entry (limit order) |
| Taker | 0.045% | Exit (market order) |
| **Total per trade** | **0.060%** | Maker in + Taker out |

---

## 🔧 Configuration

```python
CAPITAL_DEPART_USDT = Decimal('6.50')   # Starting capital
LEVIER              = 2                  # Leverage x2
SYMBOL              = "BNB"             # Trading pair
FRAIS_MAKER         = Decimal('0.00015') # 0.015%
FRAIS_TAKER         = Decimal('0.00045') # 0.045%

HAUSSE_ACHAT_CREUX  = Decimal('0.00003') # 0.003% bounce to buy
BAISSE_VENTE_SOMMET = Decimal('0.00003') # 0.003% dip to sell
PROFIT_MIN_NET_USDT = Decimal('0.010')   # Minimum net profit
STOP_LOSS_POURCENT  = Decimal('0.006')   # Stop loss at -0.6%
DELAI_SECURITE      = 300                # 5min cooldown after stop
```

---

## 🚀 Features

- ✅ Real-time prices via Binance WebSocket
- ✅ Hyperliquid fee simulation (Maker entry / Taker exit)
- ✅ Leverage x2 simulation
- ✅ Trailing buy on p_creux (local low)
- ✅ Trailing sell on p_sommet (local high)
- ✅ Automatic WebSocket reconnection (no recursion)
- ✅ Thread-safe with Lock()
- ✅ Position persistence across restarts (JSON save)
- ✅ Detailed logging to file
- ✅ Final report + JSON trade export

---

## 📦 Installation

```bash
pip install websocket-client
python kryss_armor_v5_hyperliquid_paper.py
```

Stop with `Ctrl+C` — final report + trade history saved automatically.

---

## 📁 Files

```
kryss_armor_v5_hyperliquid_paper.py   ← Main bot
position_sauvegarde.json              ← Auto-saved open position
paper_trades_YYYYMMDD_HHMMSS.json    ← Trade history export
kryss_armor_errors.log               ← Error log
```

---

## 🗺️ Roadmap

- [x] Paper trading mode with real prices
- [x] Maker/Taker fee split
- [x] Position save/restore on restart
- [ ] Leverage x3 testing
- [ ] Deploy on Railway (20ms ping)
- [ ] Live trading on Hyperliquid

---

## ⚠️ Disclaimer

This bot is for educational and paper trading purposes only.
Do not use with real funds without fully understanding the risks.
Past paper trading performance does not guarantee future real results.

---

*Built with passion by CEO-Kryss — Student & Algo Trading Enthusiast 🇨🇲*
