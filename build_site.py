"""
Cloud-Builder (laeuft in GitHub Actions). Fuehrt beide Scanner aus und legt die
Ergebnisse + eine mobile Startseite unter docs/ ab (von GitHub Pages serviert).

Robust: faellt EIN Scanner aus (z.B. Boerse blockt Cloud-IP, oder TVRemix 429),
bleibt das letzte gute docs/-HTML des anderen erhalten und die Seite wird mit
Hinweis-Badge gebaut. Exit-Code immer 0, damit der Commit/Deploy-Step laeuft.

ENV:
  TVREMIX_API_KEY   -- GitHub-Secret, fuer den Aktien-Scan (Pflicht fuer Aktien)
  CRYPTO_EXCHANGE   -- Boerse fuer den Krypto-Scan (default bybit; cloud-tauglich)
"""
import os, sys, subprocess, shutil, datetime, html, json

DIR  = os.path.dirname(os.path.abspath(__file__))
DOCS = os.path.join(DIR, "docs")
PY   = sys.executable
os.makedirs(DOCS, exist_ok=True)


def run(label, args, extra_env=None):
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    try:
        r = subprocess.run([PY, *args], cwd=DIR, env=env,
                            capture_output=True, text=True, timeout=900)
        print(f"----- {label}  (exit {r.returncode}) -----")
        sys.stdout.write((r.stdout or "")[-3000:])
        if (r.stderr or "").strip():
            sys.stdout.write("\n[stderr] " + r.stderr[-1500:])
        print()
        return r.returncode == 0
    except Exception as ex:
        print(f"{label}: FEHLER {ex}")
        return False


def copy_if(src, dst):
    s = os.path.join(DIR, src)
    if os.path.exists(s):
        shutil.copyfile(s, os.path.join(DOCS, dst))
        return True
    return False


def teaser(csv, by, sym_col):
    """Top-3-Zeile des juengsten Tages als Vorschau fuer die Startseite."""
    p = os.path.join(DIR, csv)
    if not os.path.exists(p):
        return ""
    try:
        import pandas as pd
        d = pd.read_csv(p)
        if not len(d):
            return ""
        if "date" in d:
            d = d[d["date"] == d["date"].max()]
        d = d.sort_values(by, ascending=False).drop_duplicates(sym_col).head(3)
        return ", ".join(f"{r[sym_col]} {r[by]:+.0f}%" for _, r in d.iterrows())
    except Exception:
        return ""


def render_regime(json_path, html_out):
    """Render the multi-asset regime allocation page from the signal JSON.
    Returns (ok, teaser)."""
    if not os.path.exists(json_path):
        return False, ""
    try:
        d = json.load(open(json_path))
    except Exception:
        return False, ""
    n = d["n_assets"]
    coins = ""
    for a in d["assets"]:
        per = a["exposure"] / n * 100
        cls = "on" if a["gate"] > 0 else "off"
        px = a["price"]
        pxs = f"${px:,.2f}" if px >= 1 else f"${px:,.4f}"
        coins += (
            f'<div class="coin"><div class="cl">'
            f'<span class="sym">{html.escape(a["sym"])}</span>'
            f'<span class="hold {cls}">{per:.1f}%</span></div>'
            f'<div class="cr"><span>{pxs}</span><span>EMAs {a["emas_above"]}/4</span>'
            f'<span>Gate {a["gate"]*100:.0f}%</span><span>Vol {a["vol"]*100:.0f}%</span></div></div>'
        )
    port = d["portfolio_exposure"] * 100
    teaser = (f"Portfolio {port:.0f}% long" if port > 1 else "Portfolio 0% – alles Cash")
    page = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Krypto-Regime</title><style>
:root{{color-scheme:dark}}
body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#0f1115;color:#e6e9ef}}
.wrap{{max-width:560px;margin:0 auto;padding:20px 16px 40px}}
h1{{font-size:20px;margin:2px 0}} .sub{{color:#9aa4b2;font-size:12px;margin:2px 0 16px}}
.big{{background:#171a21;border:1px solid #232733;border-radius:14px;padding:18px;text-align:center;margin-bottom:16px}}
.bign{{font-size:34px;font-weight:720;color:#5ee08a}} .bign.zero{{color:#e0b15e}}
.bigl{{color:#9aa4b2;font-size:13px;margin-top:4px}}
.coin{{background:#171a21;border:1px solid #232733;border-radius:12px;padding:12px 14px;margin-bottom:10px}}
.cl{{display:flex;justify-content:space-between;align-items:center}}
.sym{{font-size:17px;font-weight:650}} .hold{{font-size:17px;font-weight:700}}
.hold.on{{color:#5ee08a}} .hold.off{{color:#6c7686}}
.cr{{display:flex;gap:12px;flex-wrap:wrap;color:#9aa4b2;font-size:12px;margin-top:6px}}
.foot{{color:#7a8493;font-size:12px;margin-top:18px;line-height:1.55}}
a.back{{color:#6ea8fe;text-decoration:none;font-size:13px}}
</style></head><body><div class="wrap">
<a class="back" href="index.html">&#8592; Scanner</a>
<h1>&#129518; Krypto-Regime-Portfolio</h1>
<div class="sub">{d["date"]} &middot; Quelle {html.escape(d["source"])} &middot; Ziel-Vola {d["target_vol"]*100:.0f}% &middot; gleichgewichtet 1/{n}</div>
<div class="big"><div class="bign {'zero' if port<=1 else ''}">{port:.0f}%</div>
<div class="bigl">des Kapitals long (Rest Cash) &middot; pro Coin = Anteil/{n}</div></div>
{coins}
<p class="foot">EMA-Regime-Gate (Anteil der EMAs 50/100/150/200 &uuml;ber dem Preis) &times; inverse-Vola-Sizing.
Long nur in Aufw&auml;rtstrends, Gr&ouml;&szlig;e nach Vola gedeckelt. Ehrliche Erwartung (Walk-Forward):
Sharpe&nbsp;~0.65, max&nbsp;Drawdown&nbsp;~25&ndash;30%. Rebalance 1&times;t&auml;glich nach Tagesschluss.
Risk-controlled Beta, kein Alpha &middot; PAPER/Research, keine Anlageberatung.</p>
</div></body></html>"""
    with open(html_out, "w", encoding="utf-8") as f:
        f.write(page)
    return True, teaser


def badge(ok, has):
    if ok:  return '<span class="ok">aktualisiert</span>'
    if has: return '<span class="stale">letzter Stand</span>'
    return '<span class="err">nicht verfuegbar</span>'


now = datetime.datetime.now(datetime.timezone.utc)
# Boersen-Kette: erste, die durchlaeuft. GitHub-Runner-IPs werden von Binance/Bitget/
# Bybit/OKX geblockt (403/451); gate/mexc sind cloud-tauglich. Reihenfolge per
# CRYPTO_EXCHANGE (Komma-Liste).
CRYPTO_CHAIN = [e.strip() for e in os.getenv("CRYPTO_EXCHANGE", "gate,mexc,bybit").split(",") if e.strip()]

# Aktien: vorboerslich (Gap-Mode) am sinnvollsten -> Cron auf Premarket-ET legen.
stock_ok = run("Aktien-Scan", ["stock_momentum.py", "scan"])

crypto_ok = False
CEX = CRYPTO_CHAIN[0] if CRYPTO_CHAIN else "gate"
for cex in CRYPTO_CHAIN:
    if run(f"Krypto-Scan [{cex}]", ["crypto_scan.py"], extra_env={"EXCHANGE": cex, "DIR": "up"}):
        crypto_ok = True; CEX = cex
        print(f"  [crypto] Boerse {cex} OK")
        break
    print(f"  [crypto] {cex} fehlgeschlagen -> naechste Boerse")

has_stock  = copy_if("stock_scan.html",  "stock.html")
has_crypto = copy_if("crypto_scan.html", "crypto.html")

# Krypto-Regime-Portfolio (BTC/ETH/XRP/SOL/LINK) -- eigenes Boersen-Fallback intern.
run("Krypto-Regime", ["crypto_regime_signal.py", "0.40", "--json", os.path.join(DOCS, "regime.json")])
regime_ok, regime_teaser = render_regime(os.path.join(DOCS, "regime.json"),
                                         os.path.join(DOCS, "regime.html"))
has_regime = os.path.exists(os.path.join(DOCS, "regime.html"))

stock_teaser  = teaser("stock_scan_log.csv",  "change", "symbol")
crypto_teaser = teaser("crypto_scan_log.csv", "pct24h", "coin")

index = f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Scanner</title><style>
:root{{color-scheme:dark}}
body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;margin:0;background:#0f1115;color:#e6e9ef}}
.wrap{{max-width:560px;margin:0 auto;padding:22px 16px 40px}}
h1{{font-size:21px;margin:4px 0 2px}} .sub{{color:#9aa4b2;font-size:13px;margin:0 0 20px}}
a.card{{display:block;text-decoration:none;color:inherit;background:#171a21;border:1px solid #232733;
 border-radius:14px;padding:16px 18px;margin:0 0 14px}}
a.card:active{{background:#1c2029}}
.rowt{{display:flex;align-items:center;justify-content:space-between;gap:10px}}
.t{{font-size:17px;font-weight:650}} .em{{font-size:21px;margin-right:9px}}
.teaser{{color:#c4ccd8;font-size:13px;margin-top:9px;min-height:18px}}
.ok{{color:#5ee08a}} .stale{{color:#e0b15e}} .err{{color:#ff6b6b}} .badge2{{font-size:12px;font-weight:600;white-space:nowrap}}
.foot{{color:#7a8493;font-size:12px;margin-top:22px;line-height:1.55}}
</style></head><body><div class="wrap">
<h1>&#128225; Scanner</h1>
<div class="sub">{now:%Y-%m-%d %H:%M} UTC &middot; automatisch aktualisiert (GitHub Actions)</div>

<a class="card" href="stock.html">
  <div class="rowt"><div><span class="em">&#128200;</span><span class="t">Aktien &middot; Warrior Gap-Scanner</span></div>
  <span class="badge2">{badge(stock_ok, has_stock)}</span></div>
  <div class="teaser">{html.escape(stock_teaser) or 'keine Treffer / kein Lauf'}</div>
</a>

<a class="card" href="crypto.html">
  <div class="rowt"><div><span class="em">&#129689;</span><span class="t">Krypto &middot; in play (RVOL)</span></div>
  <span class="badge2">{badge(crypto_ok, has_crypto)}</span></div>
  <div class="teaser">{html.escape(crypto_teaser) or 'keine Treffer / kein Lauf'}</div>
</a>

<a class="card" href="regime.html">
  <div class="rowt"><div><span class="em">&#129518;</span><span class="t">Krypto-Regime &middot; Portfolio-Allokation</span></div>
  <span class="badge2">{badge(regime_ok, has_regime)}</span></div>
  <div class="teaser">{html.escape(regime_teaser) or 'kein Lauf'}</div>
</a>

<p class="foot">Aktien: Ross-Gap-Scanner (vorboerslich Gap&ge;+10%, Float&lt;20M, $1&ndash;20).
Krypto: aktivste {html.escape(CEX)}-USDT-Perps nach RVOL. PAPER/Research, keine Anlageberatung.
Serverlos via GitHub Actions &mdash; aktuell auch wenn dein PC aus ist.</p>
</div></body></html>"""

with open(os.path.join(DOCS, "index.html"), "w", encoding="utf-8") as f:
    f.write(index)

print(f"docs/: index.html | stock.html={has_stock} | crypto.html={has_crypto}")
if not (has_stock or has_crypto):
    print("WARN: kein Scanner-Ergebnis -- Seite zeigt nur Platzhalter.")
sys.exit(0)
