"""Portfolio paper bot with Telegram updates. No exchange keys, no real money.
One signal engine feeds two resource-management models, both starting with 10,000 USDT:
  Model A: all-in. One position at a time, the whole balance is the margin.
  Model B: five slots. Up to 5 positions at once, 2,000 USDT margin each.
Both use the same fixed stop, target and leverage, so only the money management differs.
Runs every few minutes on GitHub Actions. State: state.json. Results: trades.csv, daily.csv."""
import csv, json, os, time, urllib.parse, urllib.request

VERSION = 1
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


def fmt(x):
    a = abs(x)
    return f"{x:,.1f}" if a >= 1000 else f"{x:.2f}" if a >= 1 else f"{x:.4f}" if a >= 0.01 else f"{x:.6f}"


def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "portfolio-bot/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _row(k, taker=False):
    r = [int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])]
    return r + [float(k[9])] if taker else r


def _binance(sym, tf):
    last = None
    for host in ("data-api.binance.vision", "api.binance.com", "api1.binance.com"):
        try:
            return [_row(k, True) for k in get_json(f"https://{host}/api/v3/klines?symbol={sym}&interval={tf}&limit=500")]
        except Exception as e:
            last = e
    raise last


def _okx(sym, tf):
    d = get_json(f"https://www.okx.com/api/v5/market/candles?instId={sym.replace('USDT', '-USDT')}&bar={OKX[tf]}&limit=300")["data"]
    return [_row(k) for k in reversed(d)]


def _bybit(sym, tf):
    d = get_json(f"https://api.bybit.com/v5/market/kline?category=spot&symbol={sym}&interval={BYBIT[tf]}&limit=500")["result"]["list"]
    return [_row(k) for k in reversed(d)]


def fetch(sym, tf):
    errs = []
    for name, fn in (("binance", _binance), ("okx", _okx), ("bybit", _bybit)):
        try:
            rows = fn(sym, tf)
            if len(rows) > 250:
                return rows
        except Exception as e:
            errs.append(f"{name}: {e}")
    raise RuntimeError("; ".join(errs) or "not enough data")


def ewm(a, alpha):
    out, p = [], None
    for x in a:
        p = x if p is None else alpha * x + (1 - alpha) * p
        out.append(p)
    return out


def ema(a, n):
    return ewm(a, 2 / (n + 1))


def rsi(c):
    d = [0.0] + [c[i] - c[i - 1] for i in range(1, len(c))]
    u, w = ewm([max(x, 0) for x in d], 1 / 14), ewm([max(-x, 0) for x in d], 1 / 14)
    return [50.0 if wi == 0 else 100 - 100 / (1 + ui / wi) for ui, wi in zip(u, w)]


def calc(rows):
    t, o, h, l, c, v = ([r[k] for r in rows] for k in range(6))
    n = len(c)
    tr = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])) for i in range(1, n)]
    vr = [None] * n
    for i in range(19, n):
        m = sum(v[i - 19:i + 1]) / 20
        vr[i] = v[i] / m if m > 0 else None
    has_tb = len(rows[0]) > 6
    fl = [None] * n
    if has_tb:
        d2 = [2 * r[6] - r[5] for r in rows]
        for i in range(4, n):
            s = sum(v[i - 4:i + 1])
            fl[i] = sum(d2[i - 4:i + 1]) / s if s > 0 else None
    vw, bd, day, pv, vv, k = [], [], -1, 0.0, 0.0, 0
    for i in range(n):
        d = t[i] // 86_400_000
        if d != day:
            day, pv, vv, k = d, 0.0, 0.0, 0
        k += 1
        pv += (h[i] + l[i] + c[i]) / 3 * v[i]
        vv += v[i]
        vw.append(pv / vv if vv > 0 else None)
        bd.append(k)
    return dict(n=n, t=t, o=o, h=h, l=l, c=c, e9=ema(c, 9), e21=ema(c, 21), e50=ema(c, 50),
                e200=ema(c, 200), rs=rsi(c), atr=ewm(tr, 1 / 14), vr=vr, vw=vw, bd=bd, fl=fl)


def sig(D, i):
    """Dip to EMA 21 and a bounce candle is mandatory, plus most of the other conditions."""
    if i < 200 or D["bd"][i] < 6 or D["vr"][i] is None or D["vw"][i] is None:
        return None
    c, o, a, vw, fl, vr, rs = D["c"][i], D["o"][i], D["atr"][i], D["vw"][i], D["fl"][i], D["vr"][i], D["rs"][i]
    e9, e21, e50, e200 = D["e9"][i], D["e21"][i], D["e50"][i], D["e200"][i]
    lo3, hi3 = min(D["l"][i - 2:i + 1]), max(D["h"][i - 2:i + 1])
    rng = D["h"][i] - D["l"][i]
    mn, mx = min(D["l"][i - 9:i + 1]), max(D["h"][i - 9:i + 1])

    def mk(L):
        trig = (lo3 <= e21 and c > e9 and c > o) if L else (hi3 >= e21 and c < e9 and c < o)
        G = [(c > e50 and e50 > e200) if L else (c < e50 and e50 < e200),
             c > vw if L else c < vw,
             (50 <= rs <= 65) if L else (35 <= rs <= 50),
             abs(c - vw) <= 2.5 * a,
             vr < 3 and rng <= 2.5 * a,
             ((c - mn) if L else (mx - c)) <= 4 * a,
             fl is None or ((fl > 0.03) if L else (fl < -0.03))]
        X = [(e9 > e21) if L else (e9 < e21), vr >= 1.2, fl is not None and ((fl > 0.1) if L else (fl < -0.1))]
        return L, trig, G, X

    A, B = mk(True), mk(False)
    L, trig, G, X = max(A, B, key=lambda m: (m[1], sum(m[2]) + sum(m[3]) / 10))
    return dict(long=L, ok=trig and sum(G) >= MIN_OTHER_GATES and sum(X) >= MIN_EXTRAS,
                g=sum(G), x=sum(X), atr=a, close=c, vr=vr)


def notify(text):
    print(text)
    tok, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for ch in chunks:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": ch.strip()}).encode()
            urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data), timeout=15).read()
        except Exception as e:
            print("telegram failed:", e)


def equity(st, m):
    e = st["models"][m]["cash"]
    for p in st["positions"]:
        if p["model"] == m:
            ret = (PX.get(p["sym"], p["entry"]) / p["entry"] - 1) * (1 if p["side"] == "long" else -1)
            e += max(0.0, p["margin"] * (1 + LEVERAGE * ret))
    return e


def close_position(st, p, ts, ex, outcome):
    long = p["side"] == "long"
    ret = (ex / p["entry"] - 1) * (1 if long else -1)
    net = ret - FEE / 100 * (1 + ex / p["entry"])
    pnl = max(-p["margin"], p["margin"] * LEVERAGE * net)
    M = st["models"][p["model"]]
    M["cash"] += p["margin"] + pnl
    M["n"] += 1
    M["wins"] += 1 if pnl > 0 else 0
    M["realized"] = round(M["realized"] + pnl, 2)
    st["positions"].remove(p)
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts / 1000)), p["model"], p["sym"], p["side"],
                                fmt(p["entry"]), fmt(ex), outcome, round(p["margin"]), round(pnl, 2), round(net / (STOP_PCT / 100), 2)])
    EV.append(f"{'✅' if pnl > 0 else '❌'} Model {p['model']} closed {p['sym']} {p['side']} ({outcome}): {pnl:+,.0f} USDT")


def manage(st, sym, D, j):
    ts, lo, hi, c = D["t"][j], D["l"][j], D["h"][j], D["c"][j]
    closed_any = False
    for p in list(st["positions"]):
        if p["sym"] != sym or ts < p["entry_ts"]:
            continue
        long = p["side"] == "long"
        if (long and lo <= p["stop"]) or (not long and hi >= p["stop"]):
            close_position(st, p, ts, p["stop"], "loss")
        elif (long and hi >= p["target"]) or (not long and lo <= p["target"]):
            close_position(st, p, ts, p["target"], "win")
        elif (ts - p["entry_ts"]) // MS[TF] + 1 >= HOLD:
            close_position(st, p, ts, c, "time exit")
        else:
            continue
        closed_any = True
    return closed_any


def scan_coin(st, sym, cands):
    rows = fetch(sym, TF)
    now = int(time.time() * 1000)
    PX[sym] = rows[-1][4]
    cl = rows[:-1] if rows[-1][0] + MS[TF] > now else rows[:]
    D = calc(cl)
    by_ts = {r[0]: r for r in rows}
    cs = st["coins"].setdefault(sym, {"last_ts": 0})
    if cs["last_ts"] == 0:
        cs["last_ts"] = D["t"][-1]      # start fresh from the latest closed candle
        return
    for j in range(D["n"]):
        ts = D["t"][j]
        if ts <= cs["last_ts"]:
            continue
        just_closed = manage(st, sym, D, j)
        if j == D["n"] - 1 and not just_closed:     # only the newest closed candle can start a trade
            S, nxt = sig(D, j), by_ts.get(ts + MS[TF])
            if S and S["ok"] and nxt is not None:
                if now - (ts + MS[TF]) > MS[TF]:
                    print(sym, "signal skipped: too late")
                elif abs(rows[-1][4] - S["close"]) > 0.5 * S["atr"]:
                    print(sym, "signal skipped: price already moved")
                else:
                    cands.append(dict(sym=sym, long=S["long"], entry=nxt[1], entry_ts=ts + MS[TF], x=S["x"], g=S["g"], vr=S["vr"]))
        cs["last_ts"] = ts


def allocate(st, cands):
    cands.sort(key=lambda d: (d["x"], d["g"], d["vr"]), reverse=True)    # strongest signals get the free money first
    for d in cands:
        st["seen"] += 1
        en, L = d["entry"], d["long"]
        risk = en * STOP_PCT / 100
        stop, target = (en - risk, en + TARGET_R * risk) if L else (en + risk, en - TARGET_R * risk)
        took = []
        for m in ("A", "B"):
            M, pos = st["models"][m], st["positions"]
            if any(p["model"] == m and p["sym"] == d["sym"] for p in pos):
                M["skipped"] += 1
                continue
            if m == "A":
                busy = any(p["model"] == "A" for p in pos) or M["cash"] < 100
                margin = M["cash"]
            else:
                busy = sum(1 for p in pos if p["model"] == "B") >= SLOTS or M["cash"] < SLOT_MARGIN
                margin = SLOT_MARGIN
            if busy:
                M["skipped"] += 1
                continue
            M["cash"] -= margin
            pos.append(dict(model=m, sym=d["sym"], side="long" if L else "short", entry=en, stop=stop, target=target,
                            margin=margin, entry_ts=d["entry_ts"]))
            took.append(f"A all-in {margin:,.0f}" if m == "A" else f"B slot {sum(1 for p in pos if p['model'] == 'B')}/{SLOTS}")
        if took:
            EV.append(f"{'🟢 LONG' if L else '🔴 SHORT'} {d['sym']} near {fmt(en)}, stop {fmt(stop)}, target {fmt(target)}  [{' | '.join(took)}]")


def summary(st, label):
    lines = [f"📅 {label}"]
    for m, name in (("A", "A all-in, 1 position"), ("B", f"B {SLOTS} slots x {SLOT_MARGIN:,.0f}")):
        M, e = st["models"][m], equity(st, m)
        opened = sum(1 for p in st["positions"] if p["model"] == m)
        lines.append(f"{name}: equity {e:,.0f} ({(e / START - 1) * 100:+.1f}%), {M['n']} closed, "
                     f"{(M['wins'] / M['n'] * 100 if M['n'] else 0):.0f}% won, worst dip {M['dd'] * 100:.0f}%, open {opened}")
    lines.append(f"Signals seen: {st['seen']}. A skipped {st['models']['A']['skipped']}, B skipped {st['models']['B']['skipped']}.")
    return "\n".join(lines)


def fresh():
    mod = lambda: {"cash": START, "realized": 0.0, "n": 0, "wins": 0, "peak": START, "dd": 0.0, "skipped": 0}
    return {"version": VERSION, "models": {"A": mod(), "B": mod()}, "positions": [], "coins": {}, "seen": 0,
            "day": "", "started": False, "warned": False}


def main():
    for fn, head in ((LOG_FILE, ["time_utc", "model", "coin", "side", "entry", "exit", "result", "margin", "pnl_usdt", "R"]),
                     (DAILY_FILE, ["date_utc", "equity_A", "equity_B", "closed_A", "closed_B", "open_B"])):
        if not os.path.exists(fn):
            with open(fn, "w", newline="") as f:
                csv.writer(f).writerow(head)
    st = json.load(open(STATE_FILE)) if os.path.exists(STATE_FILE) else {}
    if st.get("version") != VERSION:
        st = fresh()
    if not st["started"]:
        notify(f"🤖 Portfolio bot is live. Two models, {START:,.0f} USDT each, same signals: A all-in with one position, "
               f"B up to {SLOTS} positions of {SLOT_MARGIN:,.0f} margin. {LEVERAGE}x, {STOP_PCT:g}% stop, {len(COINS)} coins on {TF}. Paper only.")
        st["started"] = True
    cands, failed = [], 0
    for sym in COINS:
        try:
            scan_coin(st, sym, cands)
        except Exception as e:
            failed += 1
            print(sym, "failed:", e)
    allocate(st, cands)
    for m in ("A", "B"):
        e, M = equity(st, m), st["models"][m]
        M["peak"] = max(M["peak"], round(e, 2))
        M["dd"] = min(M["dd"], round(e / M["peak"] - 1, 4))
    today = time.strftime("%Y-%m-%d", time.gmtime())
    if st["day"] and st["day"] != today:
        EV.append(summary(st, f"Daily summary for {st['day']} UTC"))
        with open(DAILY_FILE, "a", newline="") as f:
            csv.writer(f).writerow([st["day"], round(equity(st, "A")), round(equity(st, "B")), st["models"]["A"]["n"],
                                    st["models"]["B"]["n"], sum(1 for p in st["positions"] if p["model"] == "B")])
    st["day"] = today
    if failed == len(COINS) and not st["warned"]:
        EV.append("⚠️ The bot could not load prices for any coin. Exchanges may be blocking the server. Check the Actions log.")
        st["warned"] = True
    elif failed < len(COINS):
        st["warned"] = False
    if EV:
        notify("📊 Portfolio bot\n" + "\n".join(EV))
    with open(STATE_FILE, "w") as f:
        json.dump(st, f, indent=1)


if __name__ == "__main__":
    main()
