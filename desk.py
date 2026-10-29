"""
Solana memecoin desk - PAPER MODE ONLY.
Scout -> Filter (8 kill rules) -> Analyst (0-100, 70+) -> Executor -> Risk -> Logger.
Telegram is send-only (no polling), so it can't cause 409 conflicts.

Env: TG_TOKEN, TG_CHAT, START_BAL (default 1000), RECAP_HOUR_UTC (default 6 = 07:00 Nigeria)
Deps: pip install requests
Run:  python desk.py
"""
import os, time, json, sqlite3, datetime as dt
import requests

DB = os.getenv("DB_PATH", "desk.db")
TG_TOKEN, TG_CHAT = os.getenv("TG_TOKEN"), os.getenv("TG_CHAT")
START_BAL = float(os.getenv("START_BAL", "1000"))
RECAP_HOUR = int(os.getenv("RECAP_HOUR_UTC", "6"))

# ---- desk rules (your numbers) ----
MIN_AGE_MIN, MAX_AGE_H = 30, 72
MCAP_MIN, MCAP_MAX = 300_000, 20_000_000
MIN_LIQ, MIN_VOL_1H = 40_000, 50_000
MAX_TOP10, MAX_DEV, MAX_BUNDLE, MAX_1H_PUMP = 25.0, 3.0, 10.0, 80.0
MIN_SCORE = 70
SIZE_PCT, STOP_PCT, TRAIL_ARM_PCT = 0.08, 0.06, 0.12
TRAIL_PCT = float(os.getenv("TRAIL_PCT", "0.08"))  # YOUR CALL: trail distance wasn't specified
# ---- paper realism: adverse fills ----
ENTRY_SLIP, EXIT_SLIP = 0.015, 0.03

DEX = "https://api.dexscreener.com"
RUG = "https://api.rugcheck.xyz/v1/tokens/{}/report"
RECHECK_SEC = 30 * 60

def now(): return int(time.time())

def get(url):
    try:
        r = requests.get(url, timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None

def tg(msg):
    print(msg)
    if not (TG_TOKEN and TG_CHAT):
        print("TG ERROR: TG_TOKEN or TG_CHAT env var is empty"); return
    try:
        r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN.strip()}/sendMessage",
                          json={"chat_id": TG_CHAT.strip(), "text": msg[:4000]}, timeout=15)
        if r.status_code != 200:
            print("TG ERROR:", r.status_code, r.text)
    except Exception as e:
        print("TG ERROR:", e)

# ---------------- storage / logger ----------------
db = sqlite3.connect(DB, check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS kills(ts INT, mint TEXT, sym TEXT, rule TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS seen(mint TEXT PRIMARY KEY, ts INT);
CREATE TABLE IF NOT EXISTS holders(mint TEXT, ts INT, n INT);
CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY, mint TEXT UNIQUE, sym TEXT, score REAL,
  ts_in INT, entry REAL, size REAL, stop REAL, peak REAL, trailing INT DEFAULT 0,
  ts_out INT, exit REAL, pnl REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
""")

def kv_get(k, d=None):
    r = db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r[0] if r else d

def kv_set(k, v):
    db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, str(v))); db.commit()

def log_kill(mint, sym, rule, detail=""):
    db.execute("INSERT INTO kills VALUES(?,?,?,?,?)", (now(), mint, sym, rule, detail)); db.commit()

def balance():
    r = db.execute("SELECT COALESCE(SUM(pnl),0) FROM trades WHERE ts_out IS NOT NULL").fetchone()[0]
    return START_BAL + r

def cash():
    open_sz = db.execute("SELECT COALESCE(SUM(size),0) FROM trades WHERE ts_out IS NULL").fetchone()[0]
    return balance() - open_sz

# ---------------- scout ----------------
def scout():
    addrs = set()
    for ep in ("/token-profiles/latest/v1", "/token-boosts/latest/v1"):
        for it in (get(DEX + ep) or []):
            if it.get("chainId") == "solana": addrs.add(it["tokenAddress"])
    addrs = list(addrs)
    pairs = []
    for i in range(0, len(addrs), 30):
        data = get(f"{DEX}/tokens/v1/solana/{','.join(addrs[i:i+30])}") or []
        pairs += data
    best = {}
    for p in pairs:  # one pair per token: deepest liquidity
        if p.get("dexId") not in ("pumpfun", "pumpswap", "raydium"): continue
        m = p["baseToken"]["address"]
        if m not in best or (p.get("liquidity") or {}).get("usd", 0) > (best[m].get("liquidity") or {}).get("usd", 0):
            best[m] = p
    out = []
    for m, p in best.items():
        created = p.get("pairCreatedAt")
        mcap = p.get("marketCap") or p.get("fdv") or 0
        if not created: continue
        age_min = (now() * 1000 - created) / 60000
        if MIN_AGE_MIN <= age_min <= MAX_AGE_H * 60 and MCAP_MIN <= mcap <= MCAP_MAX:
            out.append(p)
    return out

# ---------------- filter ----------------
def rug_report(mint):
    return get(RUG.format(mint))

def filter_pair(p, rep):
    """Returns (rule_name, detail) if killed, else None. Fails closed on missing data."""
    sym, liq = p["baseToken"]["symbol"], (p.get("liquidity") or {}).get("usd", 0)
    vol1h = (p.get("volume") or {}).get("h1", 0)
    pump1h = (p.get("priceChange") or {}).get("h1", 0) or 0
    # cheap DexScreener rules first
    if liq < MIN_LIQ: return ("3_liquidity", f"${liq:,.0f}")
    if vol1h < MIN_VOL_1H: return ("4_volume_1h", f"${vol1h:,.0f}")
    if pump1h >= MAX_1H_PUMP: return ("8_pumped_1h", f"{pump1h:.0f}%")
    if not rep: return ("data_missing", "rugcheck unavailable (fail closed)")
    if rep.get("mintAuthority") or rep.get("freezeAuthority"):
        return ("1_authority", f"mint={rep.get('mintAuthority')} freeze={rep.get('freezeAuthority')}")
    locked = max([(m.get("lp") or {}).get("lpLockedPct", 0) or 0 for m in rep.get("markets", [])] or [0])
    if locked < 95: return ("2_lp_not_locked", f"{locked:.0f}% locked/burned")
    holders = rep.get("topHolders") or []
    if not holders: return ("data_missing", "no holder data (fail closed)")
    top10 = sum(h.get("pct", 0) for h in holders[:10])
    if top10 > MAX_TOP10: return ("5_top10", f"{top10:.1f}%")
    creator = rep.get("creator")
    dev = sum(h.get("pct", 0) for h in holders if creator and h.get("address") == creator)
    if dev > MAX_DEV or rep.get("rugged"): return ("6_dev", f"dev {dev:.1f}% rugged={rep.get('rugged')}")
    bundled = sum(h.get("pct", 0) for h in holders if h.get("insider"))  # PROXY for bundle %
    if bundled > MAX_BUNDLE: return ("7_bundled", f"insiders {bundled:.1f}%")
    return None

# ---------------- analyst ----------------
def scale(x, lo, hi): return max(0.0, min(1.0, (x - lo) / (hi - lo)))

def score(p, rep):
    tx = (p.get("txns") or {}).get("h1", {})
    buys, sells = tx.get("buys", 0), tx.get("sells", 0)
    m5 = (p.get("priceChange") or {}).get("m5", 0) or 0
    h1 = (p.get("priceChange") or {}).get("h1", 0) or 0
    liq = (p.get("liquidity") or {}).get("usd", 0)
    mcap = p.get("marketCap") or p.get("fdv") or 1
    n = (rep or {}).get("totalHolders") or 0
    prev = db.execute("SELECT n FROM holders WHERE mint=? ORDER BY ts ASC LIMIT 1",
                      (p["baseToken"]["address"],)).fetchone()
    growth = ((n - prev[0]) / prev[0] * 100) if prev and prev[0] else 0
    db.execute("INSERT INTO holders VALUES(?,?,?)", (p["baseToken"]["address"], now(), n)); db.commit()
    parts = {
        "momentum":  20 * scale(0.5 * m5 + 0.5 * h1 / 4, 0, 10),          # sustained, not blown-off
        "buyers":    15 * scale(buys, 100, 1000),                          # PROXY: buy tx count, not unique wallets
        "bs_ratio":  20 * scale(buys / max(sells, 1), 1.0, 2.0),
        "holders":   15 * scale(growth, 0, 20),                            # growth since first sighting
        "liq_mcap":  15 * scale(liq / mcap, 0.03, 0.15),
        "smart":      0,                                                   # STUB: needs GMGN/Birdeye wallet data
    }
    return sum(parts.values()), parts

# ---------------- executor / risk ----------------
def open_trade(p, sc):
    mint, sym = p["baseToken"]["address"], p["baseToken"]["symbol"]
    if db.execute("SELECT 1 FROM trades WHERE mint=?", (mint,)).fetchone(): return  # one entry per coin, ever
    size = round(balance() * SIZE_PCT, 2)
    if size > cash(): return
    px = float(p["priceUsd"]); fill = px * (1 + ENTRY_SLIP)
    stop = fill * (1 - STOP_PCT)  # set BEFORE the order is recorded
    db.execute("INSERT INTO trades(mint,sym,score,ts_in,entry,size,stop,peak) VALUES(?,?,?,?,?,?,?,?)",
               (mint, sym, sc, now(), fill, size, stop, fill)); db.commit()
    tg(f"PAPER BUY {sym} ${size} @ {fill:.8g} | score {sc:.0f} | stop {stop:.8g}")

def monitor():
    rows = db.execute("SELECT id,mint,sym,entry,size,stop,peak,trailing FROM trades WHERE ts_out IS NULL").fetchall()
    if not rows: return
    prices = {}
    for i in range(0, len(rows), 30):
        chunk = rows[i:i+30]
        for p in get(f"{DEX}/tokens/v1/solana/{','.join(r[1] for r in chunk)}") or []:
            m = p["baseToken"]["address"]
            if m not in prices or (p.get("liquidity") or {}).get("usd", 0) > prices[m][1]:
                prices[m] = (float(p["priceUsd"]), (p.get("liquidity") or {}).get("usd", 0))
    for tid, mint, sym, entry, size, stop, peak, trailing in rows:
        if mint not in prices: continue
        px = prices[mint][0]
        if px > peak: peak = px
        if not trailing and peak >= entry * (1 + TRAIL_ARM_PCT): trailing = 1
        if trailing: stop = max(stop, peak * (1 - TRAIL_PCT))  # stop only ever moves up
        if px <= stop:
            fill = px * (1 - EXIT_SLIP)  # fills at the observed price minus slippage, NOT at the stop level
            pnl = round(size * (fill / entry - 1), 2)
            why = "trail" if trailing else "stop"
            db.execute("UPDATE trades SET ts_out=?,exit=?,pnl=?,reason=? WHERE id=?", (now(), fill, pnl, why, tid))
            tg(f"PAPER SELL {sym} via {why} | pnl ${pnl} ({(fill/entry-1)*100:.1f}%) | bal ${balance():.2f}")
        else:
            db.execute("UPDATE trades SET peak=?,stop=?,trailing=? WHERE id=?", (peak, stop, trailing, tid))
    db.commit()

# ---------------- recap ----------------
def recap():
    since = now() - 86400
    kills = db.execute("SELECT rule,COUNT(*) FROM kills WHERE ts>? GROUP BY rule ORDER BY rule", (since,)).fetchall()
    scanned = kv_get("scanned_24h", "0")
    tr = db.execute("SELECT sym,pnl,reason FROM trades WHERE ts_out>?", (since,)).fetchall()
    opened = db.execute("SELECT COUNT(*) FROM trades WHERE ts_in>?", (since,)).fetchone()[0]
    wins = [t for t in tr if t[1] > 0]; losses = [t for t in tr if t[1] <= 0]
    best = max(tr, key=lambda t: t[1]) if tr else None
    worst = min(tr, key=lambda t: t[1]) if tr else None
    lines = ["MORNING RECAP (paper)", f"Scanned: {scanned}",
             "Killed: " + (", ".join(f"{r}={n}" for r, n in kills) or "none"),
             f"Traded: {opened} | closed {len(tr)} | W/L {len(wins)}/{len(losses)}",
             f"Best: {best[0]} ${best[1]}" if best else "Best: -",
             f"Worst: {worst[0]} ${worst[1]}" if worst else "Worst: -",
             f"Final balance: ${balance():.2f} (start ${START_BAL:.0f})"]
    tg("\n".join(lines))
    kv_set("scanned_24h", 0)

def maybe_recap():
    today = dt.datetime.utcnow().strftime("%Y-%m-%d")
    if dt.datetime.utcnow().hour >= RECAP_HOUR and kv_get("last_recap") != today:
        recap(); kv_set("last_recap", today)

# ---------------- main loop ----------------
def cycle():
    for p in scout():
        mint, sym = p["baseToken"]["address"], p["baseToken"]["symbol"]
        r = db.execute("SELECT ts FROM seen WHERE mint=?", (mint,)).fetchone()
        if r and now() - r[0] < RECHECK_SEC: continue
        db.execute("INSERT OR REPLACE INTO seen VALUES(?,?)", (mint, now())); db.commit()
        kv_set("scanned_24h", int(kv_get("scanned_24h", 0)) + 1)
        rep = rug_report(mint)
        killed = filter_pair(p, rep)
        if killed:
            log_kill(mint, sym, *killed); continue
        sc, parts = score(p, rep)
        if sc < MIN_SCORE:
            log_kill(mint, sym, "analyst_<70", f"{sc:.0f} {json.dumps({k: round(v) for k, v in parts.items()})}")
            continue
        open_trade(p, sc)

def serve_health():
    """Render web services must bind $PORT. Also gives an uptime pinger something to hit."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(f"desk ok | bal {balance():.2f}".encode())
        def log_message(self, *a): pass
    srv = HTTPServer(("0.0.0.0", int(os.getenv("PORT", "10000"))), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

if __name__ == "__main__":
    serve_health()
    tg(f"Desk online (PAPER). Balance ${balance():.2f}")
    last_scan = 0
    while True:
        try:
            monitor()
            if time.time() - last_scan > 60:
                cycle(); last_scan = time.time()
            maybe_recap()
        except Exception as e:
            print("loop error:", e)
        time.sleep(15)
