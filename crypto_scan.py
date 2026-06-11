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
TVPREFIX = {"binance": "BINANCE", "bitget": "BITGET", "bybit": "BYBIT"}


def get_exchange():
    # bybit = cloud-tauglich (Binance/Bitget blocken US-/Azure-IPs der GitHub-Runner mit 451).
    klass = {"binance": ccxt.binanceusdm, "bitget": ccxt.bitget,
             "bybit": ccxt.bybit}.get(EXCHANGE)
    if klass is None:
        raise SystemExit(f"EXCHANGE '{EXCHANGE}' nicht unterstuetzt (binance|bitget|bybit).")
    ex = klass({"enableRateLimit": True})
    if EXCHANGE in ("bitget", "bybit"):     # USDT-Perps; bybit braucht defaultType=swap
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


def log_scan(df):
    """Haengt die aktuellen 'in play'-Coins an crypto_scan_log.csv (dedup je date+coin+exchange).
    Prospektiver, survivorship-freier Krypto-Forward-Record -- analog zum Aktien-Scan."""
    if df is None or len(df) == 0:
        return 0
    now = pd.Timestamp.now(tz="UTC")
    out = pd.DataFrame({
        "date": now.strftime("%Y-%m-%d"), "ts": now.strftime("%Y-%m-%d %H:%M"),
        "exchange": EXCHANGE, "coin": [b + "/" + QUOTE for b in df["base"]],
        "last": df["last"].round(6).values, "pct24h": df["pct"].round(2).values,
        "rvol": df["rvol"].round(2).values, "vol_musd": (df["qvol"] / 1e6).round(1).values,
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


def write_html(df, path):
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
        body.append(
            f'<tr><td class="num" data-val="{i}">{i}</td>'
            f'<td class="sym"><a href="{tv}" target="_blank">{_h.escape(r.base)}/{QUOTE}</a></td>'
            f'<td class="num" data-val="{r.last}">${fmt_price(r.last)}</td>'
            f'<td class="num {cls}" data-val="{r.pct}">{r.pct:+.1f}%</td>'
            f'<td class="num" data-val="{rvv}">{rv}</td>'
            f'<td class="num" data-val="{r.qvol}">{fmt_vol(r.qvol)}</td></tr>')
    doc = (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>Krypto-Scanner {now:%Y-%m-%d}</title><style>{_HTML_CSS}</style></head><body>'
        '<h1>Krypto-Scanner &middot; aktivste Coins (in play)</h1>'
        f'<div class="meta">{now:%Y-%m-%d %H:%M} UTC &nbsp;|&nbsp; {EXCHANGE} USDT-Perps &nbsp;|&nbsp; '
        f'Vol&ge;{fmt_vol(VOL_FLOOR)}, |24h|&ge;{CHG_MIN:.0f}%, RVOL&ge;{RVOL_MIN:.0f}x, '
        f'{"nur +" if DIR=="up" else "beide Richtungen"} &nbsp;|&nbsp; {len(df)} Treffer</div>'
        '<input id="q" placeholder="filtern (Symbol) ..." oninput="filt()">'
        '<table id="t"><thead><tr>'
        '<th onclick="srt(0)">#</th><th class="sym" onclick="srt(1)">Coin</th>'
        '<th onclick="srt(2)">Price</th><th onclick="srt(3)">24h %</th>'
        '<th onclick="srt(4)">RVOL</th><th onclick="srt(5)">24h Vol</th>'
        f'</tr></thead><tbody>{"".join(body)}</tbody></table>'
        '<p class="foot">RVOL = 24h-Volumen / 30-Tage-Schnitt = ungewoehnliche Aktivitaet '
        '(der "in play"-Kern). Klick Spaltenkopf = sortieren; Coin = TradingView-Chart. '
        'Krypto ist 24/7 -> 24h rollierend statt Tages-Gap. PAPER/Research.</p>'
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
    p = write_html(df, HTML)
    n_days = log_scan(df)
    print(f"  [html] {p}  (im Browser oeffnen -- sortier-/filterbar)")
    print(f"  [log]  crypto_scan_log.csv: {len(df)} Coins geloggt | {n_days} Tage im Track.")
    print("  Lesart: hohes RVOL = ungewoehnlich aktiv ('in play'). Ranking nach RVOL, nicht")
    print("  nach absolutem Volumen (sonst stehen immer nur BTC/ETH oben). PAPER/Research.")
