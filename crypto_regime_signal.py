"""
Live signal for the multi-asset crypto EMA-regime / vol-target portfolio.

Outputs the position size to hold TODAY per asset (BTC, ETH, XRP, SOL, LINK) and
the portfolio total. Each asset: gate = share of EMAs {50,100,150,200} the daily
close is above; size = clip(target_vol/realized_vol, 0, 1); exposure = gate*size.
Equal 1/N capital weight -> capital_in_asset = exposure/N.

Cloud-safe: Binance is geoblocked from GitHub runners, so it fetches via a
fallback chain (gate -> mexc -> binance). Writes JSON for the cloud site.

Usage: /c/Python313/python.exe crypto_regime_signal.py [target_vol] [--json PATH]
"""
import sys, json, warnings
warnings.filterwarnings("ignore")
import numpy as np, pandas as pd, ccxt

SYMBOLS = ["BTC", "ETH", "XRP", "SOL", "LINK"]
LENS = [50, 100, 150, 200]
VOL_WIN, BPY, MAX_LEV = 30, 365, 1.0
CHAIN = ["gate", "mexc", "binanceusdm"]
_CLS = {"gate": ccxt.gate, "mexc": ccxt.mexc, "binanceusdm": ccxt.binanceusdm}

TV = 0.40
JSON_PATH = None
_args = sys.argv[1:]
for i, a in enumerate(_args):
    if a == "--json":
        JSON_PATH = _args[i + 1]
    elif a.replace(".", "").isdigit():
        TV = float(a)


def fetch_closes(n_bars=320):
    """Return (dict sym->close-Series, exchange_name) from first working exchange."""
    last_err = None
    for ex_name in CHAIN:
        try:
            ex = _CLS[ex_name]({"enableRateLimit": True})
            ex.load_markets()
            out = {}
            for s in SYMBOLS:
                sym = f"{s}/USDT:USDT"
                raw = ex.fetch_ohlcv(sym, "1d", limit=n_bars)
                if not raw or len(raw) < 220:
                    raise RuntimeError(f"{ex_name}:{sym} only {len(raw) if raw else 0} bars")
                df = pd.DataFrame(raw, columns=["ts", "O", "H", "L", "Close", "V"])
                df["ts"] = pd.to_datetime(df["ts"], unit="ms")
                out[s] = df.set_index("ts")["Close"].astype(float)
            return out, ex_name
        except Exception as e:
            last_err = f"{ex_name}: {e}"
            print(f"[chain] {last_err}", file=sys.stderr)
    raise RuntimeError(f"all exchanges failed; last: {last_err}")


def asset_signal(close, tv):
    c = pd.Series(close)
    last = float(c.iloc[-1])
    rvol = float(np.log(c / c.shift(1)).tail(VOL_WIN).std() * np.sqrt(BPY))
    above = [last > float(c.ewm(span=l, adjust=False).mean().iloc[-1]) for l in LENS]
    gate = float(np.mean(above))
    size = min(MAX_LEV, tv / rvol) if rvol > 0 else 0.0
    return dict(price=last, vol=rvol, gate=gate, size=size,
                exposure=gate * size, emas_above=int(sum(above)))


def run():
    closes, src = fetch_closes()
    date = max(s.index[-1] for s in closes.values()).date().isoformat()
    assets = []
    for s in SYMBOLS:
        d = asset_signal(closes[s], TV); d["sym"] = s
        assets.append(d)
    port = float(np.mean([a["exposure"] for a in assets]))
    result = dict(date=date, target_vol=TV, source=src,
                  portfolio_exposure=port, n_assets=len(SYMBOLS), assets=assets)

    # console
    print("=" * 70)
    print(f"  CRYPTO REGIME PORTFOLIO   {date}   (src: {src}, target_vol {TV:.0%})")
    print("=" * 70)
    print(f"  {'COIN':<6}{'PRICE':>12}{'EMAs>':>7}{'GATE':>7}{'VOL':>7}{'HOLD':>9}")
    for a in assets:
        print(f"  {a['sym']:<6}{a['price']:>12,.2f}{a['emas_above']:>4}/4{a['gate']*100:>6.0f}%"
              f"{a['vol']*100:>6.0f}%{a['exposure']/len(SYMBOLS)*100:>8.1f}%")
    print("-" * 70)
    print(f"  >>> PORTFOLIO LONG TODAY: {port*100:.1f}% of capital "
          f"(rest in cash)   |   per coin = exposure/{len(SYMBOLS)}")
    print("=" * 70)

    if JSON_PATH:
        with open(JSON_PATH, "w") as f:
            json.dump(result, f, indent=2)
        print(f"[json] -> {JSON_PATH}")
    return result


if __name__ == "__main__":
    run()
