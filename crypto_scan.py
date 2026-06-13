"""
Krypto-Scanner -- findet die AKTIVSTEN Coins ("in play"), analog zu Ross' Aktien-
Scanner, aber mit krypto-gerechten Kriterien (kein Float/Gap/Vorboerse; 24/7).

Kriterien (alle per Env justierbar):
  1) LIQUIDITAET : 24h-Quote-Volumen >= VOL_FLOOR ($, default 10M)  -- statt OTC-Filter
  2) BEWEGUNG    : |24h-%| >= CHG_MIN (default 5%)  -- DIR=up nur Long-Momentum
  3) REL. VOLUMEN: RVOL = 24h-Vol / 30T-Schnitt >= RVOL_MIN (default 2x) -- der "in play"-Kern
  4) Stablecoins/illiquide raus; USDT-Perps des gewaehlten Exchange.
Ranking nach RVOL (ungewoehnliche Aktivitaet), nicht nur nach absolutem Volumen
(sonst stehen immer nur BTC/ETH oben).

Datenquelle: ccxt (wie backtest.py). EXCHANGE=binance (binanceusdm, default) | bitget
(= TV-Ground-Truth). Ein fetch_tickers-Call fuer alle Perps + 30T-Tagesvolumen je
Shortlist-Coin (gecacht pro Tag). Schreibt crypto_scan.html (sortier-/filterbar).

  /c/Python313/python.exe crypto_scan.py
  DIR=up /c/Python313/python.exe crypto_scan.py        # nur Long-Momentum (+%)
  EXCHANGE=bitget RVOL_MIN=3 /c/Python313/python.exe crypto_scan.py
"""
import os, sys, json, time, warnings; warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import ccxt

try:                                   # Konsole robust gegen exotische Coin-Namen
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _ca(s):                            # konsolen-sicherer ASCII-Fallback fuer Labels
    return str(s).encode("ascii", "replace").decode()

EXCHANGE  = os.getenv("EXCHANGE", "binance").lower()
QUOTE     = os.getenv("QUOTE", "USDT")
VOL_FLOOR = float(os.getenv("VOL_FLOOR", "10000000"))   # $ 24h Quote-Volumen
CHG_MIN   = float(os.getenv("CHG_MIN", "5"))            # |24h-%|
RVOL_MIN  = float(os.getenv("RVOL_MIN", "2"))           # RVOL-Schwelle
DIR       = os.getenv("DIR", "abs").lower()             # abs (beide) | up (nur +)
LB        = int(os.getenv("LB", "30"))                  # Tage fuer Volumen-Schnitt
SHORTLIST = int(os.getenv("SHORTLIST", "90"))           # so viele (nach Vol) bekommen RVOL
DISPLAY_N = int(os.getenv("DISPLAY_N", "25"))
_DIR  = os.path.dirname(os.path.abspath(__file__))
HTML  = os.path.join(_DIR, "crypto_scan.html")
CRYPTOLOG = os.path.join(_DIR, "crypto_scan_log.csv")   # taeglicher Forward-Record
CACHE = os.path.join(_DIR, "crypto_rvol_cache.json")    # 30T-Schnitt je Tag gecacht

STABLES = {"USDT", "USDC", "DAI", "TUSD", "FDUSD", "USDD", "USDE", "PYUSD", "BUSD",
           "USTC", "EURT", "EUR", "USDP", "GUSD", "FRAX", "LUSD", "USD1", "USDF"}
TVPREFIX = {"binance": "BINANCE", "bitget": "BITGET", "bybit": "BYBIT",
            "gate": "GATEIO", "mexc": "MEXC"}

# News: erklaeren, was den Volumen-/RVOL-Anstieg ausgeloest hat. Quelle ist
# UNABHAENGIG von der Scan-Boerse -- TVRemix-News fuer GATEIO-Symbole timeouten
# (504), BINANCE hat die beste Krypto-Coverage. Daher NEWS_PREFIX=BINANCE.
SHOW_NEWS   = os.getenv("SHOW_NEWS", "1") == "1"
NEWS_PREFIX = os.getenv("NEWS_PREFIX", "BINANCE")
NEWS_N      = int(os.getenv("NEWS_N", str(DISPLAY_N)))   # nur Top-N bekommen News
try:                                   # _mcp_call/_ascii vom Aktien-Scan wiederverwenden
    from stock_momentum import _mcp_call as _sm_mcp, _ascii as _sm_ascii
    _HAS_NEWS = True
except Exception:
    _HAS_NEWS = False


# Cloud-Tauglichkeit: GitHub-Runner stehen im US-/Azure-IP-Raum. Binance/Bitget/Bybit/OKX
# blocken die (403/451). gate & mexc sind von Cloud-IPs erreichbar -> Default-Kette in
# build_site.py = gate,mexc,bybit. Alle hier nutzen lineare USDT-Perps ("BASE/USDT:USDT").
_EX_CLASSES = {"binance": ccxt.binanceusdm, "bitget": ccxt.bitget, "bybit": ccxt.bybit,
               "gate": ccxt.gate, "mexc": ccxt.mexc}
_SWAP_DEFAULT = {"bitget", "bybit", "gate", "mexc"}   # brauchen options.defaultType=swap


def get_exchange():
    klass = _EX_CLASSES.get(EXCHANGE)
    if klass is None:
        raise SystemExit(f"EXCHANGE '{EXCHANGE}' nicht unterstuetzt ({'|'.join(_EX_CLASSES)}).")
    ex = klass({"enableRateLimit": True})
    if EXCHANGE in _SWAP_DEFAULT:
        ex.options["defaultType"] = "swap"
    ex.load_markets()
    return ex


def base_of(symbol):
    return symbol.split("/")[0]


def fetch_movers(ex):
    """Ein Call: alle Perp-Ticker -> liquide, bewegte USDT-Perps (ohne Stables)."""
    tickers = ex.fetch_tickers()
    rows = []
    for sym, t in tickers.items():
        m = ex.markets.get(sym)
        if not m or not m.get("swap") or m.get("quote") != QUOTE:
            continue
        base = base_of(sym)
        if base in STABLES:
            continue
        last = t.get("last"); pct = t.get("percentage"); qv = t.get("quoteVolume")
        if last is None or qv is None or pct is None:
            continue
        if qv < VOL_FLOOR:
            continue
        if (DIR == "up" and pct < CHG_MIN) or (DIR != "up" and abs(pct) < CHG_MIN):
            continue
        rows.append(dict(symbol=sym, base=base, last=float(last),
                         pct=float(pct), qvol=float(qv)))
    df = pd.DataFrame(rows).sort_values("qvol", ascending=False).reset_index(drop=True)
    return df.head(SHORTLIST)


def _load_cache():
    if os.path.exists(CACHE):
        try:
            return json.load(open(CACHE))
        except Exception:
            return {}
    return {}


def add_rvol(ex, df):
    """RVOL = 24h-Quote-Vol / Schnitt der letzten LB abgeschlossenen Tages-Quote-Volumina.
    Tages-Quote-Vol ~ base_vol * close. Schnitt pro Tag gecacht."""
    today = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    cache = _load_cache(); cache.setdefault(today, {}).setdefault(EXCHANGE, {})
    day_cache = cache[today][EXCHANGE]
    rvols = []
    for sym, qv in zip(df["symbol"], df["qvol"]):
        avg = day_cache.get(sym)
        if avg is None:
            try:
                o = ex.fetch_ohlcv(sym, "1d", limit=LB + 1)
                if len(o) >= 5:
                    qd = [c[5] * c[4] for c in o[:-1]][-LB:]   # base_vol*close, ohne heute
                    avg = float(np.mean(qd)) if qd else None
            except Exception:
                avg = None
            day_cache[sym] = avg
        rvols.append(qv / avg if (avg and avg > 0) else np.nan)
        time.sleep(ex.rateLimit / 1000)
    df = df.copy(); df["rvol"] = rvols
    try:
        json.dump(cache, open(CACHE, "w"))
    except Exception:
        pass
    return df


_CAT_RULES = [
    ("delist", "Delisting"), ("listing", "Listing"), ("listed", "Listing"),
    ("etf", "ETF"), ("spot etf", "ETF"),
    ("hack", "Hack"), ("exploit", "Hack"), ("breach", "Hack"), ("drain", "Hack"),
    ("partner", "Partner"), ("integrat", "Partner"), ("collab", "Partner"),
    ("unlock", "Unlock"), ("vesting", "Unlock"), ("airdrop", "Airdrop"),
    ("mainnet", "Upgrade"), ("upgrade", "Upgrade"), ("hard fork", "Upgrade"), ("halving", "Halving"),
    ("lawsuit", "Regulierung"), ("regulat", "Regulierung"), ("court", "Regulierung"),
    ("sec ", "Regulierung"), ("approv", "Approval"), ("burn", "Burn"),
    ("whale", "Whale"), ("all-time high", "ATH"), ("record high", "ATH"),
]


def _crypto_catalyst(title):
    t = " " + title.lower() + " "
    for k, v in _CAT_RULES:
        if k in t:
            return v
    return ""


def _get_heads(sym, limit, retries):
    data = _sm_mcp("get_news", {"symbol": sym, "limit": limit}, retries=retries)  # kann werfen
    d = data or {}
    heads = d.get("headlines")
    if heads is None and isinstance(d.get("data"), dict):
        heads = d["data"].get("headlines")
    return heads or []


def crypto_news(base, limit=4):
    """Neueste Schlagzeile zu BASE -> (tag, alter, alter_tage, 'Provider: Title') | None | 'ERR'.
    Versucht NEWS_PREFIX (z.B. BINANCE), dann CRYPTO:BASEUSD als Fallback (fuer Coins,
    die nicht auf der News-Boerse gelistet sind)."""
    heads, errored = [], False
    for sym, rt in ((f"{NEWS_PREFIX}:{base}{QUOTE}", 2), (f"CRYPTO:{base}USD", 1)):
        try:
            heads = _get_heads(sym, limit, rt)
        except Exception:
            errored = True; continue
        if heads:
            break
    if not heads:
        return "ERR" if errored else None
    h = heads[0]
    title = _sm_ascii(h.get("title", ""))
    if not title:
        return None
    prov = _sm_ascii(h.get("provider", ""))
    try:
        pub = pd.to_datetime(h.get("published"), utc=True)
        age_d = (pd.Timestamp.now(tz="UTC") - pub).days
        age = f"{age_d}d" if age_d >= 1 else "heute"
    except Exception:
        age_d, age = None, "?"
    return _crypto_catalyst(title), age, age_d, (f"{prov}: {title}" if prov else title)


def add_news(df, top=None):
    """News fuer die Top-N (nach RVOL) Coins, mit Circuit-Breaker bei Rate-Limit.
    Gibt {base -> news_tuple | 'ERR' | None} zurueck."""
    if not (_HAS_NEWS and SHOW_NEWS):
        return {}
    out, streak, off = {}, 0, False
    for base in list(df["base"])[: (top or len(df))]:
        if off:
            out[base] = "ERR"; continue
        ns = crypto_news(base)
        if ns == "ERR":
            streak += 1; out[base] = "ERR"
            if streak >= 4:
                off = True            # genug -- Rest nicht mehr abfragen
        else:
            streak = 0; out[base] = ns
            time.sleep(0.30)
    return out


def log_scan(df, news_map=None):
    """Haengt die aktuellen 'in play'-Coins an crypto_scan_log.csv (dedup je date+coin+exchange).
    Prospektiver, survivorship-freier Krypto-Forward-Record -- analog zum Aktien-Scan.
    news_map (base -> news_tuple) loggt zusaetzlich Tag/Alter/Schlagzeile je Coin."""
    if df is None or len(df) == 0:
        return 0
    now = pd.Timestamp.now(tz="UTC")
    nm = news_map or {}
    n_tag, n_age, n_head = [], [], []
    for b in df["base"]:
        ns = nm.get(b)
        if isinstance(ns, tuple):
            n_tag.append(ns[0] or ""); n_age.append(ns[2] if ns[2] is not None else "")
            n_head.append(ns[3])
        else:
            n_tag.append(""); n_age.append(""); n_head.append("")
    out = pd.DataFrame({
        "date": now.strftime("%Y-%m-%d"), "ts": now.strftime("%Y-%m-%d %H:%M"),
        "exchange": EXCHANGE, "coin": [b + "/" + QUOTE for b in df["base"]],
        "last": df["last"].round(6).values, "pct24h": df["pct"].round(2).values,
        "rvol": df["rvol"].round(2).values, "vol_musd": (df["qvol"] / 1e6).round(1).values,
        "news_tag": n_tag, "news_age_d": n_age, "news_headline": n_head,
    })
    if os.path.exists(CRYPTOLOG):
        out = pd.concat([pd.read_csv(CRYPTOLOG), out], ignore_index=True)
        out = out.drop_duplicates(subset=["date", "coin", "exchange"], keep="last")
    out = out.sort_values(["date", "rvol"], ascending=[True, False]).reset_index(drop=True)
    out.to_csv(CRYPTOLOG, index=False)
    return out["date"].nunique()


def fmt_price(p):
    if p >= 1000: return f"{p:,.0f}"
    if p >= 1:    return f"{p:,.2f}"
    if p >= 0.01: return f"{p:.4f}"
    return f"{p:.6f}"


def fmt_vol(v):
    return f"${v/1e9:.2f}B" if v >= 1e9 else f"${v/1e6:.0f}M"


def write_html(df, path, news_map=None):
    try:
        from stock_momentum import _HTML_CSS, _HTML_JS
    except Exception:
        _HTML_CSS = "body{font-family:sans-serif}"; _HTML_JS = ""
    import html as _h
    now = pd.Timestamp.now(tz="UTC")
    pref = TVPREFIX.get(EXCHANGE, "BINANCE")
    body = []
    for i, r in enumerate(df.itertuples(), 1):
        tv = f"https://www.tradingview.com/chart/?symbol={pref}:{r.base}{QUOTE}.P"
        cls = "up" if r.pct >= 0 else "down"
        rv = f"{r.rvol:.1f}x" if pd.notna(r.rvol) else "?"
        rvv = r.rvol if pd.notna(r.rvol) else -1
        ns = (news_map or {}).get(r.base)
        news_html = '<span class="mut">keine News (eher technisch/Flow)</span>'; age_val = 99999
        if ns == "ERR":
            news_html = '<span class="mut">News n/v (Symbol/Quelle)</span>'; age_val = 99998
        elif isinstance(ns, tuple):
            tag, age, age_d, txt = ns
            age_val = age_d if age_d is not None else 99999
            b = ""
            if age_d is not None and age_d <= 2:
                b += '<span class="badge fresh">FRISCH</span>'
            elif (age_d or 0) > 7:
                b += '<span class="badge old">alt</span>'
            if tag:
                b += f'<span class="badge tag">{_h.escape(tag)}</span>'
            news_html = f'{b}<span class="mut">{_h.escape(age)}</span> {_h.escape(txt)}'
        body.append(
            f'<tr><td class="num" data-val="{i}">{i}</td>'
            f'<td class="sym"><a href="{tv}" target="_blank">{_h.escape(r.base)}/{QUOTE}</a></td>'
            f'<td class="num" data-val="{r.last}">${fmt_price(r.last)}</td>'
            f'<td class="num {cls}" data-val="{r.pct}">{r.pct:+.1f}%</td>'
            f'<td class="num" data-label="RVOL" data-val="{rvv}">{rv}</td>'
            f'<td class="num" data-label="24h Vol" data-val="{r.qvol}">{fmt_vol(r.qvol)}</td>'
            f'<td class="news" data-val="{age_val}">{news_html}</td></tr>')
    doc = (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Krypto-Scanner {now:%Y-%m-%d}</title><style>{_HTML_CSS}</style></head><body>'
        '<h1>Krypto-Scanner &middot; aktivste Coins (in play)</h1>'
        f'<div class="meta">{now:%Y-%m-%d %H:%M} UTC &nbsp;|&nbsp; {EXCHANGE} USDT-Perps &nbsp;|&nbsp; '
        f'Vol&ge;{fmt_vol(VOL_FLOOR)}, |24h|&ge;{CHG_MIN:.0f}%, RVOL&ge;{RVOL_MIN:.0f}x, '
        f'{"nur +" if DIR=="up" else "beide Richtungen"} &nbsp;|&nbsp; {len(df)} Treffer</div>'
        '<input id="q" placeholder="filtern (Symbol oder Schlagzeile) ..." oninput="filt()">'
        '<div class="sortbar"><span class="lbl">Sortieren:</span>'
        '<button onclick="srt(4,1)">RVOL</button><button onclick="srt(3,1)">24h%</button>'
        '<button onclick="srt(5,1)">Vol</button><button onclick="srt(6)">News&nbsp;frisch</button>'
        '<button onclick="srt(1)">Coin</button></div>'
        '<table id="t"><thead><tr>'
        '<th onclick="srt(0)">#</th><th class="sym" onclick="srt(1)">Coin</th>'
        '<th onclick="srt(2)">Price</th><th onclick="srt(3)">24h %</th>'
        '<th onclick="srt(4)">RVOL</th><th onclick="srt(5)">24h Vol</th>'
        '<th class="news" onclick="srt(6)">Katalysator / News</th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table>'
        '<p class="foot">RVOL = 24h-Volumen / 30-Tage-Schnitt = ungewoehnliche Aktivitaet '
        '(der "in play"-Kern). FRISCH (&le;2 Tage) = wahrscheinlicher News-Ausloeser; '
        '&bdquo;keine News&ldquo; bei grossem Move = eher technisch/Flow. News-Quelle: '
        f'{_h.escape(NEWS_PREFIX)} (Coverage), unabh. von der Scan-Boerse. '
        'Klick Spaltenkopf = sortieren; Coin = TradingView-Chart. PAPER/Research.</p>'
        f'<script>{_HTML_JS}</script></body></html>')
    open(path, "w", encoding="utf-8").write(doc)
    return path


if __name__ == "__main__":
    ex = get_exchange()
    W = 84
    print("=" * W)
    print(f"  KRYPTO-SCANNER  --  aktivste Coins (in play)  --  {pd.Timestamp.now(tz='UTC'):%Y-%m-%d %H:%M} UTC")
    print(f"  {EXCHANGE} USDT-Perps | Vol>={fmt_vol(VOL_FLOOR)} | |24h|>={CHG_MIN:.0f}% | "
          f"RVOL>={RVOL_MIN:.0f}x | {'nur +' if DIR=='up' else 'beide Richtungen'}")
    print("=" * W)

    mv = fetch_movers(ex)
    if len(mv) == 0:
        print("  Keine Treffer (Markt ruhig oder Schwellen zu streng).")
        sys.exit(0)
    print(f"  {len(mv)} liquide Mover -> berechne RVOL (30T-Schnitt) ...")
    df = add_rvol(ex, mv)
    df = df[df["rvol"] >= RVOL_MIN].sort_values("rvol", ascending=False).reset_index(drop=True)
    if len(df) == 0:
        print("  Liquide Mover gefunden, aber keiner ueber der RVOL-Schwelle.")
        sys.exit(0)

    print(f"\n  {'#':>3s}  {'COIN':14s}{'PRICE':>12s}{'24h%':>9s}{'RVOL':>8s}{'24h VOL':>10s}")
    print("  " + "-" * (W - 2))
    for i, r in enumerate(df.head(DISPLAY_N).itertuples(), 1):
        rv = f"{r.rvol:.1f}x" if pd.notna(r.rvol) else "?"
        print(f"  {i:>3d}  {_ca(r.base) + '/' + QUOTE:14s}{fmt_price(r.last):>12s}"
              f"{r.pct:>+8.1f}%{rv:>8s}{fmt_vol(r.qvol):>10s}")
    print("  " + "-" * (W - 2))

    news_map = {}
    if _HAS_NEWS and SHOW_NEWS:
        print(f"  News-Katalysatoren (Top {min(NEWS_N, len(df))}, Quelle {NEWS_PREFIX}) ...")
        news_map = add_news(df, top=NEWS_N)
        shown = 0
        for r in df.head(NEWS_N).itertuples():
            ns = news_map.get(r.base)
            if isinstance(ns, tuple) and shown < 8:
                tag, age, age_d, txt = ns
                fresh = "*" if (age_d is not None and age_d <= 2) else " "
                print(f"   {fresh}{_ca(r.base):8s} [{(tag or '-'):11s} {age:>5s}] {_ca(txt)[:72]}")
                shown += 1
        if shown == 0:
            print("   (keine frischen Schlagzeilen -- Moves eher technisch/Flow)")
    elif not _HAS_NEWS:
        print("  [news] uebersprungen (stock_momentum/TVREMIX_API_KEY nicht verfuegbar)")
    print("  " + "-" * (W - 2))

    p = write_html(df, HTML, news_map)
    n_days = log_scan(df, news_map)
    print(f"  [html] {p}  (im Browser oeffnen -- sortier-/filterbar)")
    print(f"  [log]  crypto_scan_log.csv: {len(df)} Coins geloggt | {n_days} Tage im Track.")
    print("  Lesart: hohes RVOL = ungewoehnlich aktiv ('in play'). Ranking nach RVOL, nicht")
    print("  nach absolutem Volumen (sonst stehen immer nur BTC/ETH oben). PAPER/Research.")
