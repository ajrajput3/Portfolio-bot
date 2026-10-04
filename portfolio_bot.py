"""Portfolio paper bot with Telegram coaching alerts: when to buy, short, move the stop, book profit, exit. No exchange keys, no real money.
One signal engine feeds two resource-management models, both starting with 10,000 USDT:
  Model A: all-in. One position at a time, the whole balance is the margin.
  Model B: five slots. Up to 5 positions at once, 2,000 USDT margin each.
Both use the same fixed stop, target and leverage, so only the money management differs.
Runs every few minutes on GitHub Actions. State: state.json. Results: trades.csv, daily.csv."""
import csv, json, os, time, urllib.parse, urllib.request

VERSION = 1
RISK_PCT = 1.0          # only used for the safer-size line in alerts
START = 10_000.0
LEVERAGE = 5
STOP_PCT = 4.5          # stop loss, percent from entry
TARGET_R = 1.5          # target = 1.5 x the stop distance (6.75%)
FEE = 0.06              # percent per side on the position, fee plus slippage. 0 switches it off.
SLOTS, SLOT_MARGIN = 5, 2_000.0
TF = "15m"
HOLD = 24               # candles before a time exit (24 x 15m = 6 hours)
MIN_OTHER_GATES = 5     # of 7, on top of the mandatory bounce candle. Lower = more trades.
MIN_EXTRAS = 1          # of 3. Lower = more trades.
COINS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
         "LINKUSDT", "DOTUSDT", "LTCUSDT", "TRXUSDT", "ATOMUSDT", "NEARUSDT", "APTUSDT", "ARBUSDT",
         "OPUSDT", "SUIUSDT", "AAVEUSDT", "INJUSDT"]

MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}
BYBIT = {"5m": "5", "15m": "15", "1h": "60"}
OKX = {"5m": "5m", "15m": "15m", "1h": "1H"}
STATE_FILE, LOG_FILE, DAILY_FILE = "state.json", "trades.csv", "daily.csv"
EV, PX = [], {}         # events for this run, latest prices for this run

